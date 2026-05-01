defmodule UnitaresLeasePlane.LeaseSupervisor do
  @moduledoc """
  DynamicSupervisor over `LeaseHolder` GenServers — one process per active
  `local_beam` lease. The Registry maps lease_id → pid for lookup.

  Remote-heartbeat leases do NOT live here; they are pure DB rows reaped
  by TTL via the Reaper (Oban job, separate module).
  """

  use DynamicSupervisor

  alias UnitaresLeasePlane.{LeaseHolder, Repo}

  def start_link(_args), do: DynamicSupervisor.start_link(__MODULE__, [], name: __MODULE__)

  @impl true
  def init(_), do: DynamicSupervisor.init(strategy: :one_for_one)

  @doc """
  Acquire a `local_beam` lease and spawn its `LeaseHolder` process.
  Returns `{:ok, lease, :new | :idempotent}` on success.
  """
  @spec start_holder(map()) ::
          {:ok, map(), :new | :idempotent}
          | {:error, :held_by_other, map()}
          | {:error, term()}
  def start_holder(%{} = params) do
    params = Map.put(params, :holder_kind, "local_beam")

    case Repo.acquire(params) do
      {:ok, lease, kind} ->
        case spawn_holder(lease) do
          {:ok, _pid} -> {:ok, lease, kind}
          {:error, :already_started} -> {:ok, lease, :idempotent}
          err -> err
        end

      err ->
        err
    end
  end

  defp spawn_holder(%{lease_id: lease_id} = lease) do
    spec = %{
      id: LeaseHolder,
      start: {LeaseHolder, :start_link, [lease]},
      restart: :temporary
    }

    case DynamicSupervisor.start_child(__MODULE__, spec) do
      {:ok, pid} ->
        {:ok, pid}

      {:error, {:already_started, _pid}} ->
        {:error, :already_started}

      {:error, _reason} = err ->
        # Lease persisted but holder process refused to spawn — release the
        # row so the surface isn't left ghost-held with no process to renew it.
        _ = Repo.release(lease_id, "reaped_after_supervisor_failed")
        err
    end
  end

  @doc "Look up the holder pid for a given `lease_id`."
  @spec holder_for(binary()) :: {:ok, pid()} | :error
  def holder_for(lease_id) when is_binary(lease_id) do
    case Registry.lookup(UnitaresLeasePlane.HolderRegistry, {:lease, lease_id}) do
      [{pid, _}] -> {:ok, pid}
      [] -> :error
    end
  end

  @doc "How many local_beam holders are currently alive."
  @spec count_holders() :: non_neg_integer()
  def count_holders, do: DynamicSupervisor.count_children(__MODULE__).active
end
