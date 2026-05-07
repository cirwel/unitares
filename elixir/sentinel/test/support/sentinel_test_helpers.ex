defmodule SentinelTestHelpers do
  @moduledoc """
  Test fixtures + cleanup for `:db`-tagged integration tests.

  All fixtures use a unique `surface_id` prefix derived from a per-test
  random label so they cannot collide with concurrent tests or production
  rows. Cleanup deletes by exactly that prefix.

  Mirrors `LeaseTestHelpers` in `elixir/lease_plane/test/support/`.
  """

  alias UnitaresSentinel.DB

  @doc "Random hex label suitable as a fixture-isolation suffix."
  def random_label do
    :crypto.strong_rand_bytes(6) |> Base.encode16(case: :lower)
  end

  @doc """
  Generate a unique surface_id for a single test using the
  `dialectic:/test_sentinel_<label>_<rand>` canonical scheme so the
  surface_id passes migration 026's `surface_id_grammar` CHECK.
  """
  def unique_surface_id(label) when is_binary(label) do
    "dialectic:/test_sentinel_#{label}_#{random_label()}"
  end

  @doc "Stable random UUID-as-string for holder_agent_uuid in fixtures."
  def random_uuid do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)
    parts = [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    parts |> Enum.map_join("-", &Base.encode16(&1, case: :lower))
  end

  @doc """
  Insert one `event_type='forced'` row into `lease_plane.lease_plane_events`
  with the given surface_id and ts. Returns the inserted event_id (uuid string).
  """
  def insert_forced_event(surface_id, ts \\ nil) do
    sql = """
    INSERT INTO lease_plane.lease_plane_events
      (ts, event_type, lease_id, surface_id, surface_kind, advisory_mode, payload)
    VALUES (COALESCE($1::timestamptz, now()), 'forced', gen_random_uuid(), $2, $3, true, '{}'::jsonb)
    RETURNING event_id::text, ts
    """

    {:ok, %{rows: [[event_id, returned_ts]]}} =
      Postgrex.query(DB, sql, [ts, surface_id, surface_kind_of(surface_id)])

    {event_id, returned_ts}
  end

  @doc """
  Insert one `event_type='conflict_held_by_other'` row.
  """
  def insert_conflict_event(surface_id, ts \\ nil) do
    sql = """
    INSERT INTO lease_plane.lease_plane_events
      (ts, event_type, lease_id, surface_id, surface_kind, advisory_mode, payload)
    VALUES (COALESCE($1::timestamptz, now()), 'conflict_held_by_other', gen_random_uuid(), $2, $3, true, '{}'::jsonb)
    RETURNING event_id::text, ts
    """

    {:ok, %{rows: [[event_id, returned_ts]]}} =
      Postgrex.query(DB, sql, [ts, surface_id, surface_kind_of(surface_id)])

    {event_id, returned_ts}
  end

  @doc """
  Insert one `event_type='lease.deprecation_swept'` row with payload
  carrying `deprecation_id` (text). Caller is responsible for ensuring
  the corresponding `deprecated_schemes` row exists with a
  non-NULL `sweep_completed_at`.
  """
  def insert_deprecation_swept_event(surface_id, deprecation_id, ts \\ nil) do
    sql = """
    INSERT INTO lease_plane.lease_plane_events
      (ts, event_type, lease_id, surface_id, surface_kind, advisory_mode, payload)
    VALUES (
      COALESCE($1::timestamptz, now()),
      'lease.deprecation_swept',
      gen_random_uuid(),
      $2, $3, true,
      jsonb_build_object('deprecation_id', $4::text)
    )
    RETURNING event_id::text, ts
    """

    {:ok, %{rows: [[event_id, returned_ts]]}} =
      Postgrex.query(DB, sql, [ts, surface_id, surface_kind_of(surface_id), deprecation_id])

    {event_id, returned_ts}
  end

  @doc "DELETE all lease_plane_events rows for surface_ids beginning with the prefix."
  def cleanup_surface_prefix(prefix) when is_binary(prefix) do
    Postgrex.query!(
      DB,
      "DELETE FROM lease_plane.lease_plane_events WHERE surface_id LIKE $1",
      [prefix <> "%"]
    )

    :ok
  end

  defp surface_kind_of("dialectic:/" <> _), do: "dialectic"
  defp surface_kind_of(other), do: other |> String.split(":", parts: 2) |> hd()
end
