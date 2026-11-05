defmodule UnitaresSdk.LeaseReclaim do
  @moduledoc """
  Shared lease-orphan reclaim contracts for BEAM lease-plane clients (#1459, #1462).

  The surface lease plane (`:8788`) auto-renews active leases while the server
  is up, so a lease whose acquire response or release request was lost — or
  whose owning singleton supervisor restarted without running `terminate/2` —
  becomes an immortal orphan unless its legitimate owner can recognize and
  reclaim it on a later `409 held_by_other`.

  Two distinct ownership proofs exist across the fleet, and their safety rules
  must not be conflated:

    1. **Per-attempt UUID history (`:uuid_history`)** — default for resident
       tick clients (`UnitaresSentinel.LeaseAdvisory`). Every `holder_agent_uuid`
       put on the wire (both double-transport-failure attempts and successful
       acquires) is remembered in process state until a later successful
       acquire proves the surface held no prior orphan, plus a 15-minute
       `@absence_grace_ms` window for delayed in-flight duplicates. Because
       each attempt mints a fresh random UUID locally, a `409 held_by_other`
       naming a remembered `held_by_uuid` is a cryptographic authorship proof
       that THIS process created the blocking lease.

    2. **Instance-qualified singleton intent tag (`:intent_tag`)** — opt-in
       for supervised singletons (`Dispatch.Lease`) that restart with empty
       in-memory state. Because a `409` body does not carry `intent`, the
       caller fetches `GET /v1/lease/status` and passes `{status, body}` to
       `intent_reclaim_decision/2` with its instance-qualified `owner_intent`.
       Only an exact match on a non-empty `owner_intent` is reclaimable.

  ## Release reasons and 422 vintage fallback

    * `:uuid_history` releases with `"reclaimed_lost_acquire"`. If and only if
      the lease plane rejects that enum value with `422 schema_invalid` (an
      older plane vintage), `fallback_release_reason/2` returns
      `{:retry, "normal"}` so reclaim still succeeds across deploy skew.
      Transport timeouts and `503` errors deliberately return `:no_fallback`
      so an unapplied migration or DB stall never silently mislabels orphan
      hold durations as `"normal"`.
    * `:intent_tag` releases with `"reaped_after_supervisor_failed"`.
  """

  @max_candidates 4096
  @absence_grace_ms 15 * 60 * 1000

  @uuid_history_reason "reclaimed_lost_acquire"
  @intent_tag_reason "reaped_after_supervisor_failed"

  @typedoc """
  State keys merged into a caller GenServer's state for `:uuid_history`
  reclaim. Each entry is `{holder_uuid, absence_proven_at}` — `nil` until a
  successful acquire proves the uuid holds nothing on this surface.
  """
  @type state :: %{
          optional(any()) => any(),
          lease_reclaim_candidates: [{String.t(), DateTime.t() | nil}]
        }

  @type mode :: :uuid_history | :intent_tag

  @type intent_decision ::
          :already_free
          | {:reclaimable, String.t()}
          | :foreign

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

  Preserves the council-approved (#1459) retention rules:
    1. drop entries whose absence was proven more than `@absence_grace_ms`
       (15 minutes) ago — never by age alone, and never by outcome clearing;
    2. on a successful acquire (`:acquired_new` or `:acquired_idempotent`),
       stamp still-unproven entries as absence-proven now (preserving any
       earlier stamp);
    3. remember `conflict.attempted_holder_uuid` (both transport attempts
       failed) and the scope's `holder_uuid` (successful acquire) as fresh,
       unproven entries, bounded at `@max_candidates` (4096).

  Options: `:now` — injected `DateTime` clock for tests.
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
  Classify a decoded `GET /v1/lease/status` reply (`{status, body}`) against an
  instance-qualified singleton `owner_intent` string (`Dispatch.Lease` pattern).

    * `:already_free` — `200` with `%{"ok" => true, "lease" => nil}`
    * `{:reclaimable, lease_id}` — `200` with `%{"ok" => true, "lease" => %{"intent" => ^owner_intent, "lease_id" => lease_id}}`
    * `:foreign` — any other intent, blank `owner_intent`, missing `lease_id`, or non-200 reply
  """
  @spec intent_reclaim_decision({non_neg_integer(), term()}, String.t()) :: intent_decision()
  def intent_reclaim_decision({status, body}, owner_intent) when is_integer(status),
    do: intent_reclaim_decision(status, body, owner_intent)

  def intent_reclaim_decision(_reply, _owner_intent), do: :foreign

  @doc """
   3-arity form of `intent_reclaim_decision/2` taking `status`, `body`, and `owner_intent`.
  """
  @spec intent_reclaim_decision(non_neg_integer(), term(), String.t()) :: intent_decision()
  def intent_reclaim_decision(status, body, owner_intent)
      when is_integer(status) and is_binary(owner_intent) do
    case String.trim(owner_intent) do
      "" ->
        :foreign

      _non_empty ->
        case {status, body} do
          {200, %{"ok" => true, "lease" => nil}} ->
            :already_free

          {200,
           %{
             "ok" => true,
             "lease" => %{"intent" => ^owner_intent, "lease_id" => lease_id}
           }}
          when is_binary(lease_id) and lease_id != "" ->
            {:reclaimable, lease_id}

          _ ->
            :foreign
        end
    end
  end

  def intent_reclaim_decision(_status, _body, _owner_intent), do: :foreign

  @doc """
  Alias for `intent_reclaim_decision/2`.
  """
  @spec own_orphan_status?({non_neg_integer(), term()}, String.t()) :: intent_decision()
  def own_orphan_status?(reply, owner_intent),
    do: intent_reclaim_decision(reply, owner_intent)

  @doc """
  Canonical `release_reason` for a reclaim mode.
  """
  @spec release_reason(mode()) :: String.t()
  def release_reason(:uuid_history), do: @uuid_history_reason
  def release_reason(:intent_tag), do: @intent_tag_reason

  @doc """
  Ordered list of release reasons to try for a reclaim mode (primary reason
  first, followed by any 422 vintage fallback).
  """
  @spec reclaim_release_reasons(mode()) :: [String.t()]
  def reclaim_release_reasons(:uuid_history), do: [@uuid_history_reason, "normal"]
  def reclaim_release_reasons(:intent_tag), do: [@intent_tag_reason]

  @doc """
  Determine whether a failed reclaim release should fall back to `"normal"`.

  Per the #1459 council decision, ONLY a `422 schema_invalid` response from the
  lease plane (indicating an older router vintage that predates
  `"reclaimed_lost_acquire"`) triggers `{:retry, "normal"}`. Transport errors
  and `503` responses return `:no_fallback` so DB stalls and unapplied
  migrations never silently mislabel orphan spans.
  """
  @spec fallback_release_reason(mode(), term()) :: {:retry, String.t()} | :no_fallback
  def fallback_release_reason(:uuid_history, 422), do: {:retry, "normal"}

  def fallback_release_reason(
        :uuid_history,
        {:ok, %{"ok" => false, "error" => "schema_invalid"}}
      ),
      do: {:retry, "normal"}

  def fallback_release_reason(
        :uuid_history,
        {:error, {:release_refused, 422, "schema_invalid", _}}
      ),
      do: {:retry, "normal"}

  def fallback_release_reason(_mode, _failure), do: :no_fallback
end
