defmodule UnitaresLeasePlane.CanonicalizeProperty do
  @moduledoc """
  Property-based tests for `UnitaresLeasePlane.Canonicalize`.

  The example-based tests in `canonicalize_test.exs` pin specific inputs the
  RFC names. These properties explore the input space the example tests
  miss — random scheme paths, random inputs that look canonical-ish, and
  ensure the invariants the module is supposed to uphold actually hold under
  randomization.

  Properties pinned here:

  1. **Idempotency** — for any successfully-canonicalized input, a second call
     produces the same output. Critical: if idempotency breaks, two callers
     that submit equivalent surface_ids could end up with different canonical
     strings depending on how many times the helper has run.
  2. **Scheme preservation** — successful output starts with the same scheme
     prefix as the input (no scheme-rewriting).
  3. **No-crash on arbitrary binary** — `canonicalize/1` always returns
     `{:ok, _}` or `{:error, _}` for any binary input; never raises. (The
     module is called from inside an HTTP request handler; an exception
     would surface as a 503.)
  4. **dialectic:/ output is always lowercase** in the path component.
  5. **capture:/ output members are always sorted lexically**.
  6. **resident:/ output never contains the reserved chars** (` `, `\\t`,
     `\\n`, `#`, `&`).
  7. **Top-level `?` rejection** — any input containing `?` produces
     `{:error, :reserved_query_string}`, regardless of scheme.
  """

  use ExUnit.Case, async: true
  use ExUnitProperties

  alias UnitaresLeasePlane.Canonicalize

  # ---------- generators ----------

  # Path component characters we know neither implementation rejects:
  # printable ASCII minus the top-level rejected `?` and minus the
  # resident:/ reserved chars (those are scheme-specific so we filter
  # per-property when needed).
  defp safe_path_char do
    StreamData.string(
      Enum.concat([?A..?Z, ?a..?z, ?0..?9, [?_, ?-, ?., ?/, ?:, ?+, ?=]]),
      length: 1
    )
  end

  defp safe_path_string do
    StreamData.list_of(safe_path_char(), min_length: 1, max_length: 64)
    |> StreamData.map(&Enum.join/1)
  end

  # Generates dialectic:/ surface_ids with random ASCII-letter+digit paths.
  defp dialectic_surface_id do
    StreamData.bind(safe_path_string(), fn path ->
      StreamData.constant("dialectic:/" <> path)
    end)
  end

  # resident:/, maintenance:/, agent:/ — exclude reserved chars (`?` already
  # at top, plus space, tab, newline, `#`, `&`).
  defp opaque_surface_id(prefix) do
    StreamData.list_of(
      StreamData.string(
        Enum.concat([?A..?Z, ?a..?z, ?0..?9, [?_, ?-, ?., ?/, ?:, ?+, ?=]]),
        length: 1
      ),
      min_length: 1,
      max_length: 64
    )
    |> StreamData.map(fn chars -> prefix <> Enum.join(chars) end)
  end

  defp resident_surface_id do
    opaque_surface_id("resident:/")
  end

  defp maintenance_surface_id do
    opaque_surface_id("maintenance:/")
  end

  defp agent_surface_id do
    opaque_surface_id("agent:/")
  end

  defp capture_member do
    StreamData.list_of(
      StreamData.string(Enum.concat([?A..?Z, ?a..?z, ?0..?9]), length: 1),
      min_length: 1,
      max_length: 16
    )
    |> StreamData.map(&Enum.join/1)
  end

  defp capture_surface_id do
    StreamData.list_of(capture_member(), min_length: 1, max_length: 8)
    |> StreamData.map(fn members -> "capture:/" <> Enum.join(members, ",") end)
  end

  defp td_surface_id do
    StreamData.bind(safe_path_string(), fn path ->
      StreamData.constant("td:/" <> path)
    end)
  end

  # ---------- properties ----------

  describe "idempotency (RFC §7.12 — split-brain prevention)" do
    property "dialectic:/ canonicalize is idempotent" do
      check all(input <- dialectic_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, once} ->
            assert {:ok, ^once} = Canonicalize.canonicalize(once)

          {:error, _reason} ->
            # Generator may produce inputs the canonicalizer rejects;
            # idempotency is meaningful only on the {:ok, _} branch.
            :ok
        end
      end
    end

    property "resident:/ canonicalize is idempotent" do
      check all(input <- resident_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, once} -> assert {:ok, ^once} = Canonicalize.canonicalize(once)
          {:error, _} -> :ok
        end
      end
    end

    property "maintenance:/ canonicalize is idempotent" do
      check all(input <- maintenance_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, once} -> assert {:ok, ^once} = Canonicalize.canonicalize(once)
          {:error, _} -> :ok
        end
      end
    end

    property "capture:/ canonicalize is idempotent" do
      check all(input <- capture_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, once} -> assert {:ok, ^once} = Canonicalize.canonicalize(once)
          {:error, _} -> :ok
        end
      end
    end

    property "td:/ canonicalize is idempotent" do
      check all(input <- td_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, once} -> assert {:ok, ^once} = Canonicalize.canonicalize(once)
          {:error, _} -> :ok
        end
      end
    end

    property "agent:/ canonicalize is idempotent" do
      check all(input <- agent_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, once} -> assert {:ok, ^once} = Canonicalize.canonicalize(once)
          {:error, _} -> :ok
        end
      end
    end
  end

  describe "scheme preservation" do
    property "successful output starts with the same scheme prefix as input" do
      check all(
              scheme <-
                StreamData.member_of([
                  "dialectic:/",
                  "resident:/",
                  "maintenance:/",
                  "capture:/",
                  "td:/",
                  "agent:/"
                ]),
              path <- safe_path_string()
            ) do
        input = scheme <> path

        case Canonicalize.canonicalize(input) do
          {:ok, output} ->
            assert String.starts_with?(output, scheme),
                   "expected #{output} to start with #{scheme}"

          {:error, _} ->
            :ok
        end
      end
    end
  end

  describe "no-crash invariant (RFC §7.12 — HTTP handler safety)" do
    property "canonicalize/1 always returns {:ok,_} or {:error,_} for any binary, never raises" do
      check all(input <- StreamData.binary(min_length: 0, max_length: 256)) do
        result = Canonicalize.canonicalize(input)

        assert match?({:ok, _}, result) or match?({:error, _}, result),
               "expected tagged tuple, got #{inspect(result)}"
      end
    end

    property "canonicalize/1 never raises on arbitrary string input" do
      check all(input <- StreamData.string(:printable, min_length: 0, max_length: 256)) do
        result = Canonicalize.canonicalize(input)
        assert match?({:ok, _}, result) or match?({:error, _}, result)
      end
    end
  end

  describe "per-scheme output invariants" do
    property "dialectic:/ output path is always lowercase" do
      check all(input <- dialectic_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, "dialectic:/" <> path} ->
            assert path == String.downcase(path),
                   "expected lowercase path, got #{inspect(path)}"

          {:error, _} ->
            :ok
        end
      end
    end

    property "capture:/ output members are sorted lexically" do
      check all(input <- capture_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, "capture:/" <> path} ->
            members = String.split(path, ",", trim: true)

            assert members == Enum.sort(members),
                   "expected sorted members, got #{inspect(members)}"

          {:error, _} ->
            :ok
        end
      end
    end

    property "resident:/ output never contains reserved chars (space, tab, newline, #, &)" do
      check all(input <- resident_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, "resident:/" <> path} ->
            refute String.contains?(path, " ")
            refute String.contains?(path, "\t")
            refute String.contains?(path, "\n")
            refute String.contains?(path, "#")
            refute String.contains?(path, "&")

          {:error, _} ->
            :ok
        end
      end
    end

    property "maintenance:/ output never contains reserved chars (space, tab, newline, #, &)" do
      check all(input <- maintenance_surface_id()) do
        case Canonicalize.canonicalize(input) do
          {:ok, "maintenance:/" <> path} ->
            refute String.contains?(path, " ")
            refute String.contains?(path, "\t")
            refute String.contains?(path, "\n")
            refute String.contains?(path, "#")
            refute String.contains?(path, "&")

          {:error, _} ->
            :ok
        end
      end
    end
  end

  describe "top-level ? rejection (PR 7 council BLOCK B1 — RFC §7.12.4 OPERATOR_NOTE 3)" do
    property "any input containing ? produces {:error, :reserved_query_string}" do
      check all(
              prefix <-
                StreamData.member_of([
                  "dialectic:/",
                  "resident:/",
                  "maintenance:/",
                  "capture:/",
                  "td:/",
                  "agent:/",
                  "file:///",
                  ""
                ]),
              before <- StreamData.string(:alphanumeric, max_length: 16),
              after_q <- StreamData.string(:alphanumeric, max_length: 16)
            ) do
        # Inject `?` into otherwise-canonical-looking inputs.
        input = prefix <> before <> "?" <> after_q

        # NUL byte and length checks fire BEFORE the ? check, so filter
        # those out — they're tested separately in the example tests.
        unless String.contains?(input, <<0>>) or byte_size(input) > 4096 do
          assert {:error, :reserved_query_string} = Canonicalize.canonicalize(input)
        end
      end
    end
  end
end
