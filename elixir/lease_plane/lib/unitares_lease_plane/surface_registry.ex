defmodule UnitaresLeasePlane.SurfaceRegistry do
  @moduledoc """
  Pure in-memory surface registry for the R7 Phase 1 spike.

  The registry serializes acquire decisions for active surfaces and keeps one
  `LeaseProcess` alive per active lease. It intentionally has no database,
  HTTP, audit, or telemetry dependency; those stay in the durable lease plane.
  """

  use GenServer

  alias UnitaresLeasePlane.LeaseProcess

  defstruct surfaces: %{}, leases: %{}, monitors: %{}

  @type lease :: map()

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    {name, opts} = Keyword.pop(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  @spec acquire(GenServer.server(), map()) ::
          {:ok, lease(), :new | :idempotent}
          | {:error, :held_by_other, map()}
  def acquire(registry, %{} = params) do
    GenServer.call(registry, {:acquire, params})
  end

  @spec status(GenServer.server(), binary()) :: {:ok, lease() | nil}
  def status(registry, surface_id) when is_binary(surface_id) do
    GenServer.call(registry, {:status, surface_id})
  end

  @spec renew(GenServer.server(), binary(), binary(), pos_integer()) ::
          {:ok, lease()} | {:error, :not_found | :not_holder}
  def renew(registry, lease_id, holder_agent_uuid, ttl_ms)
      when is_binary(lease_id) and is_binary(holder_agent_uuid) do
    GenServer.call(registry, {:renew, lease_id, holder_agent_uuid, ttl_ms})
  end

  @spec release(GenServer.server(), binary(), binary(), String.t()) ::
          :ok | {:error, :not_found | :not_holder}
  def release(registry, lease_id, holder_agent_uuid, reason)
      when is_binary(lease_id) and is_binary(holder_agent_uuid) and is_binary(reason) do
    GenServer.call(registry, {:release, lease_id, holder_agent_uuid, reason})
  end

  @impl true
  def init(_opts), do: {:ok, %__MODULE__{}}

  @impl true
  def handle_call({:acquire, params}, _from, state) do
    case Map.fetch(state.surfaces, params.surface_id) do
      {:ok, lease_id} ->
        lease = Map.fetch!(state.leases, lease_id)

        if lease.holder_agent_uuid == params.holder_agent_uuid do
          {:reply, {:ok, public_lease(lease), :idempotent}, state}
        else
          {:reply, {:error, :held_by_other, conflict_info(lease)}, state}
        end

      :error ->
        case start_lease(params) do
          {:ok, lease, pid, ref} ->
            state =
              state
              |> put_surface(lease)
              |> put_lease(lease, pid, ref)

            {:reply, {:ok, public_lease(lease), :new}, state}
        end
    end
  end

  def handle_call({:status, surface_id}, _from, state) do
    lease =
      with {:ok, lease_id} <- Map.fetch(state.surfaces, surface_id),
           {:ok, lease} <- Map.fetch(state.leases, lease_id) do
        public_lease(lease)
      else
        _ -> nil
      end

    {:reply, {:ok, lease}, state}
  end

  def handle_call({:renew, lease_id, holder_agent_uuid, ttl_ms}, _from, state) do
    with {:ok, lease} <- fetch_lease(state, lease_id),
         :ok <- same_holder(lease, holder_agent_uuid),
         {:ok, renewed} <- LeaseProcess.renew(lease.pid, ttl_ms) do
      state = put_lease(state, Map.put(renewed, :pid, lease.pid), lease.pid, lease.monitor_ref)
      {:reply, {:ok, public_lease(renewed)}, state}
    else
      {:error, reason} -> {:reply, {:error, reason}, state}
    end
  end

  def handle_call({:release, lease_id, holder_agent_uuid, reason}, _from, state) do
    with {:ok, lease} <- fetch_lease(state, lease_id),
         :ok <- same_holder(lease, holder_agent_uuid),
         {:ok, _released} <- LeaseProcess.release(lease.pid, reason) do
      state = remove_lease(state, lease)
      {:reply, :ok, state}
    else
      {:error, reason} -> {:reply, {:error, reason}, state}
    end
  end

  @impl true
  def handle_cast({:lease_expired, lease_id, pid}, state) do
    state =
      case Map.fetch(state.leases, lease_id) do
        {:ok, %{pid: ^pid} = lease} -> remove_lease(state, lease)
        _ -> state
      end

    {:noreply, state}
  end

  @impl true
  def handle_info({:DOWN, ref, :process, _pid, _reason}, state) do
    state =
      case Map.fetch(state.monitors, ref) do
        {:ok, lease_id} ->
          case Map.fetch(state.leases, lease_id) do
            {:ok, lease} -> remove_lease(state, lease, demonitor?: false)
            :error -> %{state | monitors: Map.delete(state.monitors, ref)}
          end

        :error ->
          state
      end

    {:noreply, state}
  end

  defp start_lease(params) do
    now = DateTime.utc_now()
    ttl_ms = Map.get(params, :ttl_ms, Map.get(params, :ttl_s, 30) * 1000)

    lease = %{
      lease_id: random_uuid(),
      surface_id: params.surface_id,
      holder_agent_uuid: params.holder_agent_uuid,
      holder_label: Map.get(params, :holder_label),
      episode_id: Map.get(params, :episode_id),
      harness: Map.get(params, :harness),
      intent: Map.get(params, :intent),
      evidence_ref: Map.get(params, :evidence_ref),
      ttl_ms: ttl_ms,
      acquired_at: now,
      expires_at: DateTime.add(now, ttl_ms, :millisecond),
      released_at: nil,
      release_reason: nil
    }

    {:ok, pid} = LeaseProcess.start_link({self(), lease})
    ref = Process.monitor(pid)

    {:ok, Map.put(lease, :pid, pid), pid, ref}
  end

  defp put_surface(state, lease),
    do: %{state | surfaces: Map.put(state.surfaces, lease.surface_id, lease.lease_id)}

  defp put_lease(state, lease, pid, ref) do
    internal = lease |> Map.put(:pid, pid) |> Map.put(:monitor_ref, ref)

    %{
      state
      | leases: Map.put(state.leases, lease.lease_id, internal),
        monitors: Map.put(state.monitors, ref, lease.lease_id)
    }
  end

  defp remove_lease(state, lease, opts \\ []) do
    if Keyword.get(opts, :demonitor?, true) do
      Process.demonitor(lease.monitor_ref, [:flush])
    end

    %{
      state
      | surfaces: Map.delete(state.surfaces, lease.surface_id),
        leases: Map.delete(state.leases, lease.lease_id),
        monitors: Map.delete(state.monitors, lease.monitor_ref)
    }
  end

  defp fetch_lease(state, lease_id) do
    case Map.fetch(state.leases, lease_id) do
      {:ok, lease} -> {:ok, lease}
      :error -> {:error, :not_found}
    end
  end

  defp same_holder(%{holder_agent_uuid: holder_agent_uuid}, holder_agent_uuid), do: :ok
  defp same_holder(_lease, _holder_agent_uuid), do: {:error, :not_holder}

  defp conflict_info(lease) do
    %{
      lease_id: lease.lease_id,
      held_by_uuid: lease.holder_agent_uuid,
      expires_at: lease.expires_at,
      intent: lease.intent
    }
  end

  defp public_lease(lease), do: Map.drop(lease, [:pid, :monitor_ref])

  defp random_uuid do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)
    parts = [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    Enum.map_join(parts, "-", &Base.encode16(&1, case: :lower))
  end
end
