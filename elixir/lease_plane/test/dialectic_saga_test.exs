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
  alias UnitaresLeasePlane.DB
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

  describe "resolve/1" do
    test "commits the terminal session row and the saga" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, %{status: "resolved", saga_id: saga_id, origin: :new}} =
               DialecticSaga.resolve(claim_params(session_id))

      assert session_status(session_id) == "resolved"
      assert saga_state(saga_id) == "pg_committed"
    end

    test "is idempotent on an already-resolved session" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, %{status: "resolved"}} = DialecticSaga.resolve(claim_params(session_id))
      # Second resolve: the session is terminal -> idempotent success, no new saga.
      assert {:ok, %{status: "resolved", saga_id: nil, origin: :already_terminal}} =
               DialecticSaga.resolve(claim_params(session_id))
    end

    test "commits a failed terminal transition when status=failed" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      params = Map.put(claim_params(session_id, %{"reason" => "safety"}), :status, "failed")

      assert {:ok, %{status: "failed", saga_id: saga_id, origin: :new}} =
               DialecticSaga.resolve(params)

      assert session_status(session_id) == "failed"
      assert saga_state(saga_id) == "pg_committed"
    end

    test "rejects an invalid status" do
      assert {:error, :invalid_status} =
               DialecticSaga.resolve(Map.put(claim_params("s"), :status, "bogus"))
    end

    test "rejects when a different live resolution is in flight" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      # A fresh (non-stale) reserved saga held by a different payload blocks.
      assert {:ok, %{origin: :new}} = DialecticSaga.claim(claim_params(session_id))

      assert {:error, :saga_in_flight} =
               DialecticSaga.resolve(claim_params(session_id, %{"verdict" => "other"}))
    end
  end

  describe "stale-reserved reclaim" do
    test "claim reclaims an orphaned (old, reserved) saga and proceeds" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      # Simulate a crashed resolver: a reserved saga with an old last_attempt_at.
      Postgrex.query!(
        DB,
        """
        INSERT INTO coordination.session_resolution_sagas
          (saga_id, session_id, paused_agent_id, reviewer_agent_id, state,
           resolution_payload_json, resolution_payload_hash, last_attempt_at, attempt_count)
        VALUES (gen_random_uuid(), $1, 'p', 'r', 'reserved', '{}'::jsonb, $2, now() - interval '10 minutes', 1)
        """,
        [session_id, "orphan-hash-#{session_id}"]
      )

      # A new claim with a different payload must reclaim the orphan and succeed.
      assert {:ok, %{origin: :new}} =
               DialecticSaga.claim(claim_params(session_id, %{"verdict" => "fresh"}))
    end

    test "a recent reserved saga is NOT reclaimed" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, %{origin: :new}} = DialecticSaga.claim(claim_params(session_id))
      # Recent reserved saga still blocks a different payload.
      assert {:error, :saga_in_flight} =
               DialecticSaga.claim(claim_params(session_id, %{"verdict" => "other"}))
    end
  end

  describe "reclaim_all_stale/0 + reaper" do
    test "reverts orphaned reserved sagas across sessions" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      insert_stale_reserved(session_id)
      assert {:ok, n} = DialecticSaga.reclaim_all_stale()
      assert n >= 1
      # The session's one-pending slot is free again.
      assert {:ok, nil} = DialecticSaga.get_inflight(session_id)
    end

    test "DialecticSagaReaper.perform returns a reclaimed count" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      insert_stale_reserved(session_id)
      assert {:ok, %{reclaimed: n}} = UnitaresLeasePlane.DialecticSagaReaper.perform(%{})
      assert n >= 1
    end
  end

  describe "live_sessions/1" do
    test "lists a non-terminal session with phase, age, and resolving flag" do
      session_id = insert_dialectic_session(phase: "synthesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, %{origin: :new}} = DialecticSaga.claim(claim_params(session_id))

      {:ok, sessions} = DialecticSaga.live_sessions(500)
      mine = Enum.find(sessions, &(&1.session_id == session_id))
      assert mine.phase == "synthesis"
      assert mine.resolving == true
      assert is_integer(mine.age_seconds)
    end

    test "excludes resolved sessions" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, _} = DialecticSaga.resolve(claim_params(session_id))
      {:ok, sessions} = DialecticSaga.live_sessions(500)
      refute Enum.any?(sessions, &(&1.session_id == session_id))
    end
  end

  describe "update_phase/2" do
    test "advances a non-terminal phase" do
      session_id = insert_dialectic_session(phase: "thesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_phase(session_id, "antithesis")
      assert session_phase(session_id) == "antithesis"
    end

    test "rejects an invalid / terminal target phase" do
      assert {:error, :invalid_phase} = DialecticSaga.update_phase("x", "resolved")
      assert {:error, :invalid_phase} = DialecticSaga.update_phase("x", "bogus")
    end

    test "does not move an already-terminal session (no-op :ok)" do
      session_id = insert_dialectic_session(phase: "resolved", status: "resolved")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_phase(session_id, "antithesis")
      assert session_phase(session_id) == "resolved"
    end

    test "missing session -> :session_not_found" do
      assert {:error, :session_not_found} =
               DialecticSaga.update_phase("test_elixir_nope_phase", "thesis")
    end
  end

  describe "create_session/1" do
    test "inserts a session and starts a liveness watcher" do
      sid = "test_elixir_create_" <> Integer.to_string(System.unique_integer([:positive]))
      on_exit(fn -> cleanup_dialectic_session(sid) end)

      assert {:ok, :created} =
               DialecticSaga.create_session(%{
                 session_id: sid,
                 paused_agent_id: "p",
                 reviewer_agent_id: "r",
                 reason: "test"
               })

      assert session_status(sid) == "active"
      assert :gone != UnitaresLeasePlane.DialecticLiveness.snapshot(sid)
    end

    test "is idempotent on a duplicate session_id" do
      sid = "test_elixir_create_" <> Integer.to_string(System.unique_integer([:positive]))
      on_exit(fn -> cleanup_dialectic_session(sid) end)

      assert {:ok, :created} =
               DialecticSaga.create_session(%{session_id: sid, paused_agent_id: "p"})

      assert {:ok, :exists} =
               DialecticSaga.create_session(%{session_id: sid, paused_agent_id: "p"})
    end

    test "rejects missing paused_agent_id" do
      assert {:error, :invalid_params} = DialecticSaga.create_session(%{session_id: "x"})
    end
  end

  defp insert_stale_reserved(session_id) do
    Postgrex.query!(
      DB,
      """
      INSERT INTO coordination.session_resolution_sagas
        (saga_id, session_id, paused_agent_id, reviewer_agent_id, state,
         resolution_payload_json, resolution_payload_hash, last_attempt_at, attempt_count)
      VALUES (gen_random_uuid(), $1, 'p', 'r', 'reserved', '{}'::jsonb, $2, now() - interval '10 minutes', 1)
      """,
      [session_id, "stale-hash-#{session_id}"]
    )
  end

  defp session_status(session_id) do
    %{rows: [[status]]} =
      Postgrex.query!(DB, "SELECT status FROM core.dialectic_sessions WHERE session_id = $1", [
        session_id
      ])

    status
  end

  defp session_phase(session_id) do
    %{rows: [[phase]]} =
      Postgrex.query!(DB, "SELECT phase FROM core.dialectic_sessions WHERE session_id = $1", [
        session_id
      ])

    phase
  end

  defp saga_state(saga_id) do
    %{rows: [[state]]} =
      Postgrex.query!(
        DB,
        "SELECT state FROM coordination.session_resolution_sagas WHERE saga_id::text = $1",
        [saga_id]
      )

    state
  end
end
