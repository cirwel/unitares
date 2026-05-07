defmodule UnitaresSentinel.Cutover do
  @moduledoc """
  File-level cutover helpers for Sentinel-on-BEAM.

  These helpers implement the v0.1.2 §B3 max-wins protocol:

    * cutover to BEAM writes the max cursor to `STATE_FILE.beam` with
      `runtime: "beam_canonical"`;
    * rollback to Python writes the max cursor back to `STATE_FILE` and marks
      the shadow as `runtime: "python_canonical"` for forensics.

  Launchctl stop/start ordering remains an operator/script concern outside
  these pure file operations.
  """

  alias UnitaresSentinel.{AtomicWrite, CycleState}

  @runtime_key "runtime"
  @beam_canonical "beam_canonical"
  @python_canonical "python_canonical"

  @type result :: %{
          canonical: Path.t(),
          shadow: Path.t(),
          cursor: String.t() | nil,
          runtime: String.t()
        }

  @doc """
  Mark the BEAM shadow file canonical after applying max-cursor merge.
  """
  @spec cutover_to_beam(keyword()) :: {:ok, result()}
  def cutover_to_beam(opts \\ []) do
    {canonical, shadow} = resolve_paths(opts)

    state =
      canonical
      |> load_max_state(shadow)
      |> Map.put(@runtime_key, @beam_canonical)

    :ok = write_state(shadow, state)

    {:ok,
     %{
       canonical: canonical,
       shadow: shadow,
       cursor: CycleState.get_last_event_ts(state),
       runtime: @beam_canonical
     }}
  end

  @doc """
  Copy the max cursor back to the Python canonical file and mark shadow rollback.
  """
  @spec rollback_to_python(keyword()) :: {:ok, result()}
  def rollback_to_python(opts \\ []) do
    {canonical, shadow} = resolve_paths(opts)

    state =
      canonical
      |> load_max_state(shadow)
      |> Map.delete(@runtime_key)

    :ok = write_state(canonical, state)
    :ok = write_state(shadow, Map.put(state, @runtime_key, @python_canonical))

    {:ok,
     %{
       canonical: canonical,
       shadow: shadow,
       cursor: CycleState.get_last_event_ts(state),
       runtime: @python_canonical
     }}
  end

  @doc false
  @spec resolve_paths(keyword()) :: {Path.t(), Path.t()}
  def resolve_paths(opts \\ []) do
    canonical = Keyword.get(opts, :canonical) || CycleState.resolve_canonical_path()
    shadow = Keyword.get(opts, :shadow) || canonical <> ".beam"
    {canonical, shadow}
  end

  defp load_max_state(canonical, shadow) do
    canonical
    |> read_state()
    |> pick_max(read_state(shadow))
  end

  defp read_state(path) do
    with {:ok, contents} <- File.read(path),
         {:ok, decoded} when is_map(decoded) <- Jason.decode(contents) do
      decoded
    else
      _ -> %{}
    end
  end

  defp write_state(path, state) do
    encoded = state |> Jason.encode!() |> Jason.decode!() |> Jason.encode!()
    AtomicWrite.write(path, encoded)
  end

  defp pick_max(%{} = canonical, shadow) when map_size(canonical) == 0, do: shadow
  defp pick_max(canonical, %{} = shadow) when map_size(shadow) == 0, do: canonical

  defp pick_max(canonical, shadow) do
    canonical_cursor = CycleState.get_last_event_ts(canonical)
    shadow_cursor = CycleState.get_last_event_ts(shadow)

    if CycleState.compare_cursor(canonical_cursor, shadow_cursor) in [:gt, :eq] do
      canonical
    else
      shadow
    end
  end
end
