defmodule UnitaresSdk.LeaseReclaim do
  @moduledoc """
  Shared per-resident memory of the holder uuids a process has recently put on
  the wire, so a later `held_by_other` that names one of them can be recognized
  as this resident's own stranded lease and released (#1459, #1462).

  ## The gap this closes (2026-08-01 incident)

  `UnitaresSentinel.LeaseAdvisory.post_acquire_with_recovery/4` resolves a
  single lost acquire response by retrying once with the same body — idempotent
  re-acquire on `holder_agent_uuid`. On 2026-08-01 15:42 BOTH attempts timed out
  at the transport (a Postgres stall slowed the plane past the 2s client budget
  while the first attempt's INSERT had already committed), the attempt's holder
  uuid was discarded, and every subsequent tick minted a fresh uuid that saw
  only `held_by_other`. The poller was starved for 1h49m (216 ticks) until an
  operator force-released the lease — even though every one of those 409
  responses named both the orphan's `held_by_uuid` (a uuid this process minted)
  and the `blocking_lease_id` needed to free it.

  ## What is remembered

  Every holder uuid this process sends is remembered:

    * an acquire whose both transport attempts failed contributes its
      `attempted_holder_uuid` — that uuid MAY own a committed lease this
      process never learned about;
    * a SUCCESSFUL acquire contributes its `holder_uuid` too — if the eventual
      release request is lost, the lease plane auto-renews that lease forever,
      and the only way to recognize the resulting orphan is to still remember
      the uuid that acquired it (an end-of-tick `release/2` is often
      fire-and-forget, so one lost release message is strictly MORE probable
      than the double-loss that motivated this module).

  ## When an entry may be forgotten

  Only after its lease is PROVEN absent, plus a grace window — never by age
  alone, and never by outcome-based clearing:

    * An orphan lives unboundedly (the plane-side holder auto-renews forever),
      so a uuid that might hold one must be remembered for as long as the
      starvation could persist. Pure age expiry would forget the
      stall-opening uuid during any stall longer than the window — the one
      uuid whose INSERT committed.
    * A successful acquire proves that, at that instant, no remembered uuid
      holds this surface (one active lease per surface). It does NOT prove
      one cannot appear later: a delayed duplicate request (e.g. the PR #1443
      recovery retry executing server-side late) can still commit afterwards.
      So a success stamps existing entries as absence-proven, and a stamped
      entry survives a further `@absence_grace_ms` — comfortably beyond any
      abandoned request's plausible server-side lifetime — before it is
      dropped.

  The list is bounded (`@max_candidates`) purely as a backstop; entries are a
  few dozen bytes, and the cap is sized to outlast a day-long transport-dark
  stall at the default 30s tick interval.

  In-memory only: a resident restart forfeits reclaim for leases stranded
  before the restart. That residual is carried by the doctor's
  `immortal_lease` check (scheduled via `com.unitares.doctor-findings`) and
  the `LeaseStarvation` self-finding, whose remedy names the blocking lease id
  for a manual force-release.

  ## Safety

  Holder uuids are minted process-locally from `:crypto.strong_rand_bytes/1`
  (`LeaseAdvisory.new_holder_uuid/0`). A match between `held_by_uuid` and a
  remembered uuid therefore proves the blocking lease was created by an
  acquire THIS process sent — releasing it can never take a lease away from
  another live holder. (This is a fleet convention, not a server-enforced
  property: the plane does not authenticate holder uuids, and every 409
  publishes the holder uuid to every bearer client. It holds because every
  client mints uuids randomly per attempt and none echoes observed uuids back
  into acquire — verified fleet-wide at review time.) This is deliberately NOT
  the rejected stable-holder-uuid design: uuids stay per-attempt, so two
  concurrently live residents still contend correctly and can never adopt each
  other's leases.

  ## Release reason and 422 vintage fallback

  Reclaim releases use `"reclaimed_lost_acquire"` (`release_reason/0`). If and
  only if the lease plane rejects that enum value with HTTP `422` and
  `"error" => "schema_invalid"` (an older plane router vintage),
  `fallback_release_reason/2` returns `{:retry, "normal"}` so reclaim still
  succeeds across deploy skew. Transport timeouts, `503` responses, and any
  non-422 errors return `:no_fallback` so DB stalls and unapplied migrations
  never silently mislabel orphan hold durations as `"normal"`.
  """

  @max_candidates 4096
  @absence_grace_ms 15 * 60 * 1000
  @uuid_history_reason "reclaimed_lost_acquire"

  @typedoc """
  State keys this module merges into a caller GenServer's state. Each entry is
  `{holder_uuid, absence_proven_at}` — `nil` until a successful acquire proves
  the uuid holds nothing on this surface.
  """
  @type state :: %{
          optional(any()) => any(),
          lease_reclaim_candidates: [{String.t(), DateTime.t() | nil}]
        }

  @type mode :: :uuid_history

  @doc """
  Fresh reclaim state to `Map.merge/2` into a GenServer's state at `init/1`.
  """
  @spec new() :: %{lease_reclaim_candidates: []}
  def new, do: %{lease_reclaim_candidates: []}

  @doc """
  Options to merge into acquire opts so the client can recognize a conflict
  that names one of this process's own recent holder uuids.
  """
  @spec acquire_opts(map()) :: keyword()
  def acquire_opts(%{lease_reclaim_candidates: candidates}) when is_list(candidates),
    do: [reclaim_candidates: Enum.map(candidates, &elem(&1, 0))]

  def acquire_opts(_state), do: []

  @doc """
  True when `held_by_uuid` matches one of the caller's remembered candidate
  holder UUIDs (accepts a GenServer state map, acquire keyword opts with
  `:reclaim_candidates`, or a candidate list of UUID strings / `{uuid, _}`
  tuples).
  """
  @spec own_orphan_uuid?(term(), term()) :: boolean()
  def own_orphan_uuid?(held_by_uuid, %{lease_reclaim_candidates: candidates})
      when is_binary(held_by_uuid) and is_list(candidates) do
    own_orphan_uuid?(held_by_uuid, candidates)
  end

  def own_orphan_uuid?(held_by_uuid, candidates)
      when is_binary(held_by_uuid) and held_by_uuid != "" and is_list(candidates) do
    list =
      if Keyword.keyword?(candidates) and Keyword.has_key?(candidates, :reclaim_candidates) do
        Keyword.get(candidates, :reclaim_candidates, [])
      else
        candidates
      end

    Enum.any?(list, fn
      {uuid, _proven_at} when is_binary(uuid) -> uuid == held_by_uuid
      uuid when is_binary(uuid) -> uuid == held_by_uuid
      _ -> false
    end)
  end

  def own_orphan_uuid?(_held_by_uuid, _candidates), do: false

  @doc """
  Update reclaim memory from an acquire scope.

  Call with every scope `LeaseAdvisory.acquire_cycle/1` returns, whatever its
  outcome:

    1. drop entries whose absence was proven more than the grace window ago;
    2. on a successful acquire, stamp still-unproven entries as
       absence-proven now (the new acquire's own uuid is exempt — its lease
       is the one currently active);
    3. remember `conflict.attempted_holder_uuid` (both transport attempts
       failed) and the scope's `holder_uuid` (successful acquire) as fresh,
       unproven entries.

  Options: `:now` — injected clock for tests.
  """
  @spec absorb(map(), map(), keyword()) :: map()
  def absorb(state, scope, opts \\ [])

  def absorb(%{lease_reclaim_candidates: candidates} = state, %{} = scope, opts) do
    now = Keyword.get(opts, :now) || DateTime.utc_now()
    conflict = Map.get(scope, :conflict) || %{}
    acquired? = Map.get(scope, :outcome) in [:acquired_new, :acquired_idempotent]

    fresh =
      [Map.get(conflict, :attempted_holder_uuid), Map.get(scope, :holder_uuid)]
      |> Enum.filter(&is_binary/1)
      |> Enum.map(&{&1, nil})

    survivors =
      Enum.reject(candidates, fn {_uuid, proven_at} ->
        is_struct(proven_at, DateTime) and
          DateTime.diff(now, proven_at, :millisecond) > @absence_grace_ms
      end)

    survivors =
      if acquired? do
        Enum.map(survivors, fn
          {uuid, nil} -> {uuid, now}
          entry -> entry
        end)
      else
        survivors
      end

    next = (survivors ++ fresh) |> Enum.take(-@max_candidates)

    %{state | lease_reclaim_candidates: next}
  end

  def absorb(state, _scope, _opts), do: state

  @doc """
  Canonical `release_reason` for a UUID-history reclaim release.
  """
  @spec release_reason() :: String.t()
  def release_reason, do: @uuid_history_reason

  @spec release_reason(mode()) :: String.t()
  def release_reason(:uuid_history), do: @uuid_history_reason

  @doc """
  Determine whether a failed reclaim release should fall back to `"normal"`.

  Per the #1459 council decision, ONLY an HTTP `422` response carrying
  `"error" => "schema_invalid"` from the lease plane (indicating an older
  router vintage that predates `"reclaimed_lost_acquire"`) triggers
  `{:retry, "normal"}`. Transport errors, `503` responses, non-422 status
  codes, and `422` errors for any other reason return `:no_fallback` so DB
  stalls and unapplied migrations never silently mislabel orphan spans.
  """
  @spec fallback_release_reason(term()) :: {:retry, String.t()} | :no_fallback
  def fallback_release_reason(reply), do: fallback_release_reason(:uuid_history, reply)

  @spec fallback_release_reason(mode(), term()) :: {:retry, String.t()} | :no_fallback
  def fallback_release_reason(
        :uuid_history,
        {:ok, 422, %{"ok" => false, "error" => "schema_invalid"}}
      ),
      do: {:retry, "normal"}

  def fallback_release_reason(
        :uuid_history,
        {:error, {:release_refused, 422, "schema_invalid", _}}
      ),
      do: {:retry, "normal"}

  def fallback_release_reason(_mode, _failure), do: :no_fallback
end
