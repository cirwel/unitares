defmodule UnitaresLeasePlane.LeaseHolder do
  @moduledoc """
  GenServer that owns a single `local_beam` lease for the lifetime of its
  process. Runs an in-process renew timer at `original_ttl_s / 3` cadence
  (RFC §4.4.2) so that a supervisor crash leaves a corpse lease for at
  most `original_ttl_s` before the reaper sweeps it.

  Process death paths:
    - normal stop / `release/2` → updates `released_at` with the given reason
    - abnormal exit              → terminate/2 records `release_reason='down_local'`
    - reaper sweep (TTL expiry)  → DB row is closed externally; this process
                                   detects the now-released row at next renew
                                   and exits cleanly with `:ttl_expired`
  """

  use GenServer

  require Logger
  alias UnitaresLeasePlane.{HolderRegistry, Repo}

  defstruct [:lease, :tick_ms]

  # ---------- public API ----------

  def start_link(%{lease_id: lease_id} = lease) do
    GenServer.start_link(__MODULE__, lease, name: via(lease_id))
  end

  @spec renew(pid()) :: :ok | {:error, term()}
  def renew(pid), do: GenServer.call(pid, :renew)

  @spec release(pid(), String.t()) :: :ok | {:error, term()}
  def release(pid, reason) when is_binary(reason) do
    GenServer.call(pid, {:release, reason})
  end

  @spec lease(pid()) :: map()
  def lease(pid), do: GenServer.call(pid, :lease)

  defp via(lease_id), do: {:via, Registry, {HolderRegistry, {:lease, lease_id}}}

  # ---------- callbacks ----------

  @impl true
  def init(%{original_ttl_s: ttl_s} = lease) do
    Process.flag(:trap_exit, true)
    tick_ms = max(div(ttl_s * 1000, 3), 1_000)
    schedule_tick(tick_ms)
    {:ok, %__MODULE__{lease: lease, tick_ms: tick_ms}}
  end

  @impl true
  def handle_call(:lease, _from, state), do: {:reply, state.lease, state}

  def handle_call(:renew, _from, state) do
    case Repo.renew(state.lease.lease_id) do
      :ok -> {:reply, :ok, state}
      {:error, :not_found} -> {:stop, :ttl_expired, {:error, :not_found}, state}
      err -> {:reply, err, state}
    end
  end

  def handle_call({:release, reason}, _from, state) do
    case Repo.release(state.lease.lease_id, reason) do
      :ok -> {:stop, :normal, :ok, %{state | lease: %{state.lease | released_at: :released}}}
      err -> {:reply, err, state}
    end
  end

  @impl true
  def handle_info(:tick, state) do
    case Repo.renew(state.lease.lease_id) do
      :ok ->
        schedule_tick(state.tick_ms)
        {:noreply, state}

      {:error, :not_found} ->
        # DB-side release happened (reaper, force-release, manual close) — the
        # canonical truth says we no longer hold this lease. Exit cleanly.
        {:stop, :ttl_expired, state}

      {:error, reason} ->
        Logger.warning("lease_plane renew tick failed: #{inspect(reason)}; will retry next tick")

        schedule_tick(state.tick_ms)
        {:noreply, state}
    end
  end

  # trap_exit is on so we can run terminate/2 on abnormal shutdowns. EXIT
  # signals (Process.exit, supervisor shutdown) land here and we forward to
  # {:stop, reason, state} so terminate/2 gets a chance to write release rows.
  def handle_info({:EXIT, _from, reason}, state) do
    {:stop, reason, state}
  end

  @impl true
  def terminate(reason, %{lease: %{released_at: :released}})
      when reason in [:normal, :shutdown] do
    :ok
  end

  def terminate(reason, %{lease: lease}) do
    # Process is going down without an explicit release — record it.
    case Repo.release(lease.lease_id, "down_local") do
      :ok ->
        :ok

      err ->
        Logger.warning(
          "lease_plane terminate could not write down_local for #{lease.lease_id} reason=#{inspect(reason)}: #{inspect(err)}"
        )

        :ok
    end
  end

  defp schedule_tick(ms), do: Process.send_after(self(), :tick, ms)
end
