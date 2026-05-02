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

  ## file:// — deferred normalization

  This Phase A pass implements the four pure-logic schemes (`dialectic`,
  `resident`, `capture`, `td`) that have deterministic cross-language behavior.
  `file://` requires double-realpath (macOS /var → /private/var) and tmpfile
  case-detection probing, both of which are filesystem-state-dependent and need
  a more involved cross-language parity harness. PR 2.5 migrated all production
  agents (watcher, vigil, sentinel, chronicler, ship.sh) off `file://` to
  `resident:/`, so the deferral does not block production. Tracking note in
  `docs/proposals/surface-lease-plane-phase-a-plan.md` PR 8+ section.

  For `file://` in this PR: validate prefix + NUL + length only. Pass through
  the path component unchanged. The DB grammar CHECK from migration 026 still
  enforces the scheme prefix. A `Logger.warning` fires on every `file://`
  ingress so the deferral leaves an audit trail when re-violated by a future
  caller.

  ## capture:/ — comma is the member separator

  `capture:/` member names MUST NOT contain commas; the splitter has no escape
  mechanism. A comma-bearing member would silently split into multiple members
  and sort. Both Python and Elixir corrupt identically here; not a parity gap.
  If RFC ever needs comma-bearing members, both implementations need a coupled
  escape rule.
  """

  require Logger

  # v0.8 canonical scheme list (RFC §7.2.1). Single source of truth in Elixir.
  @canonical_schemes ~w(file dialectic resident capture td)

  @path_max 4096

  @typedoc """
  Reasons returned by `canonicalize/1` on failure. Mirrors Python
  `CanonicalizeError.reason` plus Python's top-level `?`-rejection in
  `_validate_surface_id`.
  """
  @type reason :: :nul_byte | :path_too_long | :invalid_scheme | :reserved_query_string

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
  defp dispatch("capture:/" <> rest), do: canonicalize_capture(rest)
  defp dispatch("td:/" <> rest), do: canonicalize_td(rest)
  defp dispatch(_), do: {:error, :invalid_scheme}

  @doc """
  Returns the canonical scheme list. Useful for error messages and tests.
  """
  @spec canonical_schemes() :: [String.t()]
  def canonical_schemes, do: @canonical_schemes

  # ---------- per-scheme rules ----------

  # file:// canonicalization deferred to a follow-up PR. See moduledoc.
  # PR 2.5 moved all production agents off file://; the DB grammar CHECK still
  # enforces the prefix. Pass-through preserves the path component verbatim.
  # Logger.warning fires on every ingress so the deferral leaves an audit trail
  # when re-violated by a future caller (per PR 7 council CONCERN C4).
  defp canonicalize_file(path) do
    Logger.warning(
      "lease_plane canonicalize: file:// surface_id received without normalization (PR 7 deferred); path=#{inspect(path)}"
    )

    {:ok, "file://" <> path}
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
end
