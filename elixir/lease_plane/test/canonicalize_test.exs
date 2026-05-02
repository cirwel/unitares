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

    test "canonical_schemes/0 returns the v0.8 list" do
      assert Canonicalize.canonical_schemes() == ~w(file dialectic resident capture td)
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

  # ---------- file:// (deferred normalization) ----------

  describe "file:// (deferred normalization)" do
    test "accepts prefix without realpath/case normalization" do
      # Per moduledoc: file:// canonicalization is deferred to a follow-up PR.
      # This pass validates the prefix and pass-through; the DB grammar CHECK
      # still enforces the scheme.
      assert {:ok, "file:///Users/X/foo"} =
               Canonicalize.canonicalize("file:///Users/X/foo")
    end

    test "passes through case + trailing slash unchanged for now" do
      # When file:// canonicalization lands, this case will need revisiting:
      # macOS APFS is case-insensitive by default and would lowercase, plus
      # strip trailing /.
      assert {:ok, "file:///Users/X/foo/"} =
               Canonicalize.canonicalize("file:///Users/X/foo/")
    end

    test "still rejects NUL byte even in file://" do
      assert {:error, :nul_byte} =
               Canonicalize.canonicalize("file:///has\0null")
    end
  end

  # ---------- cross-language parity (Python ↔ Elixir) ----------

  describe "cross-language parity with Python canonicalize.py" do
    # These pairs MUST produce the same output as the Python helper at
    # src/lease_plane/canonicalize.py for the four implemented schemes.
    # Lock this in so future drift between the two implementations is caught
    # by `mix test` in CI rather than at production split-brain time.
    parity_cases = [
      {"dialectic:/Session-ABC", "dialectic:/session-abc"},
      {"dialectic:/already-lower", "dialectic:/already-lower"},
      {"resident:/Watcher_Cycle/", "resident:/Watcher_Cycle"},
      {"resident:/sentinel_cycle", "resident:/sentinel_cycle"},
      {"capture:/B,A,C", "capture:/A,B,C"},
      {"capture:/ x , y , z ", "capture:/x,y,z"},
      {"capture:/,A,,B,", "capture:/A,B"},
      {"td:/eisv_basin_v31", "td:/eisv_basin_v31"}
    ]

    for {input, expected} <- parity_cases do
      test "matches Python output for #{inspect(input)}" do
        assert {:ok, unquote(expected)} = Canonicalize.canonicalize(unquote(input))
      end
    end
  end
end
