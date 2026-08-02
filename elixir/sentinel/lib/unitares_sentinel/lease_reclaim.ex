defmodule UnitaresSentinel.LeaseReclaim do
  @moduledoc """
  Per-resident memory of acquire attempts whose outcome was never learned, so a
  later `held_by_other` that names one of OUR holder uuids can be recognized as
  this resident's own stranded lease and released.

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

  ## How it works

  Callers merge `new/0` into their GenServer state, thread `acquire_opts/1`
  into `LeaseAdvisory.acquire_cycle/1`, and pass every returned scope through
  `absorb/2`:

    * an acquire whose both transport attempts failed contributes its
      `attempted_holder_uuid` as a reclaim candidate — that uuid MAY own a
      committed lease this process never learned about;
    * a successful acquire clears all candidates — the surface's unique active
      constraint means none of them can hold a lease if we just acquired it
      (a pending competing INSERT would have blocked ours);
    * a reclaim (`LeaseAdvisory` released a lease held by a candidate uuid)
      clears the candidates it just resolved.

  The candidate list is bounded (`@max_candidates`, FIFO) and in-memory only:
  a resident restart forfeits reclaim for leases stranded before the restart. That
  residual is carried by the doctor's `immortal_lease` check and the
  `LeaseStarvation` self-finding, whose remedy names the blocking lease id for
  a manual force-release.

  ## Safety

  Holder uuids are minted process-locally from `:crypto.strong_rand_bytes/1`
  (`LeaseAdvisory.new_holder_uuid/0`). A match between `held_by_uuid` and a
  remembered candidate therefore proves the blocking lease was created by an
  acquire THIS process sent — releasing it can never take a lease away from
  another live holder. This is deliberately NOT the rejected stable-holder-uuid
  design: uuids stay per-attempt, so two concurrently live residents still
  contend correctly and can never adopt each other's leases.
  """

  @max_candidates 64

  @typedoc "State keys this module merges into a caller GenServer's state."
  @type state :: %{optional(any()) => any(), lease_reclaim_candidates: [String.t()]}

  @doc """
  Fresh reclaim state to `Map.merge/2` into a GenServer's state at `init/1`.
  """
  @spec new() :: %{lease_reclaim_candidates: []}
  def new, do: %{lease_reclaim_candidates: []}

  @doc """
  Options to merge into `LeaseAdvisory.acquire_cycle/1` opts so the advisory
  can recognize a conflict that names one of this resident's own lost attempts.
  """
  @spec acquire_opts(map()) :: keyword()
  def acquire_opts(%{lease_reclaim_candidates: candidates}) when is_list(candidates),
    do: [reclaim_candidates: candidates]

  def acquire_opts(_state), do: []

  @doc """
  Update reclaim memory from an acquire scope.

  Call with every scope `LeaseAdvisory.acquire_cycle/1` returns, whatever its
  outcome — the bookkeeping is driven by the scope's shape:

    * `:acquired_new` / `:acquired_idempotent` → clear candidates (see
      moduledoc: a successful acquire proves none of them holds this surface);
    * `conflict.reclaimed_lease_id` present → the advisory released the
      candidate-held lease this tick; the surviving candidates never committed
      (only one active lease per surface), so clear them too;
    * `conflict.attempted_holder_uuid` present → both transport attempts
      failed; remember the uuid (bounded FIFO).

  A scope can both clear and contribute: a reclaim whose re-acquire itself
  died at the transport clears the resolved candidates and remembers the fresh
  attempt's uuid.
  """
  @spec absorb(map(), map()) :: map()
  def absorb(%{lease_reclaim_candidates: candidates} = state, %{outcome: outcome} = scope) do
    conflict = Map.get(scope, :conflict) || %{}

    base =
      cond do
        outcome in [:acquired_new, :acquired_idempotent] -> []
        Map.has_key?(conflict, :reclaimed_lease_id) -> []
        true -> candidates
      end

    next =
      case Map.get(conflict, :attempted_holder_uuid) do
        uuid when is_binary(uuid) -> append_bounded(base, uuid)
        _ -> base
      end

    %{state | lease_reclaim_candidates: next}
  end

  def absorb(state, _scope), do: state

  defp append_bounded(candidates, uuid) do
    (candidates ++ [uuid])
    |> Enum.take(-@max_candidates)
  end
end
