defmodule UnitaresLeasePlane.HandoffServer do
  @moduledoc """
  In-node handoff state machine.

  Handoff is release-and-reacquire: an offer reserves intent in BEAM memory and
  audit outbox, then accept closes the old lease row with `release_reason='handoff'`
  and creates a new remote-heartbeat lease for the receiving holder.
  """

  use GenServer

  require Logger

  alias UnitaresLeasePlane.{LeaseSupervisor, Repo}

  defmodule Pending do
    @moduledoc false
    defstruct [
      :handoff_id,
      :lease_id,
      :to_holder_agent_uuid,
      :ttl_s,
      :offered_at,
      :expires_at
    ]
  end

  @type pending :: %Pending{
          handoff_id: binary(),
          lease_id: binary(),
          to_holder_agent_uuid: binary(),
          ttl_s: pos_integer(),
          offered_at: DateTime.t(),
          expires_at: DateTime.t()
        }

  # ---------- public API ----------

  def start_link(_args), do: GenServer.start_link(__MODULE__, %{}, name: __MODULE__)

  @spec offer(binary(), binary(), pos_integer()) :: {:ok, binary()} | {:error, term()}
  def offer(lease_id, to_holder_agent_uuid, ttl_s)
      when is_binary(lease_id) and is_binary(to_holder_agent_uuid) and is_integer(ttl_s) and
             ttl_s > 0 do
    GenServer.call(__MODULE__, {:offer, lease_id, to_holder_agent_uuid, ttl_s})
  end

  @spec accept(binary()) :: :ok | {:error, term()}
  def accept(handoff_id) when is_binary(handoff_id) do
    GenServer.call(__MODULE__, {:accept, handoff_id})
  end

  @spec expire_stale :: non_neg_integer()
  def expire_stale do
    GenServer.call(__MODULE__, :expire_stale)
  end

  # ---------- callbacks ----------

  @impl true
  def init(_args), do: {:ok, %{pending: %{}}}

  @impl true
  def handle_call({:offer, lease_id, to_holder_agent_uuid, ttl_s}, _from, state) do
    with {:ok, lease} <- Repo.active_lease(lease_id),
         pending = new_pending(lease_id, to_holder_agent_uuid, ttl_s),
         :ok <- Repo.log_handoff_offer(lease, pending) do
      ms = max(DateTime.diff(pending.expires_at, DateTime.utc_now(), :millisecond), 1)
      Process.send_after(self(), {:expire, pending.handoff_id}, ms)
      state = put_in(state, [:pending, pending.handoff_id], pending)
      {:reply, {:ok, pending.handoff_id}, state}
    else
      {:error, reason} ->
        Logger.warning("lease_plane handoff offer failed: #{inspect(reason)}")
        {:reply, {:error, reason}, state}
    end
  end

  def handle_call({:accept, handoff_id}, _from, state) do
    case Map.fetch(state.pending, handoff_id) do
      {:ok, pending} ->
        accept_pending(pending, state)

      :error ->
        {:reply, {:error, :not_found}, state}
    end
  end

  def handle_call(:expire_stale, _from, state) do
    {count, pending} = drop_expired(state.pending)
    {:reply, count, %{state | pending: pending}}
  end

  @impl true
  def handle_info({:expire, handoff_id}, state) do
    case Map.fetch(state.pending, handoff_id) do
      {:ok, pending} ->
        if expired?(pending) do
          {:noreply, %{state | pending: Map.delete(state.pending, handoff_id)}}
        else
          {:noreply, state}
        end

      :error ->
        {:noreply, state}
    end
  end

  defp accept_pending(pending, state) do
    cond do
      expired?(pending) ->
        state = %{state | pending: Map.delete(state.pending, pending.handoff_id)}
        {:reply, {:error, :expired}, state}

      true ->
        case Repo.accept_handoff(pending) do
          {:ok, _new_lease} ->
            LeaseSupervisor.stop_after_handoff(pending.lease_id)
            state = %{state | pending: Map.delete(state.pending, pending.handoff_id)}
            {:reply, :ok, state}

          {:error, reason} ->
            Logger.warning("lease_plane handoff accept failed: #{inspect(reason)}")
            {:reply, {:error, reason}, state}
        end
    end
  end

  defp new_pending(lease_id, to_holder_agent_uuid, ttl_s) do
    now = DateTime.utc_now()
    offer_window_s = Application.get_env(:lease_plane, :handoff_offer_window_s, 30)

    %Pending{
      handoff_id: uuid4(),
      lease_id: lease_id,
      to_holder_agent_uuid: to_holder_agent_uuid,
      ttl_s: ttl_s,
      offered_at: now,
      expires_at: DateTime.add(now, offer_window_s, :second)
    }
  end

  defp drop_expired(pending) do
    Enum.reduce(pending, {0, %{}}, fn {handoff_id, offer}, {count, acc} ->
      if expired?(offer) do
        {count + 1, acc}
      else
        {count, Map.put(acc, handoff_id, offer)}
      end
    end)
  end

  defp expired?(%Pending{expires_at: expires_at}) do
    DateTime.compare(DateTime.utc_now(), expires_at) != :lt
  end

  defp uuid4 do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)

    [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    |> Enum.map_join("-", &Base.encode16(&1, case: :lower))
  end
end
