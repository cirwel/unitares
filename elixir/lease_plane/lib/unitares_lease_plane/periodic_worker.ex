defmodule UnitaresLeasePlane.PeriodicWorker do
  @moduledoc """
  Lightweight scheduler for worker modules with a `perform/1` callback.

  The production shape matches the Oban worker boundary (`perform(args)`), while
  keeping this v0 lease-plane app on its existing Postgrex-only dependency set.
  """

  use GenServer

  require Logger

  def child_spec(opts) do
    id = Keyword.fetch!(opts, :id)

    %{
      id: id,
      start: {__MODULE__, :start_link, [opts]},
      restart: :permanent,
      type: :worker
    }
  end

  def start_link(opts) do
    name = Keyword.get(opts, :name)

    if name do
      GenServer.start_link(__MODULE__, opts, name: name)
    else
      GenServer.start_link(__MODULE__, opts)
    end
  end

  @impl true
  def init(opts) do
    state = %{
      worker: Keyword.fetch!(opts, :worker),
      args: Keyword.get(opts, :args, %{}),
      interval_ms: Keyword.fetch!(opts, :interval_ms)
    }

    Process.send_after(self(), :run, Keyword.get(opts, :initial_delay_ms, 0))
    {:ok, state}
  end

  @impl true
  def handle_info(:run, state) do
    run_worker(state.worker, state.args)
    Process.send_after(self(), :run, state.interval_ms)
    {:noreply, state}
  end

  defp run_worker(worker, args) do
    case worker.perform(args) do
      {:ok, _summary} ->
        :ok

      {:error, reason} ->
        Logger.warning("lease_plane worker #{inspect(worker)} failed: #{inspect(reason)}")

      other ->
        Logger.warning("lease_plane worker #{inspect(worker)} returned #{inspect(other)}")
    end
  rescue
    error ->
      Logger.error("lease_plane worker #{inspect(worker)} raised: #{Exception.message(error)}")
  end
end
