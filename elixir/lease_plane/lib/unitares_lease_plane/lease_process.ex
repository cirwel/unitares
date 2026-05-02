defmodule UnitaresLeasePlane.LeaseProcess do
  @moduledoc """
  Pure in-memory lease owner for the R7 Phase 1 spike.

  This process does not touch Postgres or the durable lease-plane contract.
  `SurfaceRegistry` owns conflict decisions; `LeaseProcess` owns the live TTL
  timer for a single acquired surface.
  """

  use GenServer

  defstruct [:lease, :registry, :timer_ref]

  @type lease :: map()

  @spec start_link({GenServer.server(), lease()}) :: GenServer.on_start()
  def start_link({registry, %{} = lease}) do
    GenServer.start_link(__MODULE__, {registry, lease})
  end

  @spec current(pid()) :: lease()
  def current(pid), do: GenServer.call(pid, :current)

  @spec renew(pid(), pos_integer()) :: {:ok, lease()}
  def renew(pid, ttl_ms), do: GenServer.call(pid, {:renew, ttl_ms})

  @spec release(pid(), String.t()) :: {:ok, lease()}
  def release(pid, reason), do: GenServer.call(pid, {:release, reason})

  @impl true
  def init({registry, lease}) do
    timer_ref = schedule_expiry(lease.ttl_ms)
    {:ok, %__MODULE__{lease: lease, registry: registry, timer_ref: timer_ref}}
  end

  @impl true
  def handle_call(:current, _from, state), do: {:reply, state.lease, state}

  def handle_call({:renew, ttl_ms}, _from, state) when is_integer(ttl_ms) and ttl_ms > 0 do
    cancel_timer(state.timer_ref)

    lease = %{state.lease | ttl_ms: ttl_ms, expires_at: expires_at(ttl_ms)}
    timer_ref = schedule_expiry(ttl_ms)

    {:reply, {:ok, lease}, %{state | lease: lease, timer_ref: timer_ref}}
  end

  def handle_call({:release, reason}, _from, state) when is_binary(reason) do
    cancel_timer(state.timer_ref)

    released =
      state.lease
      |> Map.put(:released_at, DateTime.utc_now())
      |> Map.put(:release_reason, reason)

    {:stop, :normal, {:ok, released}, %{state | lease: released, timer_ref: nil}}
  end

  @impl true
  def handle_info(:expire, state) do
    expired =
      state.lease
      |> Map.put(:released_at, DateTime.utc_now())
      |> Map.put(:release_reason, "expired")

    GenServer.cast(state.registry, {:lease_expired, expired.lease_id, self()})
    {:stop, :normal, %{state | lease: expired, timer_ref: nil}}
  end

  defp schedule_expiry(ttl_ms), do: Process.send_after(self(), :expire, ttl_ms)

  defp cancel_timer(nil), do: :ok

  defp cancel_timer(ref) do
    Process.cancel_timer(ref)
    :ok
  end

  defp expires_at(ttl_ms), do: DateTime.add(DateTime.utc_now(), ttl_ms, :millisecond)
end
