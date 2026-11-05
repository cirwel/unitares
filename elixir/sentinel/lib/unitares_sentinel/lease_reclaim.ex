defmodule UnitaresSentinel.LeaseReclaim do
  @moduledoc """
  Per-resident memory of the holder uuids this process has recently put on the
  wire, so a later `held_by_other` that names one of them can be recognized as
  this resident's own stranded lease and released.

  Delegates to the shared `UnitaresSdk.LeaseReclaim` contract (#1462), which
  carries the council-approved (#1459) UUID-history + 15-minute absence-grace
  state machine across BEAM lease-plane clients.

  ## The gap this closes (2026-08-01 incident)

  `LeaseAdvisory.post_acquire_with_recovery/4` resolves a single lost acquire
  response by retrying once with the same body — idempotent re-acquire on
  `holder_agent_uuid`. On 2026-08-01 15:42 BOTH attempts timed out at the
  transport (a Postgres stall slowed the plane past the 2s client budget while
  the first attempt's INSERT had already committed), the attempt's holder uuid
  was discarded, and every subsequent tick minted a fresh uuid that saw only
  `held_by_other`. The poller was starved for 1h49m (216 ticks) until an
  operator force-released the lease — even though every one of those 409
  responses named both the orphan's `held_by_uuid` (a uuid this process minted)
  and the `blocking_lease_id` needed to free it.
  """

  @type state :: UnitaresSdk.LeaseReclaim.state()

  @doc """
  Fresh reclaim state to `Map.merge/2` into a GenServer's state at `init/1`.
  """
  @spec new() :: %{lease_reclaim_candidates: []}
  defdelegate new(), to: UnitaresSdk.LeaseReclaim

  @doc """
  Options to merge into `LeaseAdvisory.acquire_cycle/1` opts so the advisory
  can recognize a conflict that names one of this resident's own recent
  holder uuids.
  """
  @spec acquire_opts(map()) :: keyword()
  defdelegate acquire_opts(state), to: UnitaresSdk.LeaseReclaim

  @doc """
  True when `held_by_uuid` matches one of this resident's remembered candidate
  holder UUIDs.
  """
  @spec own_orphan_uuid?(term(), term()) :: boolean()
  defdelegate own_orphan_uuid?(held_by_uuid, candidates), to: UnitaresSdk.LeaseReclaim

  @doc """
  Update reclaim memory from an acquire scope.

  See `UnitaresSdk.LeaseReclaim.absorb/3`.
  """
  @spec absorb(map(), map(), keyword()) :: map()
  def absorb(state, scope, opts \\ []),
    do: UnitaresSdk.LeaseReclaim.absorb(state, scope, opts)
end
