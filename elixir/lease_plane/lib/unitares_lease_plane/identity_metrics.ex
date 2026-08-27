defmodule UnitaresLeasePlane.IdentityMetrics do
  @moduledoc "Bounded-cardinality counters for lease identity verification."

  use GenServer

  @outcomes [:verified, :invalid, :unavailable, :replayed]
  @proof_types [:attestation, :legacy, :missing]

  def start_link(_opts), do: GenServer.start_link(__MODULE__, :ok, name: __MODULE__)

  @spec record(atom(), atom(), non_neg_integer()) :: :ok
  def record(proof_type, outcome, elapsed_us)
      when proof_type in @proof_types and outcome in @outcomes and is_integer(elapsed_us) do
    if Process.whereis(__MODULE__) do
      GenServer.cast(__MODULE__, {:record, proof_type, outcome, max(elapsed_us, 0)})
    end

    :ok
  end

  def record(_proof_type, _outcome, _elapsed_us), do: :ok

  @spec snapshot() :: map()
  def snapshot do
    if Process.whereis(__MODULE__) do
      GenServer.call(__MODULE__, :snapshot)
    else
      initial_state()
    end
  end

  @impl true
  def init(:ok), do: {:ok, initial_state()}

  @impl true
  def handle_cast({:record, proof_type, outcome, elapsed_us}, state) do
    state =
      state
      |> update_in([:total], &(&1 + 1))
      |> update_in([:outcomes, outcome], &(&1 + 1))
      |> update_in([:proof_types, proof_type], &(&1 + 1))
      |> update_in([:latency, :count], &(&1 + 1))
      |> update_in([:latency, :total_us], &(&1 + elapsed_us))
      |> update_in([:latency, :max_us], &max(&1, elapsed_us))

    {:noreply, state}
  end

  @impl true
  def handle_call(:snapshot, _from, state), do: {:reply, state, state}

  defp initial_state do
    %{
      total: 0,
      outcomes: Map.new(@outcomes, &{&1, 0}),
      proof_types: Map.new(@proof_types, &{&1, 0}),
      latency: %{count: 0, total_us: 0, max_us: 0}
    }
  end
end
