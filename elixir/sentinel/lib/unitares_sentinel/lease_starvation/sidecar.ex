defmodule UnitaresSentinel.LeaseStarvation.Sidecar do
  @moduledoc """
  Finding construction, summary rendering, and single-writer sidecar
  persistence for `UnitaresSentinel.LeaseStarvation`.
  """

  alias UnitaresSentinel.{AtomicWrite, CycleState}

  require Logger

  @max_closure_retries 5
  @schema_version 2
  @finding_type "sentinel_lease_starved"
  @cleared_finding_type "sentinel_lease_starvation_cleared"
  @starved_severity "high"
  @default_lease_plane_base_url "http://127.0.0.1:8788"
  @persisted_conflict_keys [:blocking_lease_id, :held_by_uuid, :expires_at]

  @doc false
  def max_closure_retries, do: @max_closure_retries

  @doc false
  def persisted_conflict_keys, do: @persisted_conflict_keys

  @doc false
  @spec finding(map(), DateTime.t(), pos_integer()) :: map()
  def finding(tracker, now, multiple) do
    seconds = blocked_seconds(tracker, now)
    conflict = tracker.lease_blocked_last_conflict || %{}
    blocking_lease_id = Map.get(conflict, :blocking_lease_id)

    %{
      type: @finding_type,
      severity: @starved_severity,
      violation_class: "BEH",
      # Keyed on the SURFACE, not on agent_id. Without this the two residents
      # get distinct fingerprints only by accident — the emitter carries the
      # anchor agent_uuid (`Application.maybe_add_self_agent_id/1`) while the
      # poller, started as a bare module atom, falls back to the literal
      # "sentinel". Two residents on one agent_id would dedup one outage into
      # the other's.
      fingerprint_extra: [tracker.lease_blocked_surface_id],
      change_token: change_token(tracker.lease_blocked_since, multiple),
      summary: starved_summary(tracker, seconds, blocking_lease_id),
      extra:
        %{
          # `handle_checkin_pause/3` sets this as a top-level key, where
          # `finding_body/2` has always silently dropped it. Routed through
          # `:extra` so it actually ships.
          self_observation: true,
          surface_id: tracker.lease_blocked_surface_id,
          resident: tracker.lease_blocked_resident,
          blocked_since: DateTime.to_iso8601(tracker.lease_blocked_since),
          blocked_seconds: seconds,
          blocked_ticks_this_process: tracker.lease_blocked_streak,
          escalation_multiple: multiple,
          alert_after_seconds: tracker.lease_blocked_alert_after_seconds,
          lease_outcome_counts: stringify_counts(tracker.lease_blocked_outcome_counts)
        }
        |> maybe_put(:blocking_lease_id, blocking_lease_id)
        |> maybe_put(:held_by_uuid, Map.get(conflict, :held_by_uuid))
        |> maybe_put(:lease_expires_at, Map.get(conflict, :expires_at))
    }
  end

  @doc false
  @spec cleared_finding(map(), DateTime.t()) :: map()
  def cleared_finding(tracker, now), do: closure_finding(closure_episode(tracker, now))

  # Freeze an ending episode into the minimal set of facts the closure finding
  # is a pure function of. Everything downstream — the in-memory debt, the
  # sidecar record, the retried POST — works from THIS, so there is exactly one
  # description of the episode and nothing to keep in sync.
  @doc false
  @spec closure_episode(map(), DateTime.t()) :: map()
  def closure_episode(tracker, now) do
    %{
      surface_id: tracker.lease_blocked_surface_id,
      resident: tracker.lease_blocked_resident,
      since: tracker.lease_blocked_since,
      ended_at: now,
      ticks: tracker.lease_blocked_streak,
      counts: stringify_counts(tracker.lease_blocked_outcome_counts)
    }
  end

  @doc false
  @spec closure_finding(map()) :: map()
  def closure_finding(episode) do
    # `ended_at - since`, never `now - since`: the duration belongs to the
    # episode, not to whenever the POST finally lands.
    seconds = max(DateTime.diff(episode.ended_at, episode.since, :second), 0)

    %{
      type: @cleared_finding_type,
      severity: "info",
      violation_class: "BEH",
      fingerprint_extra: [episode.surface_id],
      change_token: change_token(episode.since, "cleared"),
      summary:
        "Sentinel resident #{episode.resident} lease starvation CLEARED: surface " <>
          "#{episode.surface_id} acquired its lease again after " <>
          "#{format_duration(seconds)} dark (#{tick_phrase(episode.ticks)} observed by this " <>
          "process). Closes the sentinel_lease_starved finding for the episode " <>
          "that started #{DateTime.to_iso8601(episode.since)}.",
      extra: %{
        self_observation: true,
        surface_id: episode.surface_id,
        resident: episode.resident,
        blocked_since: DateTime.to_iso8601(episode.since),
        blocked_seconds: seconds,
        blocked_ticks_this_process: episode.ticks,
        lease_outcome_counts: episode.counts
      }
    }
  end

  # ---- summaries ---------------------------------------------------------

  # Scoped to this PROCESS, not to the lease plane. The earlier wording ("The
  # lease plane reported NO blocking lease at any point in this episode") is an
  # assertion about the whole episode that this process is not in a position to
  # make: an episode resumed from the sidecar after a restart carries its clock
  # and its ladder, and — since a review — its sticky blocker, but a sidecar that
  # is absent, unreadable or from an older schema restores no conflict at all.
  # If the first blocked tick after such a restart is one of the
  # `service_unavailable` bursts and a rung is already due, the old sentence told
  # the operator there was nothing to force-release while an immortal lease was
  # in fact holding the surface — the "worse than saying nothing" outcome the
  # sticky-blocker design exists to prevent. `tick_phrase/1` was already careful
  # this way; this sentence now is too.
  defp starved_summary(tracker, seconds, nil) do
    preamble(tracker, seconds) <>
      " No blocking lease was observed by THIS PROCESS during the episode (a restart mid-episode " <>
      "resumes the clock, not necessarily the observations that preceded it), so this resident " <>
      "has nothing to force-release by name: check the lease plane is up on " <>
      "$LEASE_PLANE_BASE_URL (default #{@default_lease_plane_base_url}), that " <>
      "LEASE_PLANE_BEARER_TOKEN is set for this resident, and query the plane directly for a " <>
      "lease on this surface before concluding there is none."
  end

  defp starved_summary(tracker, seconds, lease_id) do
    conflict = tracker.lease_blocked_last_conflict || %{}

    held_by =
      case Map.get(conflict, :held_by_uuid) do
        uuid when is_binary(uuid) and uuid != "" -> " held_by=#{uuid}"
        _ -> ""
      end

    preamble(tracker, seconds) <>
      " Most recent blocking lease_id=#{lease_id}#{held_by}. If that holder is an immortal lease " <>
      "(holder_pid NULL, auto-renewing, expires_at never in the past), clear it: " <>
      ~s|POST $LEASE_PLANE_BASE_URL/v1/lease/force-release {"lease_id": "#{lease_id}"} | <>
      "(default base #{@default_lease_plane_base_url})."
  end

  defp preamble(tracker, seconds) do
    "Sentinel resident #{tracker.lease_blocked_resident} is LEASE-STARVED: surface " <>
      "#{tracker.lease_blocked_surface_id} has been refused by lease enforcement for " <>
      "#{format_duration(seconds)} (since #{DateTime.to_iso8601(tracker.lease_blocked_since)}; " <>
      "#{tick_phrase(tracker.lease_blocked_streak)} observed by this process; " <>
      "#{outcome_phrase(tracker)}). This resident is doing NO governance work while its OS " <>
      "process, launchd job and liveness checks all read healthy."
  end

  # The streak is honestly per-process: a resident restarted mid-episode resumes
  # the episode (and its ladder) but counts its own ticks from zero. Say
  # "observed by this process" rather than implying a total. Takes the count and
  # not a tracker, because a closure re-rendered from a persisted debt reports
  # the count the ENDED episode had, not this process's.
  defp tick_phrase(1), do: "1 blocked tick"
  defp tick_phrase(n), do: "#{n} blocked ticks"

  defp outcome_phrase(%{lease_blocked_outcome_counts: counts}) when map_size(counts) == 0,
    do: "outcomes: none recorded"

  defp outcome_phrase(%{lease_blocked_outcome_counts: counts}) do
    rendered =
      counts
      |> Enum.sort_by(fn {outcome, count} -> {-count, to_string(outcome)} end)
      |> Enum.map_join(", ", fn {outcome, count} -> "#{outcome}=#{count}" end)

    "outcomes: " <> rendered
  end

  @doc false
  @spec format_duration(non_neg_integer()) :: String.t()
  def format_duration(seconds) when seconds < 60, do: "#{seconds}s"
  def format_duration(seconds) when seconds < 3600, do: "#{div(seconds, 60)}m"

  def format_duration(seconds),
    do: "#{div(seconds, 3600)}h#{div(rem(seconds, 3600), 60)}m"

  # The blocking lease id is deliberately NOT part of the token. A genuinely
  # contended surface with per-tick holder churn would otherwise emit on every
  # tick, reintroducing exactly the spam this ladder exists to avoid. The
  # re-emissions pick up the CURRENT holder anyway, because the summary is
  # rendered at emit time.
  defp change_token(%DateTime{} = since, suffix),
    do: "#{DateTime.to_iso8601(since)}|#{suffix}"

  @doc false
  def blocked_seconds(%{lease_blocked_since: nil}, _now), do: 0

  def blocked_seconds(%{lease_blocked_since: since}, now),
    do: max(DateTime.diff(now, since, :second), 0)

  defp stringify_counts(counts), do: Map.new(counts, fn {k, v} -> {to_string(k), v} end)

  # ---- persistence -------------------------------------------------------
  #
  # Single-writer sidecar, one file per surface, derived from the Sentinel state
  # file path so no new config key is introduced. NOT the shared
  # `.sentinel_state.beam` shadow file: that one is load-modify-save and is
  # already written by ForcedReleasePoller's cursor advance, so two residents
  # writing starvation state into it could interleave and clobber the cursor.
  # Per-surface files keep every writer alone with its own file.

  @doc false
  def resolve_state_path(false, _surface_id), do: nil
  def resolve_state_path(path, _surface_id) when is_binary(path), do: path
  def resolve_state_path(_derive, surface_id), do: derive_state_path(surface_id)

  defp derive_state_path(surface_id) do
    CycleState.resolve_canonical_path() <> ".lease_starvation." <> slug(surface_id)
  rescue
    # STATE_FILE unset (tests, ad-hoc runs): persistence is simply off. Never a
    # reason to fail a tick.
    _ -> nil
  end

  defp slug(surface_id), do: String.replace(surface_id, ~r/[^A-Za-z0-9]+/, "_")

  @doc false
  def persist(%{lease_blocked_state_path: nil}), do: :ok

  def persist(tracker) do
    payload =
      %{
        "schema_version" => @schema_version,
        "surface_id" => tracker.lease_blocked_surface_id,
        "resident" => tracker.lease_blocked_resident
      }
      |> Map.merge(episode_payload(tracker))
      # An owed closure is orthogonal to the episode phase: it may be the ONLY
      # thing in the file ("closing"), or it may ride alongside a newer episode
      # that opened while it was still undelivered ("open"). Both are real and
      # both used to be unrepresentable, which is how one got destroyed by the
      # other.
      |> maybe_put("owed_closure", encode_owed_closure(tracker.lease_blocked_pending_closure))

    AtomicWrite.write(tracker.lease_blocked_state_path, Jason.encode!(payload))
    :ok
  rescue
    e ->
      Logger.debug("LeaseStarvation.persist failed: #{inspect(e)}")
      :ok
  end

  # "closing" carries no live episode fields on purpose: there is no clock still
  # running, and a reader that finds none cannot accidentally restart one.
  defp episode_payload(%{lease_blocked_since: nil}), do: %{"state" => "closing"}

  defp episode_payload(tracker) do
    %{
      "state" => "open",
      "blocked_since" => DateTime.to_iso8601(tracker.lease_blocked_since),
      "last_blocked_at" => DateTime.to_iso8601(tracker.lease_blocked_last_blocked_at),
      "last_emitted_multiple" => tracker.lease_blocked_last_emitted_multiple
    }
    # The sticky blocker rides along. Without it a resumed episode started with
    # `last_conflict: nil`, so a restart mid-episode could make the very next
    # finding assert that the lease plane had named no blocking lease — while
    # an immortal lease held the surface. The episode's most load-bearing fact
    # is the one thing the sidecar used to drop.
    |> maybe_put("last_conflict", encode_conflict(tracker.lease_blocked_last_conflict))
  end

  # `blocked_seconds` is deliberately NOT written: it is `ended_at - since`, and
  # a stored copy is one more thing that can disagree with the pair it is derived
  # from. Same reason `surface_id` / `resident` are read back off the top level.
  defp encode_owed_closure(nil), do: nil

  defp encode_owed_closure(%{episode: episode, attempts: attempts}) do
    %{
      "blocked_since" => DateTime.to_iso8601(episode.since),
      "ended_at" => DateTime.to_iso8601(episode.ended_at),
      "blocked_ticks" => episode.ticks,
      "outcome_counts" => episode.counts,
      "attempts" => attempts
    }
  end

  @doc false
  def discard_persisted(%{lease_blocked_state_path: nil}), do: :ok

  def discard_persisted(tracker) do
    _ = File.rm(tracker.lease_blocked_state_path)
    :ok
  end

  # Read the sidecar and restore whatever it says is true: an open episode, an
  # owed closure, both, or neither. Every failure mode — missing file, bad JSON,
  # another writer, a schema version this build does not know, a stale record —
  # lands on "nothing restored". Never crashes: `init/1` calls this.
  @doc false
  def load_episode(nil, _surface_id, _resident, _alert_after_seconds, _now), do: nil

  def load_episode(path, surface_id, resident, alert_after_seconds, now) do
    with {:ok, raw} <- File.read(path),
         {:ok, %{} = decoded} <- Jason.decode(raw),
         true <- same_writer?(decoded, surface_id, resident) do
      decoded
      |> load_state(episode_state(decoded), surface_id, resident, alert_after_seconds, now)
      |> presence()
    else
      _ -> nil
    end
  rescue
    _ -> nil
  end

  # A HEAD-era file and a current-era file both exist on real disks, and neither
  # carries a version key. Reading them as "open" is not a guess: it is exactly
  # what they meant, because before the explicit state existed the sidecar was
  # deleted unconditionally the moment an episode ended, so every v1 file on
  # disk IS an in-progress episode. (The narrow exception is a file left by the
  # intermediate build that retained a sidecar on a lost closure without marking
  # it — never released, so at most one dev tree, and the resume window bounds
  # even that to one threshold of over-patience rather than a fabricated multi-
  # day outage.)
  defp episode_state(%{"schema_version" => @schema_version} = decoded),
    do: normalize_state(Map.get(decoded, "state"))

  defp episode_state(decoded) when not is_map_key(decoded, "schema_version"), do: "open"

  # A version from the future. Do not try to interpret its fields.
  defp episode_state(_decoded), do: nil

  defp normalize_state(state) when state in ["open", "closing"], do: state
  defp normalize_state(_state), do: nil

  defp load_state(decoded, "open", surface_id, resident, alert_after_seconds, now) do
    decoded
    |> resume_open_episode(alert_after_seconds, now)
    |> merge_owed_closure(decoded, surface_id, resident, alert_after_seconds, now)
  end

  # The whole point of the state field: a closed episode is NOT resumed. Its
  # clock does not restart, its ladder position is gone, and a later blocked tick
  # opens a fresh episode at rung 1 with an honest duration. Only the debt
  # survives.
  defp load_state(decoded, "closing", surface_id, resident, alert_after_seconds, now),
    do: merge_owed_closure(%{}, decoded, surface_id, resident, alert_after_seconds, now)

  defp load_state(_decoded, _unknown_state, _surface_id, _resident, _alert_after_seconds, _now),
    do: %{}

  # Resume an episode only if the process was down for less than the alert
  # threshold. A longer gap means a fresh episode would reach the threshold in
  # the same time anyway, and resuming a days-old file would make the very first
  # blocked tick claim a multi-day outage that never happened. A stale episode
  # does not veto a still-live owed closure: they are independent records.
  defp resume_open_episode(decoded, alert_after_seconds, now) do
    with {:ok, since, _} <- parse_datetime(Map.get(decoded, "blocked_since")),
         {:ok, last_blocked_at, _} <- parse_datetime(Map.get(decoded, "last_blocked_at")),
         true <- DateTime.diff(now, last_blocked_at, :second) <= alert_after_seconds do
      %{
        lease_blocked_since: since,
        lease_blocked_last_blocked_at: last_blocked_at,
        lease_blocked_last_conflict: decode_conflict(Map.get(decoded, "last_conflict")),
        lease_blocked_last_emitted_multiple:
          non_neg_integer(Map.get(decoded, "last_emitted_multiple"))
      }
    else
      _ -> %{}
    end
  end

  defp merge_owed_closure(acc, decoded, surface_id, resident, alert_after_seconds, now) do
    case decode_owed_closure(
           Map.get(decoded, "owed_closure"),
           surface_id,
           resident,
           alert_after_seconds,
           now
         ) do
      nil -> acc
      pending -> Map.put(acc, :lease_blocked_pending_closure, pending)
    end
  end

  # Both bounds that survive a restart live here: the attempt budget (so the
  # cap is not reset by the crash it is most likely to follow) and staleness
  # against `ended_at` (so a sidecar found after a long downtime is not posted
  # as if the surface had just recovered).
  defp decode_owed_closure(raw, surface_id, resident, alert_after_seconds, now)
       when is_map(raw) do
    with {:ok, since, _} <- parse_datetime(Map.get(raw, "blocked_since")),
         {:ok, ended_at, _} <- parse_datetime(Map.get(raw, "ended_at")),
         attempts when attempts < @max_closure_retries <-
           non_neg_integer(Map.get(raw, "attempts")),
         true <- DateTime.diff(now, ended_at, :second) <= alert_after_seconds,
         true <- DateTime.compare(ended_at, since) != :lt do
      %{
        episode: %{
          surface_id: surface_id,
          resident: resident,
          since: since,
          ended_at: ended_at,
          ticks: non_neg_integer(Map.get(raw, "blocked_ticks")),
          counts: decode_counts(Map.get(raw, "outcome_counts"))
        },
        attempts: attempts
      }
    else
      _ -> nil
    end
  end

  defp decode_owed_closure(_raw, _surface_id, _resident, _alert_after_seconds, _now), do: nil

  # Left as string keys deliberately. These are display-only (the finding
  # stringifies them anyway) and never merge back into
  # `:lease_blocked_outcome_counts`, so a sidecar cannot seed the atom table.
  defp decode_counts(raw) when is_map(raw) do
    for {outcome, count} <- raw,
        is_binary(outcome),
        is_integer(count),
        count > 0,
        into: %{},
        do: {outcome, count}
  end

  defp decode_counts(_raw), do: %{}

  defp presence(loaded) when map_size(loaded) == 0, do: nil
  defp presence(loaded), do: loaded

  # `persist/1` has always written these two keys and nothing ever read them.
  # They are the natural guard, because `slug/1` collapses every non-alphanumeric
  # run to "_": `resident:/sentinel_cycle` and `resident.sentinel-cycle` derive
  # the SAME filename, and resuming another writer's episode would put one
  # resident's outage clock on the other's ladder. Fails closed — a mismatch
  # starts a fresh episode, which is at worst one threshold of extra patience and
  # never a fabricated outage.
  defp same_writer?(decoded, surface_id, resident) do
    Map.get(decoded, "surface_id") == surface_id and Map.get(decoded, "resident") == resident
  end

  defp encode_conflict(conflict) when is_map(conflict) and map_size(conflict) > 0,
    do: Map.new(conflict, fn {key, value} -> {Atom.to_string(key), value} end)

  defp encode_conflict(_conflict), do: nil

  # Only a conflict that names a lease is worth restoring: `sticky_conflict/2`
  # only ever stores one, and `finding/3` selects the force-release summary on
  # exactly that key.
  defp decode_conflict(%{"blocking_lease_id" => lease_id} = raw)
       when is_binary(lease_id) and lease_id != "" do
    Enum.reduce(@persisted_conflict_keys, %{}, fn key, acc ->
      case Map.get(raw, Atom.to_string(key)) do
        value when is_binary(value) and value != "" -> Map.put(acc, key, value)
        _ -> acc
      end
    end)
  end

  defp decode_conflict(_raw), do: nil

  defp parse_datetime(value) when is_binary(value), do: DateTime.from_iso8601(value)
  defp parse_datetime(_value), do: :error

  defp non_neg_integer(value) when is_integer(value) and value >= 0, do: value
  defp non_neg_integer(_value), do: 0

  defp maybe_put(map, _key, nil), do: map
  defp maybe_put(map, key, value), do: Map.put(map, key, value)
end
