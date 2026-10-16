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
    {paused_agent_id, reviewer_agent_id} =
      case Postgrex.query(
             DB,
             "SELECT paused_agent_id, reviewer_agent_id FROM core.dialectic_sessions WHERE session_id = $1",
             [session_id]
           ) do
        {:ok, %{rows: [[paused, nil]]}} ->
          reviewer = "test_reviewer_agent"

          Postgrex.query!(
            DB,
            "UPDATE core.dialectic_sessions SET reviewer_agent_id = $2 WHERE session_id = $1",
            [session_id, reviewer]
          )

          {paused, reviewer}

        {:ok, %{rows: [[paused, reviewer]]}} ->
          {paused, reviewer}

        _ ->
          {"test_paused_agent", "test_reviewer_agent"}
      end

    %{
      session_id: session_id,
      paused_agent_id: paused_agent_id,
      reviewer_agent_id: reviewer_agent_id,
      resolution_payload: payload
    }
  end

  defp reviewed_work_authorship(author, declared_by) do
    %{
      "_dialectic_reviewed_work_authorship" => %{
        "schema" => "unitares.dialectic-reviewed-work-authorship.v1",
        "status" => "declared",
        "reviewed_work_author_agent_ids" => [author],
        "declared_by_agent_id" => declared_by,
        "authorship_verified" => false
      }
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

    test "refuses a resolved transition unless the live row is still in synthesis" do
      session_id =
        insert_dialectic_session(phase: "antithesis", status: "active")

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:error, :session_phase_mismatch} =
               DialecticSaga.resolve(claim_params(session_id))

      assert session_status(session_id) == "active"
      assert session_phase(session_id) == "antithesis"
      assert {:ok, nil} = DialecticSaga.get_inflight(session_id)

      assert %{rows: [[nil]]} =
               Postgrex.query!(
                 DB,
                 "SELECT resolution_json FROM core.dialectic_sessions WHERE session_id = $1",
                 [session_id]
               )
    end

    test "is idempotent on an already-resolved session" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, %{status: "resolved"}} = DialecticSaga.resolve(claim_params(session_id))
      # Second resolve: the session is terminal -> idempotent success, no new saga.
      assert {:ok, %{status: "resolved", saga_id: nil, origin: :already_terminal}} =
               DialecticSaga.resolve(claim_params(session_id))
    end

    test "reports the actual terminal winner when the row changes after claim" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)
      params = claim_params(session_id)

      # Seed the idempotent claim. The resolver can repeat it without needing a
      # third database connection while the test holds the session row lock.
      assert {:ok, %{origin: :new}} = DialecticSaga.claim(params)
      parent = self()

      locker =
        Task.async(fn ->
          Postgrex.transaction(DB, fn conn ->
            Postgrex.query!(
              conn,
              "SELECT session_id FROM core.dialectic_sessions WHERE session_id = $1 FOR UPDATE",
              [session_id]
            )

            send(parent, {:session_row_locked, self()})

            receive do
              :terminalize ->
                Postgrex.query!(
                  conn,
                  "UPDATE core.dialectic_sessions SET status = 'failed', phase = 'failed' WHERE session_id = $1",
                  [session_id]
                )
            after
              2_000 -> raise "timed out waiting to terminalize locked dialectic row"
            end
          end)
        end)

      assert_receive {:session_row_locked, locker_pid}, 1_000
      resolver = Task.async(fn -> DialecticSaga.resolve(params) end)

      # The second pool connection can replay the claim, then blocks on the
      # terminal UPDATE behind the row lock. If it unexpectedly finishes here,
      # the test did not exercise the post-claim race it is meant to pin.
      Process.sleep(50)
      assert Task.yield(resolver, 0) == nil

      send(locker_pid, :terminalize)
      assert {:ok, _} = Task.await(locker, 2_000)

      assert {:ok, %{status: "failed", saga_id: saga_id, origin: :already_terminal}} =
               Task.await(resolver, 2_000)

      assert saga_state(saga_id) == "pg_committed"
      assert session_status(session_id) == "failed"
    end

    test "terminal write refuses a phase change after the saga claim" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)
      params = claim_params(session_id)
      parent = self()

      # SHARE permits the claim's session SELECT but blocks the terminal
      # UPDATE's ROW EXCLUSIVE table lock.  Once the newly committed saga is
      # visible, this transaction moves the live row backward before releasing
      # the resolver.  That deterministically exercises the commit-time phase
      # predicate rather than only the earlier claim guard.
      locker =
        Task.async(fn ->
          Postgrex.transaction(DB, fn conn ->
            Postgrex.query!(conn, "LOCK TABLE core.dialectic_sessions IN SHARE MODE", [])
            send(parent, {:session_table_locked, self()})

            saga_id =
              Enum.reduce_while(1..200, nil, fn _, _ ->
                case Postgrex.query!(
                       conn,
                       "SELECT saga_id::text FROM coordination.session_resolution_sagas WHERE session_id = $1 AND state = 'reserved'",
                       [session_id]
                     ).rows do
                  [[id]] ->
                    {:halt, id}

                  [] ->
                    Process.sleep(5)
                    {:cont, nil}
                end
              end)

            if is_nil(saga_id), do: raise("resolver did not commit its saga claim")

            Postgrex.query!(
              conn,
              "UPDATE core.dialectic_sessions SET phase = 'antithesis' WHERE session_id = $1",
              [session_id]
            )

            send(parent, {:phase_moved_after_claim, self(), saga_id})
          end)
        end)

      assert_receive {:session_table_locked, locker_pid}, 1_000
      resolver = Task.async(fn -> DialecticSaga.resolve(params) end)
      assert_receive {:phase_moved_after_claim, ^locker_pid, saga_id}, 2_000
      assert {:ok, _} = Task.await(locker, 2_000)

      assert {:error, :session_phase_mismatch} = Task.await(resolver, 2_000)
      assert session_status(session_id) == "active"
      assert session_phase(session_id) == "antithesis"
      assert saga_state(saga_id) == "reserved"

      assert %{rows: [[nil]]} =
               Postgrex.query!(
                 DB,
                 "SELECT resolution_json FROM core.dialectic_sessions WHERE session_id = $1",
                 [session_id]
               )
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

    test "failed transition remains valid outside synthesis" do
      session_id =
        insert_dialectic_session(phase: "antithesis", status: "active")

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      params = Map.put(claim_params(session_id, %{"reason" => "safety"}), :status, "failed")

      assert {:ok, %{status: "failed", saga_id: saga_id, origin: :new}} =
               DialecticSaga.resolve(params)

      assert session_status(session_id) == "failed"
      assert saga_state(saga_id) == "pg_committed"
    end

    test "refuses caller participant ids that do not match the stored session" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      params = Map.put(claim_params(session_id), :reviewer_agent_id, "wrong-reviewer")

      assert {:error, :session_identity_mismatch} = DialecticSaga.resolve(params)
      assert session_status(session_id) == "active"
    end

    test "refuses a declared work author as resolved-session authority" do
      proxy = "11111111-1111-4111-8111-111111111111"
      author = "22222222-2222-4222-8222-222222222222"

      session_id =
        insert_dialectic_session(
          paused_agent_id: proxy,
          reviewer_agent_id: author
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(reviewed_work_authorship(author, proxy))]
      )

      assert {:error, :reviewer_recused} =
               DialecticSaga.resolve(claim_params(session_id))

      assert session_status(session_id) == "active"
    end

    test "failed liveness close remains available with a recused reviewer" do
      proxy = "11111111-1111-4111-8111-111111111111"
      author = "22222222-2222-4222-8222-222222222222"

      session_id =
        insert_dialectic_session(
          paused_agent_id: proxy,
          reviewer_agent_id: author
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(reviewed_work_authorship(author, proxy))]
      )

      params = Map.put(claim_params(session_id), :status, "failed")
      assert {:ok, %{status: "failed"}} = DialecticSaga.resolve(params)
      assert session_status(session_id) == "failed"
    end

    test "rejects an invalid status" do
      assert {:error, :invalid_status} =
               DialecticSaga.resolve(Map.put(claim_params("s"), :status, "bogus"))
    end

    # Regression: every jsonb this module writes must land as a jsonb OBJECT,
    # not a jsonb string. Binding a `Jason.encode!` binary to a bare `$N::jsonb`
    # makes Postgrex encode it a second time, so the column holds an escaped
    # string and `->>` returns NULL on every key. Asserting only on `status`
    # (as the tests above do) cannot see that — it shipped on 2026-06-28 and
    # silently emptied 90 rows across three columns before anyone read one back.
    test "writes resolution_json as a queryable object, not a jsonb string" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      payload = %{"verdict" => "resume", "conditions" => ["monitor"]}

      assert {:ok, %{status: "resolved", saga_id: saga_id}} =
               DialecticSaga.resolve(claim_params(session_id, payload))

      assert jsonb_typeof("core.dialectic_sessions", "resolution_json", "session_id", session_id) ==
               "object"

      assert jsonb_field(
               "core.dialectic_sessions",
               "resolution_json",
               "verdict",
               "session_id",
               session_id
             ) ==
               "resume"

      # Same invariant on the saga's own copy of the payload.
      assert jsonb_typeof(
               "coordination.session_resolution_sagas",
               "resolution_payload_json",
               "saga_id::text",
               saga_id
             ) == "object"
    end

    test "writes an empty resolution as an empty object, not the string \"{}\"" do
      session_id = insert_dialectic_session()
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert {:ok, %{status: "resolved"}} = DialecticSaga.resolve(claim_params(session_id, %{}))

      assert jsonb_typeof("core.dialectic_sessions", "resolution_json", "session_id", session_id) ==
               "object"
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

    # synthesis_round persistence (2026-08-16). Python incremented the round in
    # memory while this statement wrote phase/updated_at only, so every row read
    # synthesis_round = 0 and the max_synthesis_rounds budget reset on every
    # rehydration. COALESCE keeps a nil caller byte-identical to the old write.
    test "arity-2 call leaves synthesis_round untouched" do
      session_id = insert_dialectic_session(phase: "thesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_phase(session_id, "antithesis")
      assert session_round(session_id) == 0
    end

    test "explicit nil leaves synthesis_round untouched" do
      session_id = insert_dialectic_session(phase: "thesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_phase(session_id, "synthesis", 2)
      assert session_round(session_id) == 2
      # A later nil-round update must not reset the stored value to 0.
      assert :ok = DialecticSaga.update_phase(session_id, "antithesis", nil)
      assert session_round(session_id) == 2
    end

    test "persists a supplied synthesis_round" do
      session_id = insert_dialectic_session(phase: "thesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_phase(session_id, "synthesis", 3)
      assert session_round(session_id) == 3
    end

    test "round 0 is written, not treated as absent" do
      session_id = insert_dialectic_session(phase: "thesis", status: "active", synthesis_round: 4)
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_phase(session_id, "synthesis", 0)
      assert session_round(session_id) == 0
    end

    test "rejects a negative round rather than writing it" do
      assert {:error, :invalid_phase} =
               DialecticSaga.update_phase("test_elixir_neg_round", "synthesis", -1)
    end
  end

  describe "update_reviewer/2" do
    test "assigns a reviewer on an active session" do
      session_id = insert_dialectic_session(phase: "antithesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_reviewer(session_id, "rev-99")
      assert session_reviewer(session_id) == "rev-99"
    end

    test "rejects a blank reviewer" do
      assert {:error, :invalid_reviewer} = DialecticSaga.update_reviewer("x", "")
    end

    test "open-slot claim is a NULL-to-reviewer compare-and-set" do
      session_id = insert_dialectic_session(phase: "antithesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      assert :ok = DialecticSaga.update_reviewer(session_id, "rev-first", true)

      assert {:error, :reviewer_write_refused} =
               DialecticSaga.update_reviewer(session_id, "rev-late", true)

      assert session_reviewer(session_id) == "rev-first"

      # Explicit reassignment keeps the historical arity-2 behavior.
      assert :ok = DialecticSaga.update_reviewer(session_id, "rev-operator")
      assert session_reviewer(session_id) == "rev-operator"
    end

    test "refuses a declared work author and permits an independent peer" do
      proxy = "11111111-1111-4111-8111-111111111111"
      author = "22222222-2222-4222-8222-222222222222"
      peer = "33333333-3333-4333-8333-333333333333"

      session_id =
        insert_dialectic_session(
          phase: "antithesis",
          status: "active",
          paused_agent_id: proxy
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(reviewed_work_authorship(author, proxy))]
      )

      assert {:error, :reviewer_recused} =
               DialecticSaga.update_reviewer(session_id, author)

      assert session_reviewer(session_id) == nil
      assert :ok = DialecticSaga.update_reviewer(session_id, peer)
      assert session_reviewer(session_id) == peer
    end

    test "fails closed on malformed reserved authorship metadata" do
      session_id = insert_dialectic_session(phase: "antithesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = '{\"_dialectic_reviewed_work_authorship\": {\"status\": \"declared\"}}'::jsonb WHERE session_id = $1",
        [session_id]
      )

      assert {:error, :invalid_review_authorship} =
               DialecticSaga.update_reviewer(session_id, "independent-peer")

      malformed_values = %{
        "_dialectic_reviewed_work_authorship" => %{
          "schema" => "unitares.dialectic-reviewed-work-authorship.v1",
          "status" => "declared",
          "reviewed_work_author_agent_ids" => [7],
          "declared_by_agent_id" => "11111111-1111-4111-8111-111111111111",
          "authorship_verified" => false
        }
      }

      Postgrex.query!(
        DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(malformed_values)]
      )

      assert {:error, :invalid_review_authorship} =
               DialecticSaga.update_reviewer(
                 session_id,
                 "33333333-3333-4333-8333-333333333333"
               )
    end

    test "fails closed when declaration provenance names another agent" do
      paused = "11111111-1111-4111-8111-111111111111"
      other = "22222222-2222-4222-8222-222222222222"
      peer = "33333333-3333-4333-8333-333333333333"

      session_id =
        insert_dialectic_session(
          phase: "antithesis",
          status: "active",
          paused_agent_id: paused
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(reviewed_work_authorship(other, other))]
      )

      assert {:error, :invalid_review_authorship} =
               DialecticSaga.update_reviewer(session_id, peer)
    end

    test "missing session -> :session_not_found" do
      assert {:error, :session_not_found} =
               DialecticSaga.update_reviewer("test_elixir_nope_rev", "rev-1")
    end

    # ⛔Regression for a 2026-08-22 review finding. The guarded UPDATE excludes
    # terminal rows, and this branch used to fall back to "does the session
    # exist?" and return :ok on a hit -- reporting a successful reviewer write
    # when NOTHING was written. Python treats ok:true as persisted and emits
    # `dialectic_reviewer_reassigned` on it, so a terminal row inflated the
    # criterion-10 reassignment count with reassignments that never happened.
    # Existence is not a write.
    for status <- ["resolved", "failed", "escalated"] do
      test "terminal session (#{status}) -> :session_terminal, and does not write" do
        status = unquote(status)
        session_id = insert_dialectic_session(phase: "antithesis", status: "active")
        on_exit(fn -> cleanup_dialectic_session(session_id) end)

        assert :ok = DialecticSaga.update_reviewer(session_id, "rev-before")

        {:ok, _} =
          Postgrex.query(
            UnitaresLeasePlane.DB,
            "UPDATE core.dialectic_sessions SET status = $2 WHERE session_id = $1",
            [session_id, status]
          )

        assert {:error, :session_terminal} =
                 DialecticSaga.update_reviewer(session_id, "rev-after")

        assert session_reviewer(session_id) == "rev-before",
               "a terminal row must keep its reviewer -- the guarded UPDATE wrote nothing"
      end
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

    test "refuses creation with a declared work author as reviewer" do
      sid = "test_elixir_create_" <> Integer.to_string(System.unique_integer([:positive]))
      proxy = "11111111-1111-4111-8111-111111111111"
      author = "22222222-2222-4222-8222-222222222222"
      on_exit(fn -> cleanup_dialectic_session(sid) end)

      assert {:error, :reviewer_recused} =
               DialecticSaga.create_session(%{
                 session_id: sid,
                 paused_agent_id: proxy,
                 reviewer_agent_id: author,
                 paused_agent_state: reviewed_work_authorship(author, proxy)
               })

      assert %{rows: []} =
               Postgrex.query!(
                 DB,
                 "SELECT 1 FROM core.dialectic_sessions WHERE session_id = $1",
                 [sid]
               )
    end

    test "malformed authorship is rejected even for explicit self-review" do
      sid = "test_elixir_create_" <> Integer.to_string(System.unique_integer([:positive]))
      paused = "11111111-1111-4111-8111-111111111111"
      other = "22222222-2222-4222-8222-222222222222"
      on_exit(fn -> cleanup_dialectic_session(sid) end)

      assert {:error, :invalid_review_authorship} =
               DialecticSaga.create_session(%{
                 session_id: sid,
                 paused_agent_id: paused,
                 reviewer_agent_id: paused,
                 paused_agent_state: reviewed_work_authorship(other, other)
               })
    end

    # Same double-encoding regression as resolve/1, on the creation path. This
    # column carries the paused agent's EISV snapshot; stored as a jsonb string
    # it reads back as NULL for every key, which is how 56 rows of real state
    # became unqueryable without a single error.
    test "writes paused_agent_state_json as a queryable object" do
      sid = "test_elixir_create_" <> Integer.to_string(System.unique_integer([:positive]))
      on_exit(fn -> cleanup_dialectic_session(sid) end)

      assert {:ok, :created} =
               DialecticSaga.create_session(%{
                 session_id: sid,
                 paused_agent_id: "p",
                 paused_agent_state: %{"E" => 0.7, "coherence" => 0.5}
               })

      assert jsonb_typeof("core.dialectic_sessions", "paused_agent_state_json", "session_id", sid) ==
               "object"

      assert jsonb_field(
               "core.dialectic_sessions",
               "paused_agent_state_json",
               "E",
               "session_id",
               sid
             ) ==
               "0.7"
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

  defp session_round(session_id) do
    %{rows: [[r]]} =
      Postgrex.query!(
        DB,
        "SELECT synthesis_round FROM core.dialectic_sessions WHERE session_id = $1",
        [session_id]
      )

    r
  end

  defp session_reviewer(session_id) do
    %{rows: [[rev]]} =
      Postgrex.query!(
        DB,
        "SELECT reviewer_agent_id FROM core.dialectic_sessions WHERE session_id = $1",
        [session_id]
      )

    rev
  end

  # `jsonb_typeof` is the whole point of these assertions: a double-encoded
  # write still SELECTs fine as text, so only the type discriminates.
  defp jsonb_typeof(table, column, key_expr, key) do
    %{rows: [[typ]]} =
      Postgrex.query!(
        DB,
        "SELECT jsonb_typeof(#{column}) FROM #{table} WHERE #{key_expr} = $1",
        [key]
      )

    typ
  end

  defp jsonb_field(table, column, field, key_expr, key) do
    %{rows: [[val]]} =
      Postgrex.query!(
        DB,
        "SELECT #{column} ->> $1 FROM #{table} WHERE #{key_expr} = $2",
        [field, key]
      )

    val
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
