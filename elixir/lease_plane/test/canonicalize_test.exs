defmodule UnitaresLeasePlane.CanonicalizeTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.Canonicalize

  # ---------- scheme dispatch + invalid schemes ----------

  describe "scheme dispatch" do
    test "rejects unknown scheme" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("ftp://example.com/path")
    end

    test "rejects bare path with no scheme" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("/just/a/path")
    end

    test "rejects empty string" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("")
    end

    test "rejects non-string input" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize(nil)
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize(:atom)
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize(123)
    end

    test "canonical_schemes/0 returns the canonical list" do
      assert Canonicalize.canonical_schemes() ==
               ~w(file dialectic resident maintenance capture td agent)
    end
  end

  # ---------- NUL byte + length guards ----------

  describe "input guards" do
    test "rejects NUL byte" do
      assert {:error, :nul_byte} = Canonicalize.canonicalize("dialectic:/with\0null")
    end

    test "rejects ? at top level across ALL schemes (parity with Python _validate_surface_id)" do
      # PR 7 council BLOCK B1 (architect): Python rejects `?` BEFORE per-scheme
      # dispatch per RFC §7.12.4 OPERATOR_NOTE 3. Elixir must too — otherwise
      # `dialectic:/abc?x=1` flows through Elixir but gets rejected by Python,
      # which is the exact split-brain class this module exists to prevent.
      assert {:error, :reserved_query_string} =
               Canonicalize.canonicalize("dialectic:/abc?x=1")

      assert {:error, :reserved_query_string} =
               Canonicalize.canonicalize("resident:/q?x=1")

      assert {:error, :reserved_query_string} =
               Canonicalize.canonicalize("maintenance:/q?x=1")

      assert {:error, :reserved_query_string} =
               Canonicalize.canonicalize("capture:/A?,B")

      assert {:error, :reserved_query_string} =
               Canonicalize.canonicalize("td:/x?y")

      assert {:error, :reserved_query_string} =
               Canonicalize.canonicalize("file:///foo?bar")
    end

    test "rejects path longer than PATH_MAX (4096)" do
      long = String.duplicate("x", 4090)
      input = "resident:/" <> long
      assert byte_size(input) > 4096
      assert {:error, :path_too_long} = Canonicalize.canonicalize(input)
    end

    test "accepts path exactly at PATH_MAX" do
      # "resident:/" is 10 bytes; pad to exactly 4096.
      payload = String.duplicate("x", 4096 - 10)
      input = "resident:/" <> payload
      assert byte_size(input) == 4096
      assert {:ok, _} = Canonicalize.canonicalize(input)
    end
  end

  # ---------- dialectic:/ ----------

  describe "dialectic:/" do
    test "lowercases the path" do
      assert {:ok, "dialectic:/session-abc"} =
               Canonicalize.canonicalize("dialectic:/SESSION-Abc")
    end

    test "preserves an already-lowercase id" do
      assert {:ok, "dialectic:/session-xyz-123"} =
               Canonicalize.canonicalize("dialectic:/session-xyz-123")
    end

    test "is idempotent" do
      input = "dialectic:/MIXED-Case-789"
      {:ok, once} = Canonicalize.canonicalize(input)
      {:ok, twice} = Canonicalize.canonicalize(once)
      assert once == twice
    end
  end

  # ---------- resident:/ ----------

  describe "resident:/" do
    test "preserves case (case-sensitive)" do
      assert {:ok, "resident:/Watcher_Cycle"} =
               Canonicalize.canonicalize("resident:/Watcher_Cycle")
    end

    test "strips trailing slash" do
      assert {:ok, "resident:/watcher_cycle"} =
               Canonicalize.canonicalize("resident:/watcher_cycle/")
    end

    test "rejects whitespace in path" do
      assert {:error, :invalid_scheme} =
               Canonicalize.canonicalize("resident:/with space")

      assert {:error, :invalid_scheme} =
               Canonicalize.canonicalize("resident:/with\ttab")

      assert {:error, :invalid_scheme} =
               Canonicalize.canonicalize("resident:/with\nnewline")
    end

    test "rejects # and & (URL reserved chars; ? is caught at top level)" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("resident:/h#frag")
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("resident:/a&b")
    end

    test "does NOT reject \\r/\\f/\\v (parity: only space/tab/newline are reserved per Python)" do
      # PR 7 council BLOCK 1 (reviewer): Elixir's prior `~r/[\\s?#&]/u` rejected
      # \\r/\\f/\\v plus Unicode whitespace, but Python only rejects (' ', '\\t',
      # '\\n'). Lock in the parity so future regex edits don't silently
      # over-reject.
      assert {:ok, _} = Canonicalize.canonicalize("resident:/path\rwith_cr")
      assert {:ok, _} = Canonicalize.canonicalize("resident:/path\fwith_ff")
      assert {:ok, _} = Canonicalize.canonicalize("resident:/path\vwith_vt")
    end

    test "is idempotent" do
      {:ok, once} = Canonicalize.canonicalize("resident:/watcher_scan_commits_repo/")
      {:ok, twice} = Canonicalize.canonicalize(once)
      assert once == twice
    end
  end

  # ---------- maintenance:/ ----------

  describe "maintenance:/" do
    test "preserves case (case-sensitive)" do
      assert {:ok, "maintenance:/Cleanup_Job"} =
               Canonicalize.canonicalize("maintenance:/Cleanup_Job")
    end

    test "strips trailing slash" do
      assert {:ok, "maintenance:/worktree_reaper"} =
               Canonicalize.canonicalize("maintenance:/worktree_reaper/")
    end

    test "rejects whitespace in path" do
      assert {:error, :invalid_scheme} =
               Canonicalize.canonicalize("maintenance:/with space")

      assert {:error, :invalid_scheme} =
               Canonicalize.canonicalize("maintenance:/with\ttab")

      assert {:error, :invalid_scheme} =
               Canonicalize.canonicalize("maintenance:/with\nnewline")
    end

    test "rejects # and & (URL reserved chars; ? is caught at top level)" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("maintenance:/h#frag")
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("maintenance:/a&b")
    end

    test "does NOT reject \\r/\\f/\\v (parity with resident:/)" do
      assert {:ok, _} = Canonicalize.canonicalize("maintenance:/path\rwith_cr")
      assert {:ok, _} = Canonicalize.canonicalize("maintenance:/path\fwith_ff")
      assert {:ok, _} = Canonicalize.canonicalize("maintenance:/path\vwith_vt")
    end

    test "is idempotent" do
      {:ok, once} = Canonicalize.canonicalize("maintenance:/vigil_hygiene_sweep/")
      {:ok, twice} = Canonicalize.canonicalize(once)
      assert once == twice
    end
  end

  # ---------- capture:/ ----------

  describe "capture:/" do
    test "sorts member list lexically" do
      assert {:ok, "capture:/A,B,C"} = Canonicalize.canonicalize("capture:/B,A,C")
    end

    test "preserves an already-sorted list" do
      assert {:ok, "capture:/A,B,C"} = Canonicalize.canonicalize("capture:/A,B,C")
    end

    test "trims whitespace around members" do
      assert {:ok, "capture:/A,B,C"} = Canonicalize.canonicalize("capture:/ B , A , C ")
    end

    test "drops empty members from leading/trailing/double commas" do
      assert {:ok, "capture:/A,B"} = Canonicalize.canonicalize("capture:/,A,,B,")
    end

    test "single member is preserved" do
      assert {:ok, "capture:/only"} = Canonicalize.canonicalize("capture:/only")
    end

    test "is idempotent" do
      input = "capture:/zeta,alpha,gamma"
      {:ok, once} = Canonicalize.canonicalize(input)
      {:ok, twice} = Canonicalize.canonicalize(once)
      assert once == twice
      assert once == "capture:/alpha,gamma,zeta"
    end
  end

  # ---------- td:/ ----------

  describe "td:/" do
    test "passes through unchanged (reserved scheme)" do
      assert {:ok, "td:/eisv_basin_v31"} =
               Canonicalize.canonicalize("td:/eisv_basin_v31")
    end

    test "preserves nested path components" do
      assert {:ok, "td:/project/network/v1"} =
               Canonicalize.canonicalize("td:/project/network/v1")
    end

    test "td:/ with empty path passes through (parity with Python)" do
      # PR 7 council CONCERN C5 (architect): pin the empty-path behavior so
      # neither implementation drifts. RFC v0.8 doesn't require validation
      # beyond the prefix; both languages pass through.
      assert {:ok, "td:/"} = Canonicalize.canonicalize("td:/")
    end
  end

  # ---------- file:// (full normalization, PR 7.5) ----------

  describe "agent:/ (ephemeral-agent presence, migration 042)" do
    test "preserves case and is a valid scheme" do
      assert {:ok, "agent:/ag-7SDzA2Tm"} = Canonicalize.canonicalize("agent:/ag-7SDzA2Tm")
    end

    test "strips trailing slash" do
      assert {:ok, "agent:/ag-abc"} = Canonicalize.canonicalize("agent:/ag-abc/")
    end

    test "accepts url-safe base64 ids (- and _)" do
      assert {:ok, "agent:/FQSzK8iT_-x"} = Canonicalize.canonicalize("agent:/FQSzK8iT_-x")
    end

    test "rejects whitespace, # and & (parity with resident:/)" do
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("agent:/with space")
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("agent:/h#frag")
      assert {:error, :invalid_scheme} = Canonicalize.canonicalize("agent:/a&b")
    end

    test "rejects ? at top level (parity)" do
      assert {:error, :reserved_query_string} = Canonicalize.canonicalize("agent:/q?x=1")
    end

    test "is included in the canonical scheme list" do
      assert "agent" in Canonicalize.canonical_schemes()
    end
  end

  describe "file:// (PR 7.5 full normalization)" do
    # Most file:// tests touch real filesystem state. async: false at the
    # describe level isn't supported, but ExUnit's per-test setup gives each
    # test its own isolated tmp dir so they can still run async at the file
    # level — the FS state is local to each test's randomly-named root.
    setup do
      rand = :crypto.strong_rand_bytes(6) |> Base.encode16(case: :lower)
      root = Path.join(System.tmp_dir!(), "lp_canon_test_#{rand}")
      File.mkdir_p!(root)
      on_exit(fn -> File.rm_rf!(root) end)
      {:ok, root: root}
    end

    test "still rejects NUL byte" do
      assert {:error, :nul_byte} =
               Canonicalize.canonicalize("file:///has\0null")
    end

    test "resolves an existing file via realpath", ctx do
      file = Path.join(ctx.root, "real_file.txt")
      File.write!(file, "")

      assert {:ok, canonical} = Canonicalize.canonicalize("file://" <> file)
      assert String.starts_with?(canonical, "file://")
      # On macOS APFS (case-insensitive default) the canonical form is
      # lowercased; on a case-sensitive FS it isn't. The realpath result must
      # be the same case-folded version of the input either way.
      assert canonical == "file://" <> case_fold(file)
    end

    test "macOS /var resolves to /private/var (DRIFT-2 idempotency)" do
      # Skip on non-macOS (Linux /var is the real path).
      if :os.type() == {:unix, :darwin} do
        # /var/folders is the standard macOS user temp ancestor. Use a path we
        # know exists across all macOS installs.
        assert {:ok, "file:///private/var" <> _} = Canonicalize.canonicalize("file:///var")
      end
    end

    test "follows symlinks to the resolved target", ctx do
      target = Path.join(ctx.root, "target.txt")
      File.write!(target, "")
      link = Path.join(ctx.root, "link.txt")
      File.ln_s!(target, link)

      assert {:ok, canonical} = Canonicalize.canonicalize("file://" <> link)
      assert canonical == "file://" <> case_fold(target)
    end

    test "ELOOP (symlink cycle) → :symlink_loop", ctx do
      a = Path.join(ctx.root, "a")
      b = Path.join(ctx.root, "b")
      File.ln_s!(a, b)
      File.ln_s!(b, a)

      assert {:error, :symlink_loop} = Canonicalize.canonicalize("file://" <> a)
    end

    test "ENOENT → resolves intermediate symlinks, appends missing tail (PR 7.5 BLOCK 2)", ctx do
      # Per PR 7.5 council BLOCK 2: the ENOENT branch must resolve symlinks in
      # existing path prefixes (mirrors Python's non-strict os.path.realpath).
      # ctx.root is an existing dir (realpath-able); the "Does/Not/Exist" tail
      # is appended verbatim (case-folded if APFS).
      missing = Path.join(ctx.root, "Does/Not/Exist")
      assert {:ok, "file://" <> rest} = Canonicalize.canonicalize("file://" <> missing)

      {realpath_root, 0} = System.cmd("realpath", [ctx.root], stderr_to_stdout: true)
      resolved_root = String.trim_trailing(realpath_root, "\n")
      expected = Path.join(resolved_root, "Does/Not/Exist")
      expected = if case_insensitive_probe(), do: String.downcase(expected), else: expected

      assert rest == expected
    end

    test "trailing / stripped except for root", ctx do
      file = Path.join(ctx.root, "tail")
      File.write!(file, "")

      # Trailing slash on an existing file: realpath rejects it on most OSes
      # (file is not a directory). Test the directory case which is more useful
      # operationally.
      dir = Path.join(ctx.root, "subdir")
      File.mkdir_p!(dir)

      assert {:ok, canonical_no_slash} = Canonicalize.canonicalize("file://" <> dir)
      assert {:ok, canonical_with_slash} = Canonicalize.canonicalize("file://" <> dir <> "/")

      assert canonical_no_slash == canonical_with_slash
      refute String.ends_with?(canonical_no_slash, "/")
    end

    test "root / is preserved (not stripped)" do
      assert {:ok, "file:///"} = Canonicalize.canonicalize("file:///")
    end

    test "is idempotent — canonicalize(canonicalize(x)) == canonicalize(x)", ctx do
      file = Path.join(ctx.root, "Idempotent.txt")
      File.write!(file, "")

      input = "file://" <> file
      {:ok, once} = Canonicalize.canonicalize(input)
      {:ok, twice} = Canonicalize.canonicalize(once)
      assert once == twice
    end

    test "PR 7.5 BLOCK 1 — leading-`-` path does not get parsed as realpath flag", ctx do
      # Council BLOCK 1 (reviewer): GNU realpath has flags `-s`, `-m`,
      # `--relative-to=DIR` that would silently change canonicalization
      # semantics if a surface_id like `file://-s/path` slipped through.
      # The `./` prefix guard in resolve_realpath/1 neutralizes this.
      # Build a dash-prefixed REAL file under ctx.root so the test exercises
      # the actual realpath path, not the ENOENT fall-through.
      dash_file = Path.join(ctx.root, "-not-a-flag.txt")
      File.write!(dash_file, "")

      assert {:ok, canonical} = Canonicalize.canonicalize("file://" <> dash_file)
      # The canonical form must contain the literal "-not-a-flag.txt" filename
      # (case-folded if APFS), proving realpath treated it as a path argument.
      assert String.contains?(canonical, case_fold("-not-a-flag.txt"))
    end

    test "PR 7.5 BLOCK 2 — ENOENT under /var (macOS) resolves intermediate /var symlink" do
      # Council BLOCK 2: Python's ENOENT branch calls os.path.realpath(path)
      # non-strict, which still resolves intermediate symlinks. Elixir must
      # too — otherwise file:///var/missing/foo on macOS canonicalizes to
      # `/var/missing/foo` while Python gives `/private/var/missing/foo`.
      if :os.type() == {:unix, :darwin} do
        rand = :crypto.strong_rand_bytes(4) |> Base.encode16(case: :lower)
        # /var/folders/... exists; append a definitely-missing tail.
        missing = "/var/folders/missing_pr75_test_#{rand}/no_such_file"

        assert {:ok, canonical} = Canonicalize.canonicalize("file://" <> missing)
        # Existing /var prefix must be resolved to /private/var; missing
        # tail is appended verbatim (case-folded if APFS).
        assert String.starts_with?(canonical, "file:///private/var/folders/")
        assert String.contains?(canonical, "missing_pr75_test_#{rand}")
      end
    end

    test "case-fold matches the live FS detection", ctx do
      # Whatever the runtime FS detection says, the canonical output must be
      # consistent with it for ASCII paths. Compare against case_fold which
      # applies realpath + same case-detection logic via a different code
      # path (so this isn't tautological with the module under test).
      file = Path.join(ctx.root, "MixedCase.TXT")
      File.write!(file, "")

      assert {:ok, canonical} = Canonicalize.canonicalize("file://" <> file)
      assert canonical == "file://" <> case_fold(file)
    end
  end

  # Test helper: produce the expected canonical output for a given input
  # path. Mirrors what the module does (realpath + case-fold) but via
  # a slightly different code path so assertions aren't tautological.
  # If the file exists, realpath resolves macOS /var → /private/var etc.
  # If it doesn't, fall through to the path as-given.
  defp case_fold(path) do
    realpath_args =
      case :os.type() do
        {:unix, :darwin} -> []
        _ -> ["-e"]
      end

    resolved =
      case System.cmd("realpath", realpath_args ++ [path], stderr_to_stdout: true) do
        {output, 0} -> String.trim_trailing(output, "\n")
        {_output, _nonzero} -> path
      end

    if case_insensitive_probe(), do: String.downcase(resolved), else: resolved
  end

  defp case_insensitive_probe do
    rand = :crypto.strong_rand_bytes(4) |> Base.encode16(case: :lower)
    probe_dir = Path.join(System.tmp_dir!(), "lp_case_probe_#{rand}")
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

  # ---------- cross-language parity (Python ↔ Elixir) ----------

  describe "cross-language parity with Python canonicalize.py" do
    # These pairs MUST produce the same output as the Python helper at
    # src/lease_plane/canonicalize.py for the implemented schemes.
    # Lock this in so future drift between the two implementations is caught
    # by `mix test` in CI rather than at production split-brain time.
    parity_cases = [
      {"dialectic:/Session-ABC", "dialectic:/session-abc"},
      {"dialectic:/already-lower", "dialectic:/already-lower"},
      {"resident:/Watcher_Cycle/", "resident:/Watcher_Cycle"},
      {"resident:/sentinel_cycle", "resident:/sentinel_cycle"},
      {"maintenance:/worktree_reaper/", "maintenance:/worktree_reaper"},
      {"maintenance:/Vigil_Hygiene", "maintenance:/Vigil_Hygiene"},
      {"capture:/B,A,C", "capture:/A,B,C"},
      {"capture:/ x , y , z ", "capture:/x,y,z"},
      {"capture:/,A,,B,", "capture:/A,B"},
      {"td:/eisv_basin_v31", "td:/eisv_basin_v31"},
      {"agent:/ag-abc/", "agent:/ag-abc"}
    ]

    for {input, expected} <- parity_cases do
      test "matches Python output for #{inspect(input)}" do
        assert {:ok, unquote(expected)} = Canonicalize.canonicalize(unquote(input))
      end
    end
  end
end
