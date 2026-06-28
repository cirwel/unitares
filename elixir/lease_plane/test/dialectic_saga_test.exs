defmodule UnitaresLeasePlane.DialecticSagaTest do
  @moduledoc """
  Tests for the BEAM-side dialectic resolution saga primitive (Slice 1).

  Exercises the two cross-runtime invariants against the live `governance` DB:
  phase-guard (no claim on a terminal/missing session) and one-in-flight-saga
  per session (the partial unique index), plus idempotent same-payload replay
  and commit semantics.
  """
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.DialecticSaga
  import LeaseTestHelpers

  defp claim_params(session_id, payload \\ %{"verdict" => "resume", "conditions" => ["monitor"]}) do
    %{
      session_id: session_id,
      paused_agent_id: "test_paused_agent",
      reviewer_agent_id: "test_reviewer_agent",
      resolution_payload: payload
    }
  end

  test "claim reserves a fresh saga for a non-terminal session" do
    session_id = insert_dialectic_session()
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    assert {:ok, %{saga_id: saga_id, origin: :new}} =
             DialecticSaga.claim(claim_params(session_id))

    assert is_binary(saga_id)
    assert {:ok, ^saga_id} = DialecticSaga.get_inflight(session_id)
  end

  test "claim with the same payload replays the existing saga (idempotent)" do
    session_id = insert_dialectic_session()
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    assert {:ok, %{saga_id: first, origin: :new}} = DialecticSaga.claim(claim_params(session_id))

    assert {:ok, %{saga_id: ^first, origin: :idempotent}} =
             DialecticSaga.claim(claim_params(session_id))
  end

  test "a different payload while one is in flight is rejected (one-pending-per-session)" do
    session_id = insert_dialectic_session()
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    assert {:ok, %{origin: :new}} = DialecticSaga.claim(claim_params(session_id))

    other = claim_params(session_id, %{"verdict" => "pause", "conditions" => ["halt"]})
    assert {:error, :saga_in_flight} = DialecticSaga.claim(other)
  end

  test "claim is refused on an already-terminal session" do
    session_id = insert_dialectic_session(phase: "resolved", status: "resolved")
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    assert {:error, {:session_terminal, "resolved"}} =
             DialecticSaga.claim(claim_params(session_id))
  end

  test "claim is refused on a missing session" do
    assert {:error, :session_not_found} =
             DialecticSaga.claim(claim_params("test_elixir_nonexistent_session"))
  end

  test "commit marks the saga pg_committed and frees the in-flight slot" do
    session_id = insert_dialectic_session()
    on_exit(fn -> cleanup_dialectic_session(session_id) end)

    assert {:ok, %{saga_id: saga_id, origin: :new}} =
             DialecticSaga.claim(claim_params(session_id))

    assert :ok = DialecticSaga.commit(saga_id)
    # Slot freed: no in-flight saga remains.
    assert {:ok, nil} = DialecticSaga.get_inflight(session_id)
    # Idempotent re-commit.
    assert :ok = DialecticSaga.commit(saga_id)
    # A new, different resolution can now claim (the committed one no longer blocks).
    assert {:ok, %{origin: :new}} =
             DialecticSaga.claim(claim_params(session_id, %{"verdict" => "retry"}))
  end

  test "commit on an unknown saga_id returns :saga_not_found" do
    assert {:error, :saga_not_found} =
             DialecticSaga.commit("00000000-0000-0000-0000-000000000000")
  end

  test "payload_hash is stable regardless of map key order" do
    a = %{"verdict" => "resume", "conditions" => ["x", "y"], "n" => 1}
    b = %{"n" => 1, "conditions" => ["x", "y"], "verdict" => "resume"}
    assert DialecticSaga.payload_hash(a) == DialecticSaga.payload_hash(b)
  end
end
