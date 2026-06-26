defmodule UnitaresLeasePlane.Canonicalize do
  @moduledoc """
  Server-side canonicalization for surface_id (RFC v0.8 §7.12).

  Mirrors `src/lease_plane/canonicalize.py` so non-Python callers (curl, future
  Hermes/Codex/Elixir clients) cannot bypass the Python field_validator and
  produce split-brain rows in `lease_plane.surface_leases`. The Python and
  Elixir helpers are the single point of truth — two callers using different
  paths to the same logical surface MUST produce the same canonical surface_id
  IFF both go through this helper.

  Returns idiomatic `{:ok, canonical}` / `{:error, reason}` rather than raising;
  the HTTP router maps `{:error, reason}` to 422 schema_invalid with the reason
  surfaced in the body.

  ## Cross-language parity notes

  - `?` rejection is at the TOP level (before per-scheme dispatch) per RFC
    §7.12.4 OPERATOR_NOTE 3 + Python `_validate_surface_id`. Keeps v0 traffic
    clean while leaving v1 modifier-form Open.
  - The `resident:/` reserved-character set is exactly Python's three
    whitespace chars + `#` + `&` (`?` already caught at top level). Do NOT
    use `\\s` here — `\\s` in PCRE matches `\\r`/`\\f`/`\\v` plus Unicode
    whitespace, which would over-reject relative to Python.
  - `byte_size/1` is byte-count (Elixir convention); Python `len/1` is
    code-point count. For the canonical-scheme grammar (ASCII-only by RFC),
    these match. A multi-byte UTF-8 surface_id would diverge but is rejected
    by the DB grammar CHECK independently.

  ## file:// — full normalization (PR 7.5)

  ### Single-FS assumption

  The case-insensitivity probe runs once in `System.tmp_dir!()` and the result
  is cached for the lifetime of the BEAM. This matches Python's
  `tempfile.gettempdir()` probe — a parity-preserving design choice. Deployments
  that span FS case-fold boundaries (e.g., case-insensitive APFS for `/tmp` +
  case-sensitive ext4 mounted under `/data`) would see one consistent
  canonicalization across both, which can be wrong for one of them. The current
  fleet runs entirely on case-insensitive APFS so this isn't observed; if it
  ever changes, see PR 7.5 council CONCERN C2.

  Mirrors Python `_canonicalize_file`:

  1. Shell out to OS `realpath` (strict mode — fails if path doesn't exist).
     macOS BSD realpath is strict by default; GNU coreutils realpath needs
     the `-e` flag. Compile-time OS detection picks the right invocation.
  2. Double-apply realpath to handle macOS `/var` → `/private/var`
     idempotency edge (DRIFT-2 in Python).
  3. Strip trailing `/` except for root (`/`).
  4. Tmpfile probe to detect case-insensitive filesystem (DRIFT-3 in
     Python — `pathconf(_PC_CASE_SENSITIVE)` was REFUTED on macOS, so the
     probe is the only reliable test). Result cached via
     `:persistent_term` for once-per-process semantics.
  5. Lowercase the resolved path if filesystem is case-insensitive.
  6. Re-prefix with `file://`.

  ENOENT (path doesn't exist) is handled per Python: fall through to a
  best-effort canonicalization of the path as-given (apply trailing-slash
  strip + case-fold), since the lease plane does not validate file
  existence (RFC §7.12.2).

  ## capture:/ — comma is the member separator

  `capture:/` member names MUST NOT contain commas; the splitter has no escape
  mechanism. A comma-bearing member would silently split into multiple members
  and sort. Both Python and Elixir corrupt identically here; not a parity gap.
  If RFC ever needs comma-bearing members, both implementations need a coupled
  escape rule.
  """

  require Logger

  # v0.8 canonical scheme list (RFC §7.2.1) plus follow-on schemes. Single
  # source of truth in Elixir.
  @canonical_schemes ~w(file dialectic resident maintenance capture td agent)

  @path_max 4096

  @typedoc """
  Reasons returned by `canonicalize/1` on failure. Mirrors Python
  `CanonicalizeError.reason` plus Python's top-level `?`-rejection in
  `_validate_surface_id`.
  """
  @type reason ::
          :nul_byte
          | :path_too_long
          | :invalid_scheme
          | :reserved_query_string
          | :symlink_loop

  @doc """
  Returns `{:ok, canonical}` if `surface_id` matches the canonical scheme list
  and per-scheme normalization succeeds; `{:error, reason}` otherwise.

  ## Examples

      iex> UnitaresLeasePlane.Canonicalize.canonicalize("dialectic:/SESSION-Abc")
      {:ok, "dialectic:/session-abc"}

      iex> UnitaresLeasePlane.Canonicalize.canonicalize("capture:/B,A,C")
      {:ok, "capture:/A,B,C"}

      iex> UnitaresLeasePlane.Canonicalize.canonicalize("resident:/watcher_cycle/")
      {:ok, "resident:/watcher_cycle"}

      iex> UnitaresLeasePlane.Canonicalize.canonicalize("maintenance:/worktree_reaper/")
      {:ok, "maintenance:/worktree_reaper"}

      iex> UnitaresLeasePlane.Canonicalize.canonicalize("td:/eisv_basin_v31")
      {:ok, "td:/eisv_basin_v31"}

      iex> UnitaresLeasePlane.Canonicalize.canonicalize("ftp://nope")
      {:error, :invalid_scheme}
  """
  @spec canonicalize(String.t()) :: {:ok, String.t()} | {:error, reason()}
  def canonicalize(surface_id) when is_binary(surface_id) do
    cond do
      String.contains?(surface_id, <<0>>) ->
        {:error, :nul_byte}

      byte_size(surface_id) > @path_max ->
        {:error, :path_too_long}

      # PR 7 council BLOCK B1 — Python `_validate_surface_id` rejects `?`
      # bearing surface_ids at the top level (before per-scheme dispatch) per
      # RFC §7.12.4 OPERATOR_NOTE 3. Mirror exactly here so non-Python
      # callers can't slip `dialectic:/abc?x=1` past the Elixir router.
      String.contains?(surface_id, "?") ->
        {:error, :reserved_query_string}

      true ->
        dispatch(surface_id)
    end
  end

  def canonicalize(_), do: {:error, :invalid_scheme}

  defp dispatch("file://" <> rest), do: canonicalize_file(rest)
  defp dispatch("dialectic:/" <> rest), do: canonicalize_dialectic(rest)
  defp dispatch("resident:/" <> rest), do: canonicalize_resident(rest)
  defp dispatch("maintenance:/" <> rest), do: canonicalize_maintenance(rest)
  defp dispatch("capture:/" <> rest), do: canonicalize_capture(rest)
  defp dispatch("td:/" <> rest), do: canonicalize_td(rest)
  defp dispatch("agent:/" <> rest), do: canonicalize_agent(rest)
  defp dispatch(_), do: {:error, :invalid_scheme}

  @doc """
  Returns the canonical scheme list. Useful for error messages and tests.
  """
  @spec canonical_schemes() :: [String.t()]
  def canonical_schemes, do: @canonical_schemes

  # ---------- per-scheme rules ----------

  # file:// canonicalization (PR 7.5). See moduledoc for the rule list.
  #
  # Cross-platform realpath invocation: macOS BSD realpath is strict by
  # default (errors on ENOENT); GNU coreutils realpath needs `-e` to match.
  # Detect at compile time and lock the args.
  #
  # Compile-time vs runtime: today the lease plane is Mac-only. If the build
  # host ever differs from the deploy host, this needs to switch to runtime
  # detection. Per PR 7.5 council CONCERN C1.
  @realpath_args (case :os.type() do
                    {:unix, :darwin} -> []
                    _ -> ["-e"]
                  end)

  # Resolve the absolute path to the realpath binary at compile time so a
  # LaunchAgent with a sparse PATH (which doesn't include /bin) doesn't get
  # silent :other → :invalid_scheme errors on every file:// acquire.
  # Per PR 7.5 council BLOCK 1 (architect).
  @realpath_bin (case System.find_executable("realpath") do
                   nil ->
                     raise "realpath binary not found on PATH at compile time"

                   path ->
                     path
                 end)

  defp canonicalize_file(path) do
    case resolve_realpath(path) do
      {:ok, resolved} ->
        # Double-realpath: catches macOS /var → /private/var idempotency edge
        # (DRIFT-2 in Python). On Linux this is a no-op for already-resolved
        # paths but harmless.
        case resolve_realpath(resolved) do
          {:ok, double_resolved} ->
            {:ok, "file://" <> finalize_file(double_resolved)}

          # If the double call fails (race: file deleted between calls),
          # fall back to the single-resolved value rather than erroring.
          {:error, _} ->
            {:ok, "file://" <> finalize_file(resolved)}
        end

      {:error, :enoent} ->
        # Per RFC §7.12.2 + Python semantics: the lease plane does not validate
        # existence, but it MUST resolve symlinks in existing path prefixes
        # so a missing-file path under /var (macOS) canonicalizes to
        # /private/var/.../missing — matches Python's `os.path.realpath`
        # non-strict behavior. Per PR 7.5 council BLOCK 2.
        resolved = nonstrict_realpath(path)
        {:ok, "file://" <> finalize_file(resolved)}

      {:error, :symlink_loop} ->
        {:error, :symlink_loop}

      {:error, :enametoolong} ->
        {:error, :path_too_long}

      {:error, {:other, msg}} ->
        # Permission-denied, ENOTDIR, missing realpath binary, etc. — surface
        # as invalid_scheme so the caller sees a typed-absence error class
        # consistent with Python. Log the original stderr so operators can
        # diagnose what was swallowed (PR 7.5 council CONCERN C3).
        Logger.warning(
          "lease_plane canonicalize file://: realpath returned :other; mapping to :invalid_scheme. stderr=#{inspect(msg)}"
        )

        {:error, :invalid_scheme}
    end
  end

  # Non-strict realpath: walks back from the user-supplied path until it finds
  # an existing prefix, runs strict realpath on that prefix, then re-attaches
  # the missing tail. Mirrors Python's `os.path.realpath(path)` (non-strict)
  # behavior in the ENOENT branch. Pure Elixir — no second shell-out class.
  defp nonstrict_realpath(path) do
    {existing, missing} = split_at_existing(path)

    case resolve_realpath(existing) do
      {:ok, resolved_existing} when missing == "" ->
        resolved_existing

      {:ok, resolved_existing} ->
        Path.join(resolved_existing, missing)

      # Even / failed to realpath — give up and return raw path.
      {:error, _} ->
        path
    end
  end

  defp split_at_existing("/"), do: {"/", ""}

  defp split_at_existing(path) do
    if File.exists?(path) do
      {path, ""}
    else
      parent = Path.dirname(path)
      base = Path.basename(path)
      {existing, missing} = split_at_existing(parent)
      next_missing = if missing == "", do: base, else: Path.join(missing, base)
      {existing, next_missing}
    end
  end

  # Strip trailing / except for root, then case-fold if FS is case-insensitive.
  defp finalize_file(path) do
    stripped =
      cond do
        path == "/" -> "/"
        String.ends_with?(path, "/") -> String.trim_trailing(path, "/")
        true -> path
      end

    if case_insensitive_fs?(), do: String.downcase(stripped), else: stripped
  end

  # Shell-out wrapper. Returns {:ok, resolved_path} or {:error, reason} where
  # reason is one of :enoent, :symlink_loop, :enametoolong, {:other, msg}.
  # Stderr is captured (stderr_to_stdout: true) and pattern-matched to map the
  # OS error message to a stable atom.
  #
  # Two adversarial-input mitigations baked in (PR 7.5 council BLOCK 1):
  # - LC_ALL=C in env: forces English error messages so substring-matching
  #   doesn't break under non-English deploy locales.
  # - Leading-`-` guard: GNU realpath accepts `-s` / `--relative-to=DIR` /
  #   `-m` flags which would silently change canonicalization semantics. A
  #   surface_id like `file://-s/tmp/foo` would pass `-s` to GNU realpath.
  #   Prepend `./` to neutralize without changing the resolved path semantics
  #   (the leading `./` collapses in realpath output).
  defp resolve_realpath(path) do
    safe_path = if String.starts_with?(path, "-"), do: "./" <> path, else: path

    case System.cmd(@realpath_bin, @realpath_args ++ [safe_path],
           stderr_to_stdout: true,
           env: [{"LC_ALL", "C"}]
         ) do
      {output, 0} ->
        {:ok, String.trim_trailing(output, "\n")}

      {output, _nonzero} ->
        msg = String.downcase(output)

        cond do
          String.contains?(msg, "too many levels of symbolic links") ->
            {:error, :symlink_loop}

          String.contains?(msg, "no such file") ->
            {:error, :enoent}

          String.contains?(msg, "file name too long") ->
            {:error, :enametoolong}

          true ->
            {:error, {:other, output}}
        end
    end
  end

  # Tmpfile probe — write "PROBE", stat "probe", same filesystem entry iff
  # case-insensitive. Cached via :persistent_term for once-per-VM resolution.
  # Per-process probes would be wasteful (the FS doesn't change underneath us).
  defp case_insensitive_fs? do
    case :persistent_term.get({__MODULE__, :case_insensitive}, :uncached) do
      :uncached ->
        result = detect_case_insensitive_fs()
        :persistent_term.put({__MODULE__, :case_insensitive}, result)
        result

      cached ->
        cached
    end
  end

  defp detect_case_insensitive_fs do
    rand = :crypto.strong_rand_bytes(6) |> Base.encode16(case: :lower)
    probe_dir = Path.join(System.tmp_dir!(), "lease_canonicalize_probe_#{rand}")
    File.mkdir_p!(probe_dir)

    try do
      upper = Path.join(probe_dir, "PROBE")
      lower = Path.join(probe_dir, "probe")
      File.write!(upper, "")
      File.exists?(lower)
    after
      File.rm_rf!(probe_dir)
    end
  end

  # dialectic:/ — opaque session id; lowercase only (matches Python).
  defp canonicalize_dialectic(path) do
    {:ok, "dialectic:/" <> String.downcase(path)}
  end

  # resident:/ — opaque resident name; case-sensitive; reject reserved chars.
  # Per PR 7 council BLOCK 1 (reviewer): mirror Python's exact set — space,
  # tab, newline, `#`, `&`. `?` is now caught at the top level. Do NOT use
  # `\s` here — PCRE `\s` includes `\r`/`\f`/`\v` plus Unicode whitespace,
  # which would over-reject relative to Python.
  defp canonicalize_resident(path) do
    if String.match?(path, ~r/[ \t\n#&]/) do
      {:error, :invalid_scheme}
    else
      {:ok, "resident:/" <> String.trim_trailing(path, "/")}
    end
  end

  # maintenance:/ — opaque cleanup/repair coordination surface; case-sensitive,
  # strip trailing /. Same reserved-char set as resident:/, but deliberately not
  # a resident lifecycle/presence surface. See migration 050.
  defp canonicalize_maintenance(path) do
    if String.match?(path, ~r/[ \t\n#&]/) do
      {:error, :invalid_scheme}
    else
      {:ok, "maintenance:/" <> String.trim_trailing(path, "/")}
    end
  end

  # capture:/ — comma-separated member list; sort lexically. Drops empty
  # members (blank between commas, or leading/trailing comma) before sorting,
  # matching Python's `[m.strip() for m in path.split(",") if m.strip()]`.
  defp canonicalize_capture(path) do
    members =
      path
      |> String.split(",")
      |> Enum.map(&String.trim/1)
      |> Enum.reject(&(&1 == ""))
      |> Enum.sort()

    {:ok, "capture:/" <> Enum.join(members, ",")}
  end

  # td:/ — reserved scheme; pass-through with no normalization (matches Python).
  defp canonicalize_td(path) do
    {:ok, "td:/" <> path}
  end

  # agent:/ — opaque ephemeral-agent id (url-safe base64-ish); case-sensitive,
  # strip trailing /. Same reserved-char set as resident:/. This is a PRESENCE
  # surface (unique per agent, routed to remote_heartbeat in the router), not a
  # mutex — see migration 042 and http_router.acquire_for_surface.
  defp canonicalize_agent(path) do
    if String.match?(path, ~r/[ \t\n#&]/) do
      {:error, :invalid_scheme}
    else
      {:ok, "agent:/" <> String.trim_trailing(path, "/")}
    end
  end
end
