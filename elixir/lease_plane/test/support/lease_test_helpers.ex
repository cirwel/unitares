defmodule LeaseTestHelpers do
  @moduledoc """
  Test fixtures + cleanup. Tests use distinct `surface_id` values prefixed
  with `test:elixir/<random>` so they cannot collide with real workloads or
  each other.
  """

  alias UnitaresLeasePlane.DB

  @doc """
  Generate a unique surface_id for a single test.

  Uses the `dialectic:/` canonical scheme (RFC v0.8 §7.2.1) so the surface_id
  passes migration 026's `surface_id_grammar` CHECK constraint. The label
  + random suffix become the opaque path portion. Pre-026 callers used
  `test:elixir/...` which the grammar CHECK rejects.
  """
  def unique_surface_id(label) when is_binary(label) do
    # Lowercase hex so the generated surface_id is already in canonical form
    # for the dialectic:/ scheme (which lowercases per RFC §7.12.1). Avoids
    # spurious round-trip-mismatch failures in tests that don't intend to
    # exercise canonicalization.
    rand = :crypto.strong_rand_bytes(6) |> Base.encode16(case: :lower)
    "dialectic:/test_elixir_#{label}_#{rand}"
  end

  @doc "Cleanup hook — DELETEs rows for a given surface_id from lease/audit tables."
  def cleanup_surface(surface_id) when is_binary(surface_id) do
    Postgrex.query!(
      DB,
      "DELETE FROM audit.tool_usage WHERE payload->>'surface_id' = $1",
      [surface_id]
    )

    Postgrex.query!(
      DB,
      "DELETE FROM lease_plane.lease_plane_events WHERE surface_id = $1",
      [surface_id]
    )

    Postgrex.query!(
      DB,
      "DELETE FROM lease_plane.surface_leases WHERE surface_id = $1",
      [surface_id]
    )

    :ok
  end

  @doc """
  Cleanup hook — DELETEs governed-effect shadow rows from `audit.events` for a
  given `idempotency_key`. The durable governed-effect record lives in the live
  `audit.events` stream (contract §8), so tests that propose a `record_only`
  effect must remove their rows just like `cleanup_surface/1` does for leases.
  """
  def cleanup_governed_effect(idempotency_key) when is_binary(idempotency_key) do
    Postgrex.query!(
      DB,
      "DELETE FROM audit.events WHERE event_type LIKE 'governed_effect.%' " <>
        "AND payload->>'idempotency_key' = $1",
      [idempotency_key]
    )

    :ok
  end

  @doc """
  Cleanup hook — DELETEs governed-effect shadow rows whose `idempotency_key`
  starts with `prefix`. For tests that mint several keys under one per-test
  prefix (e.g. the HTTP integration test).
  """
  def cleanup_governed_effect_prefix(prefix) when is_binary(prefix) do
    Postgrex.query!(
      DB,
      "DELETE FROM audit.events WHERE event_type = 'governed_effect.record_only' " <>
        "AND payload->>'idempotency_key' LIKE $1",
      [prefix <> "%"]
    )

    :ok
  end

  @doc "Stable random UUID-as-string for holder_agent_uuid in fixtures."
  def random_uuid do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)
    parts = [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    parts |> Enum.map_join("-", &Base.encode16(&1, case: :lower))
  end

  @doc "Standard local_beam acquire fixture, returns the params map."
  def local_beam_params(surface_id, opts \\ []) do
    %{
      surface_id: surface_id,
      surface_kind: Keyword.get(opts, :surface_kind, "test"),
      holder_agent_uuid: Keyword.get(opts, :holder_agent_uuid, random_uuid()),
      holder_class: "process_instance",
      ttl_s: Keyword.get(opts, :ttl_s, 30),
      intent: Keyword.get(opts, :intent, "test"),
      audit_session: Keyword.get(opts, :audit_session, "test-session")
    }
  end
end
