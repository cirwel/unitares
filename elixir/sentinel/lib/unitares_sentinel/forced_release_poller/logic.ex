defmodule UnitaresSentinel.ForcedReleasePoller.Logic do
  @moduledoc """
  Pure logic for transforming `lease_plane.lease_plane_events` rows into
  Sentinel alarms + advancing the cursor.

  Three event classes (v0.1.3 §B1 combined poller binding):
    * ad_hoc — `event_type='forced'`, one alarm per event
    * deprecation_batch — `event_type='lease.deprecation_swept'`, one
      alarm per completed batch (joined with `deprecated_schemes`)
    * conflict_batch — `event_type='conflict_held_by_other'`, one alarm
      per surface per cycle

  Cursor advances on `max(event.ts)` across ALL classes. Filter columns
  differ per class — see `ForcedReleasePoller.query_*` for the SQL side.
  v0.1.3 §B2 asymmetry: deprecation_batch FILTERS on `sweep_completed_at`,
  but cursor ADVANCES on `last_ts` (event-stream parity).

  Mirrors `_ad_hoc_alarm`, `_batch_alarm`, `_conflict_alarm` in
  `agents/sentinel/forced_release_alarm.py` for cross-runtime parity.
  Cursor never regresses (defensive against a buggy SQL filter).
  """

  @type ad_hoc_row :: %{
          required(:event_id) => binary(),
          required(:ts) => DateTime.t(),
          required(:lease_id) => binary() | nil,
          required(:surface_id) => binary(),
          required(:surface_kind) => binary()
        }

  @type deprecation_batch_row :: %{
          required(:deprecation_id) => binary(),
          required(:surface_kind) => binary(),
          required(:sweep_completed_at) => DateTime.t(),
          required(:event_count) => non_neg_integer(),
          required(:first_ts) => DateTime.t(),
          required(:last_ts) => DateTime.t()
        }

  @type conflict_batch_row :: %{
          required(:surface_id) => binary(),
          required(:surface_kind) => binary(),
          required(:event_count) => non_neg_integer(),
          required(:first_ts) => DateTime.t(),
          required(:last_ts) => DateTime.t()
        }

  # Backwards-compat alias — earlier tests reference `Logic.row` as a type.
  @type row :: ad_hoc_row()

  # Surfaces minted by the force-release contract test (a REAL force-release
  # against the live router on every `pytest` run) are test fixtures, not
  # operator-typed force-releases. Suppress them so the active Sentinel does not
  # page on every test run. MUST stay byte-equal to Python's
  # `_SUPPRESSED_TEST_SURFACE_PREFIXES`
  # (agents/sentinel/forced_release_alarm.py) for cross-runtime parity:
  #   * "td:/test/" — reserved namespace (PR #1102+)
  #   * "td:/force-release-contract-test-" — legacy pre-#1102 naming whose
  #     events still linger in lease_plane_events (governance DB forbids DELETE)
  @suppressed_test_surface_prefixes ["td:/test/", "td:/force-release-contract-test-"]

  @type alarm :: %{
          kind: String.t(),
          severity: String.t(),
          summary: String.t(),
          fingerprint: String.t(),
          extra: map()
        }

  # ---- ad_hoc ----------------------------------------------------------

  @doc """
  Build ad_hoc alarms from `rows` and advance the cursor.

  Cursor never regresses: if every row's ts is older than `prior_cursor`,
  the prior is preserved.
  """
  @spec build_alarms([ad_hoc_row()], DateTime.t() | nil) :: {[alarm()], DateTime.t() | nil}
  def build_alarms(rows, prior_cursor) when is_list(rows) do
    alarms =
      rows
      |> Enum.reject(&reserved_test_surface?(&1.surface_id))
      |> Enum.map(&row_to_ad_hoc_alarm/1)

    # Cursor advances over ALL rows (incl. suppressed test fixtures) so they
    # aren't re-scanned next cycle.
    new_cursor = advance_cursor_from(rows, :ts, prior_cursor)
    {alarms, new_cursor}
  end

  defp row_to_ad_hoc_alarm(%{event_id: event_id, surface_id: surface_id} = row) do
    lease_label = lease_label(row.lease_id)

    %{
      kind: "ad_hoc",
      severity: "high",
      summary: "forced release: #{surface_id} (lease #{lease_label})",
      fingerprint: "forced_release:ad_hoc:#{event_id}",
      extra: %{
        event_id: event_id,
        ts: DateTime.to_iso8601(row.ts),
        lease_id: row.lease_id,
        surface_id: surface_id,
        surface_kind: row.surface_kind
      }
    }
  end

  defp lease_label(nil), do: "<unknown>"
  defp lease_label(lease_id) when is_binary(lease_id), do: lease_id

  # ---- deprecation_batch -----------------------------------------------

  @doc """
  Build deprecation_batch alarms — one per completed sweep batch.

  v0.1.3 §B2 asymmetry: rows are FILTERED by `sweep_completed_at > $1`
  (SQL side), but the cursor ADVANCES on `max(last_ts)` (event-stream
  parity). Never mix event-stream and table-metadata timestamps in the
  cursor (Python PR 5 council fix at agents/sentinel/forced_release_alarm.py:137-142).
  """
  @spec build_deprecation_batch_alarms([deprecation_batch_row()], DateTime.t() | nil) ::
          {[alarm()], DateTime.t() | nil}
  def build_deprecation_batch_alarms(rows, prior_cursor) when is_list(rows) do
    alarms = Enum.map(rows, &row_to_deprecation_alarm/1)
    new_cursor = advance_cursor_from(rows, :last_ts, prior_cursor)
    {alarms, new_cursor}
  end

  defp row_to_deprecation_alarm(%{deprecation_id: depr_id, surface_kind: kind, event_count: count} = row) do
    %{
      kind: "deprecation_batch",
      severity: "medium",
      summary: "deprecation sweep complete: kind=#{kind} count=#{count}",
      fingerprint: "forced_release:deprecation_batch:#{depr_id}",
      extra: %{
        deprecation_id: depr_id,
        kind: kind,
        count: count,
        first_ts: DateTime.to_iso8601(row.first_ts),
        last_ts: DateTime.to_iso8601(row.last_ts),
        sweep_completed_at: DateTime.to_iso8601(row.sweep_completed_at)
      }
    }
  end

  # ---- conflict_batch --------------------------------------------------

  @doc """
  Build conflict_batch alarms — one per surface per cycle. Fingerprint
  includes `last_ts` so a later cycle producing more conflicts on the
  same surface yields a distinct alarm (Python parity:
  `agents/sentinel/forced_release_alarm.py:204`).
  """
  @spec build_conflict_batch_alarms([conflict_batch_row()], DateTime.t() | nil) ::
          {[alarm()], DateTime.t() | nil}
  def build_conflict_batch_alarms(rows, prior_cursor) when is_list(rows) do
    alarms =
      rows
      |> Enum.reject(&reserved_test_surface?(&1.surface_id))
      |> Enum.map(&row_to_conflict_alarm/1)

    # Cursor advances over ALL rows (incl. suppressed test fixtures).
    new_cursor = advance_cursor_from(rows, :last_ts, prior_cursor)
    {alarms, new_cursor}
  end

  defp row_to_conflict_alarm(%{surface_id: surface_id, event_count: count, last_ts: last_ts} = row) do
    %{
      kind: "conflict_batch",
      severity: "medium",
      summary: "held-by-other conflicts: #{surface_id} (count=#{count})",
      # Byte-equivalent with Python's
      # `forced_release:conflict_batch:{surface_id}:{last_ts.isoformat()}`
      # (agents/sentinel/forced_release_alarm.py:203). MUST use the Python
      # isoformat shape, not DateTime.to_iso8601/1 — the latter's "Z" suffix
      # diverges from Python's "+00:00" and breaks cross-runtime dedup across
      # the direct-flip cutover gap (RFC v0.1.1 §B2/§C3; parity audit
      # 2026-06-14 GAP 1). ad_hoc/deprecation_batch are ID-only and unaffected.
      fingerprint: "forced_release:conflict_batch:#{surface_id}:#{iso8601_python(last_ts)}",
      extra: %{
        surface_id: surface_id,
        surface_kind: row.surface_kind,
        count: count,
        first_ts: DateTime.to_iso8601(row.first_ts),
        last_ts: DateTime.to_iso8601(last_ts)
      }
    }
  end

  # Mirror Python's `datetime.isoformat()` for tz-aware UTC values so the
  # conflict_batch fingerprint is byte-equivalent across runtimes. Python
  # renders the UTC offset as "+00:00" and omits the fractional part when
  # microsecond == 0; `DateTime.to_iso8601/1` emits a "Z" suffix and carries
  # Postgrex's 6-digit microsecond precision. Postgrex returns timestamptz as
  # a UTC DateTime, so the seconds rendering always ends in "Z" before the
  # suffix swap.
  @spec iso8601_python(DateTime.t()) :: String.t()
  defp iso8601_python(%DateTime{microsecond: {micro, _precision}} = dt) do
    seconds =
      %{dt | microsecond: {0, 0}}
      |> DateTime.to_iso8601()
      |> String.replace_suffix("Z", "")

    frac =
      if micro == 0 do
        ""
      else
        "." <> String.pad_leading(Integer.to_string(micro), 6, "0")
      end

    seconds <> frac <> "+00:00"
  end

  # ---- combined --------------------------------------------------------

  @doc """
  Build alarms from all three classes, advance cursor to max across all.

  Composes the three per-class builders. The combined cursor is
  `max(ad_hoc_cursor, deprecation_cursor, conflict_cursor, prior_cursor)`
  — defensive against a buggy SQL filter that lets older rows through
  in any one class.
  """
  @spec build_all_alarms(
          [ad_hoc_row()],
          [deprecation_batch_row()],
          [conflict_batch_row()],
          DateTime.t() | nil
        ) :: {[alarm()], DateTime.t() | nil}
  def build_all_alarms(ad_hoc_rows, deprecation_rows, conflict_rows, prior_cursor) do
    {ad_hoc_alarms, c1} = build_alarms(ad_hoc_rows, prior_cursor)
    {deprecation_alarms, c2} = build_deprecation_batch_alarms(deprecation_rows, prior_cursor)
    {conflict_alarms, c3} = build_conflict_batch_alarms(conflict_rows, prior_cursor)

    combined = ad_hoc_alarms ++ deprecation_alarms ++ conflict_alarms
    new_cursor = max_of([c1, c2, c3, prior_cursor])

    {combined, new_cursor}
  end

  defp max_of(cursors) do
    cursors
    |> Enum.reject(&is_nil/1)
    |> case do
      [] -> nil
      list -> Enum.max(list, DateTime)
    end
  end

  # ---- internals -------------------------------------------------------

  # True when `surface_id` is a force-release contract test fixture (reserved
  # or legacy prefix). Mirrors Python's `_is_reserved_test_surface`.
  defp reserved_test_surface?(surface_id) when is_binary(surface_id) do
    Enum.any?(@suppressed_test_surface_prefixes, &String.starts_with?(surface_id, &1))
  end

  defp reserved_test_surface?(_), do: false

  defp advance_cursor_from([], _key, prior), do: prior

  defp advance_cursor_from(rows, key, prior) do
    max_ts = rows |> Enum.map(&Map.fetch!(&1, key)) |> Enum.max(DateTime)

    cond do
      is_nil(prior) -> max_ts
      DateTime.compare(max_ts, prior) == :gt -> max_ts
      true -> prior
    end
  end
end
