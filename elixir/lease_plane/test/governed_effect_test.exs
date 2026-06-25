defmodule UnitaresLeasePlane.GovernedEffectTest do
  # async: false — record_only now durably writes to the live audit.events
  # stream (contract §8). Each persisting test registers cleanup by key.
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.GovernedEffect

  defp base(overrides \\ %{}) do
    Map.merge(
      %{
        "idempotency_key" => "idem-#{System.unique_integer([:positive])}",
        "custody_mode" => "record_only",
        "effect_type" => "file_write",
        "surface" => "repo://unitares/doc_update",
        "required_leases" => []
      },
      overrides
    )
  end

  # A unique idempotency_key whose durable audit.events row is cleaned up when
  # the test exits. Use for any record_only proposal that is expected to persist.
  defp tracked_key do
    key = "ge-test-#{System.unique_integer([:positive])}"
    on_exit(fn -> LeaseTestHelpers.cleanup_governed_effect(key) end)
    key
  end

  defp tracked_base(overrides \\ %{}) do
    base(Map.merge(%{"idempotency_key" => tracked_key()}, overrides))
  end

  describe "validation" do
    test "missing idempotency_key → schema_invalid detail" do
      assert {:error, detail} = GovernedEffect.handle(Map.delete(base(), "idempotency_key"))
      assert detail =~ "idempotency_key"
    end

    test "invalid custody_mode → schema_invalid" do
      assert {:error, detail} = GovernedEffect.handle(base(%{"custody_mode" => "sideways"}))
      assert detail =~ "custody_mode"
    end

    test "missing effect_type / surface → schema_invalid" do
      assert {:error, _} = GovernedEffect.handle(Map.delete(base(), "effect_type"))
      assert {:error, _} = GovernedEffect.handle(Map.delete(base(), "surface"))
    end

    test "malformed required_leases → schema_invalid" do
      assert {:error, detail} = GovernedEffect.handle(base(%{"required_leases" => [%{"x" => 1}]}))
      assert detail =~ "required_leases"
    end

    test "non-object body → error" do
      assert {:error, _} = GovernedEffect.handle("nope")
    end
  end

  describe "Invariant 7 — credential scrub" do
    test "credential-shaped payload key is rejected" do
      for key <- ~w(client_session_id continuity_token authorization Bearer api_key my_token) do
        assert {:error, detail} =
                 GovernedEffect.handle(base(%{"payload" => %{key => "x"}})),
               "expected #{key} to be rejected"

        assert detail =~ "Invariant 7"
      end
    end

    test "a clean payload is accepted" do
      assert {:ok, _} =
               GovernedEffect.handle(
                 tracked_base(%{"payload" => %{"sha256" => "abc", "summary" => "edit"}})
               )
    end
  end

  describe "execute mode is gated" do
    test "execute → execute_not_implemented" do
      assert {:error, :execute_not_implemented} =
               GovernedEffect.handle(base(%{"custody_mode" => "execute"}))
    end
  end

  describe "record_only result" do
    test "no leases → recorded, uuid effect_id, empty observations, no pending custody" do
      assert {:ok, body} = GovernedEffect.handle(tracked_base())
      assert body.custody_mode == "record_only"
      assert body.status == "recorded"
      assert body.effect_lane == "governed_effect"
      assert body.observations == []
      assert is_nil(body.custody_expires_at)
      refute body.idempotent
      assert body.effect_id =~ ~r/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
    end

    test "effect_id is unique per proposal" do
      {:ok, a} = GovernedEffect.handle(tracked_base())
      {:ok, b} = GovernedEffect.handle(tracked_base())
      refute a.effect_id == b.effect_id
    end
  end

  describe "durable recording (contract §8)" do
    test "a record_only proposal persists one audit.events row tagged effect_lane" do
      key = tracked_key()
      assert {:ok, body} = GovernedEffect.handle(base(%{"idempotency_key" => key}))

      assert [row] = governed_effect_rows(key)
      assert row["effect_lane"] == "governed_effect"
      assert row["effect_id"] == body.effect_id
      assert row["custody_mode"] == "record_only"
      assert row["idempotency_digest"] == body.idempotency_digest
    end

    test "proposer.agent_uuid is stored as attribution; client_session_id is never stored" do
      key = tracked_key()
      agent = "11111111-2222-3333-4444-555555555555"

      assert {:ok, _} =
               GovernedEffect.handle(
                 base(%{
                   "idempotency_key" => key,
                   "proposer" => %{
                     "agent_uuid" => agent,
                     "client_session_id" => "SECRET-cs-token"
                   },
                   "provenance" => %{"session_id" => "prov-sess-7"}
                 })
               )

      assert {agent_id, session_id, payload_text} = governed_effect_attribution(key)
      assert agent_id == agent
      assert session_id == "prov-sess-7"
      # Invariant 7: the credential must appear nowhere in the durable record.
      refute payload_text =~ "SECRET-cs-token"
      refute payload_text =~ "client_session_id"
    end
  end

  describe "idempotency (contract §4)" do
    test "same key + same digest → idempotent replay of the same effect_id" do
      key = tracked_key()
      body = base(%{"idempotency_key" => key, "payload" => %{"summary" => "x"}})

      assert {:ok, first} = GovernedEffect.handle(body)
      refute first.idempotent

      assert {:ok, second} = GovernedEffect.handle(body)
      assert second.idempotent
      assert second.effect_id == first.effect_id

      # replay does not append a second row
      assert [_one] = governed_effect_rows(key)
    end

    test "same key + different digest → idempotency_conflict" do
      key = tracked_key()

      assert {:ok, _} =
               GovernedEffect.handle(base(%{"idempotency_key" => key, "surface" => "repo://a"}))

      assert {:error, :idempotency_conflict} =
               GovernedEffect.handle(base(%{"idempotency_key" => key, "surface" => "repo://b"}))
    end

    test "a stored row with a NULL idempotency_digest does not crash the retry (CaseClauseError guard)" do
      key = tracked_key()
      # Simulate a corrupt/legacy row: a record_only event for this key whose
      # payload has no idempotency_digest. The lookup must treat it as not-found
      # (persist fresh) rather than returning a digest:nil map that matches no
      # case clause and raises CaseClauseError.
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "INSERT INTO audit.events (ts, event_type, payload) " <>
          "VALUES (now(), 'governed_effect.record_only', ($1::text)::jsonb)",
        [Jason.encode!(%{"idempotency_key" => key, "effect_lane" => "governed_effect"})]
      )

      assert {:ok, body} = GovernedEffect.handle(base(%{"idempotency_key" => key}))
      refute body.idempotent
    end
  end

  describe "execute / agent_spawn (first execute slice)" do
    test "fail-closed: agent_spawn execute is execute_not_implemented when the flag is off" do
      # default state — flag unset
      assert {:error, :execute_not_implemented} =
               GovernedEffect.handle(
                 base(%{"custody_mode" => "execute", "effect_type" => "agent_spawn"})
               )
    end

    test "only agent_spawn is wired — other effect_types stay gated even with the flag on" do
      set_execute_flag(true, "http://127.0.0.1:8789")

      assert {:error, :execute_not_implemented} =
               GovernedEffect.handle(
                 base(%{"custody_mode" => "execute", "effect_type" => "file_write"})
               )
    end

    test "FAIL-CLOSED: flag on + governance veto unreachable → governance_blocked (never spawns)" do
      key = tracked_key()
      # Orchestrator reachable-looking, but the §6 governance veto points at a
      # closed port → the veto cannot affirmatively clear the effect → we must
      # NOT spawn. The orchestrator is never even reached.
      set_execute_flag(true, "http://127.0.0.1:8789", "http://127.0.0.1:1")

      assert {:error, :governance_blocked} =
               GovernedEffect.handle(
                 base(%{
                   "idempotency_key" => key,
                   "custody_mode" => "execute",
                   "effect_type" => "agent_spawn",
                   "proposer" => %{"agent_uuid" => "00000000-0000-0000-0000-0000000000aa"},
                   "payload" => %{"cmd" => "echo", "args" => ["hi"]}
                 })
               )

      assert [row] = execute_rows(key)
      assert row["custody_mode"] == "execute"
      assert row["status"] == "governance_blocked"
      assert row["effect_lane"] == "governed_effect"
    end

    test "FAIL-CLOSED: an execute agent_spawn with no proposer is governance_blocked (unattributed)" do
      key = tracked_key()
      set_execute_flag(true, "http://127.0.0.1:8789", "http://127.0.0.1:8767")

      assert {:error, :governance_blocked} =
               GovernedEffect.handle(
                 base(%{
                   "idempotency_key" => key,
                   "custody_mode" => "execute",
                   "effect_type" => "agent_spawn",
                   "payload" => %{"cmd" => "echo"}
                 })
               )
    end
  end

  # Toggle the execute flag + orchestrator + governance-veto config for one test,
  # resetting after. `gov_url` defaults to a closed port so the veto fails closed
  # unless a test deliberately points it somewhere reachable.
  defp set_execute_flag(enabled?, url, gov_url \\ "http://127.0.0.1:1") do
    prev_enabled = Application.get_env(:lease_plane, :execute_agent_spawn_enabled)
    prev_url = Application.get_env(:lease_plane, :agent_orchestrator_url)
    prev_bearer = Application.get_env(:lease_plane, :agent_orchestrator_bearer_token)
    prev_gov = Application.get_env(:lease_plane, :governance_url)

    Application.put_env(:lease_plane, :execute_agent_spawn_enabled, enabled?)
    Application.put_env(:lease_plane, :agent_orchestrator_url, url)
    Application.put_env(:lease_plane, :governance_url, gov_url)

    Application.put_env(
      :lease_plane,
      :agent_orchestrator_bearer_token,
      "test-orchestrator-bearer"
    )

    on_exit(fn ->
      restore(:execute_agent_spawn_enabled, prev_enabled)
      restore(:agent_orchestrator_url, prev_url)
      restore(:agent_orchestrator_bearer_token, prev_bearer)
      restore(:governance_url, prev_gov)
    end)
  end

  defp restore(key, nil), do: Application.delete_env(:lease_plane, key)
  defp restore(key, val), do: Application.put_env(:lease_plane, key, val)

  defp execute_rows(key) do
    %{rows: rows} =
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "SELECT payload::text FROM audit.events " <>
          "WHERE event_type = 'governed_effect.execute' AND payload->>'idempotency_key' = $1",
        [key]
      )

    Enum.map(rows, fn [payload_text] -> Jason.decode!(payload_text) end)
  end

  # --- audit.events probes (the durable governed-effect sink) ---

  defp governed_effect_rows(key) do
    %{rows: rows} =
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "SELECT payload::text FROM audit.events " <>
          "WHERE event_type = 'governed_effect.record_only' AND payload->>'idempotency_key' = $1",
        [key]
      )

    Enum.map(rows, fn [payload_text] -> Jason.decode!(payload_text) end)
  end

  defp governed_effect_attribution(key) do
    %{rows: [[agent_id, session_id, payload_text]]} =
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "SELECT agent_id, session_id, payload::text FROM audit.events " <>
          "WHERE event_type = 'governed_effect.record_only' AND payload->>'idempotency_key' = $1",
        [key]
      )

    {agent_id, session_id, payload_text}
  end

  describe "idempotency_digest" do
    test "deterministic for identical digest fields" do
      env = %{
        effect_type: "file_write",
        surface: "s",
        custody_mode: "record_only",
        payload: %{"a" => 1}
      }

      assert GovernedEffect.idempotency_digest(env) == GovernedEffect.idempotency_digest(env)
    end

    test "excludes provenance/proposer — extra keys do not change the digest" do
      env = %{effect_type: "file_write", surface: "s", custody_mode: "record_only", payload: %{}}

      with_prov =
        Map.merge(env, %{provenance: %{"session_id" => "z"}, proposer: %{"agent_uuid" => "u"}})

      assert GovernedEffect.idempotency_digest(env) ==
               GovernedEffect.idempotency_digest(with_prov)
    end

    test "differs when payload differs" do
      a = %{
        effect_type: "file_write",
        surface: "s",
        custody_mode: "record_only",
        payload: %{"a" => 1}
      }

      b = %{a | payload: %{"a" => 2}}
      refute GovernedEffect.idempotency_digest(a) == GovernedEffect.idempotency_digest(b)
    end

    test "differs when effect_type or surface differs" do
      a = %{effect_type: "file_write", surface: "s", custody_mode: "record_only", payload: %{}}

      refute GovernedEffect.idempotency_digest(a) ==
               GovernedEffect.idempotency_digest(%{a | effect_type: "repo_commit"})

      refute GovernedEffect.idempotency_digest(a) ==
               GovernedEffect.idempotency_digest(%{a | surface: "s2"})
    end
  end
end
