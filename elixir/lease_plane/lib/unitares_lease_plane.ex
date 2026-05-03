defmodule UnitaresLeasePlane do
  @moduledoc """
  Surface lease plane v0 — Elixir/OTP coordination kernel.

  Public-API wrappers live here. The contract is documented in
  `docs/proposals/surface-lease-plane-v0.md` (RFC v0.5).

  Top-level invariant: BEAM owns live coordination, Python owns governance
  truth, Postgres owns durable truth. Nothing in this module may silently
  become source of truth for identity, EISV, KG, or calibration.
  """

  alias UnitaresLeasePlane.{HandoffServer, LeaseHolder, LeaseSupervisor, Repo}

  @doc """
  Acquire a lease for a `local_beam` surface — spawns a `LeaseHolder`
  GenServer that owns the lease for the lifetime of its process.

  Returns `{:ok, lease, idempotent_flag}` where `idempotent_flag` is `:new`
  or `:idempotent`, or `{:error, reason}` matching the typed-absence shapes
  from RFC §4.5.
  """
  @spec acquire_local_beam(map()) :: {:ok, map(), :new | :idempotent} | {:error, term()}
  def acquire_local_beam(%{} = params) do
    LeaseSupervisor.start_holder(params)
  end

  @doc """
  Acquire a lease for a `remote_heartbeat` surface — no BEAM process is
  spawned; the row is written and the remote client must HTTP-heartbeat
  before `expires_at` or be reaped.
  """
  @spec acquire_remote_heartbeat(map()) :: {:ok, map(), :new | :idempotent} | {:error, term()}
  def acquire_remote_heartbeat(%{} = params) do
    Repo.acquire(Map.put(params, :holder_kind, "remote_heartbeat"))
  end

  @doc "Status by `surface_id`. Returns `{:ok, lease | nil}` or `{:error, reason}`."
  @spec status(String.t()) :: {:ok, map() | nil} | {:error, term()}
  def status(surface_id) when is_binary(surface_id), do: Repo.status(surface_id)

  @doc "Renew a lease — extends `expires_at` by the immutable `original_ttl_s`."
  @spec renew(binary()) :: :ok | {:error, term()}
  def renew(lease_id) do
    case LeaseSupervisor.holder_for(lease_id) do
      {:ok, pid} -> LeaseHolder.renew(pid)
      :error -> Repo.renew(lease_id)
    end
  end

  @doc """
  Release a lease. Local-BEAM holders should normally let process death do
  this; calling release/2 directly is the explicit-shutdown path.
  """
  @spec release(binary(), String.t()) :: :ok | {:error, term()}
  def release(lease_id, release_reason \\ "normal") do
    case LeaseSupervisor.holder_for(lease_id) do
      {:ok, pid} -> LeaseHolder.release(pid, release_reason)
      :error -> Repo.release(lease_id, release_reason)
    end
  end

  @doc """
  Force-release a lease — operator-typed `release_reason='forced'` per RFC §7.10.

  Delegates to `release/2` so a live LeaseHolder is shut down gracefully when
  one exists; falls through to a direct Repo update when the holder is gone.

  ## Authority and trust boundary

  The contract-layer gate is the HTTPAuth path-aware token check on the
  `/v1/lease/force-release` route. This function is called only from the
  router handler for that route. Adding a new in-process caller — including
  any future OTP worker, Reaper extension, or scheduled job — requires an
  RFC amendment (§7.10 commits force-release authority to operator-only,
  contract-layer enforced). If you find yourself wanting to call this from
  in-BEAM code, route through the HTTP endpoint with the elevated bearer
  instead, even from inside the same OS process.
  """
  @spec force_release(binary()) :: :ok | {:error, term()}
  def force_release(lease_id), do: release(lease_id, "forced")

  @doc "Offer a lease handoff to another holder UUID."
  @spec handoff_offer(binary(), binary(), pos_integer()) :: {:ok, binary()} | {:error, term()}
  def handoff_offer(lease_id, to_holder_agent_uuid, ttl_s) do
    HandoffServer.offer(lease_id, to_holder_agent_uuid, ttl_s)
  end

  @doc "Accept a pending handoff by id."
  @spec handoff_accept(binary()) :: :ok | {:error, term()}
  def handoff_accept(handoff_id) do
    HandoffServer.accept(handoff_id)
  end
end
