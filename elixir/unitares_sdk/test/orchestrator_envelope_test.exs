defmodule UnitaresSdk.OrchestratorEnvelopeTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.OrchestratorEnvelope, as: Env

  describe "classify_spawn/2" do
    test "v0.2 prefers execution_id over the compatibility agent_id alias" do
      assert {:ok, "ex-durable"} =
               Env.classify_spawn(201, %{
                 "ok" => true,
                 "execution_id" => "ex-durable",
                 "agent_id" => "ag-legacy"
               })
    end

    test "201 with ok and agent_id" do
      assert {:ok, "ag-abc123"} =
               Env.classify_spawn(201, %{"ok" => true, "agent_id" => "ag-abc123"})
    end

    test "error-keyed body is typed, whatever the status" do
      assert {:error, {:orchestrator_error, 403, "permission_denied", _}} =
               Env.classify_spawn(403, %{"error" => "permission_denied"})
    end

    test "201 without agent_id is not a success" do
      assert {:error, {:orchestrator_unexpected, 201, _}} =
               Env.classify_spawn(201, %{"ok" => true})
    end
  end

  describe "classify_result/2 — the result-nesting trap" do
    test "returns the NESTED map; running/output/exit_status live inside it" do
      payload = %{
        "ok" => true,
        "protocol_version" => "v0.1",
        "result" => %{"running" => true, "output" => ["line"], "exit_status" => nil}
      }

      assert {:ok, result} = Env.classify_result(200, payload)
      assert result["running"] == true

      # The trap this module closes: the top-level payload does NOT carry
      # "running". A consumer reading it flat gets nil and concludes the
      # agent finished (a session-side poller did exactly that, 2026-08-21).
      # The classifier hands back only the map where the fields actually are.
      refute Map.has_key?(payload, "running")
    end

    test "a 200 without a nested result map is an error, never a success" do
      assert {:error, {:orchestrator_unexpected, 200, _}} =
               Env.classify_result(200, %{"ok" => true, "running" => false})
    end

    test "504 await_timeout is a CONTROL signal, not a generic error" do
      # The router keeps 504 distinct from not_found so callers re-await
      # instead of reaping; dispatch_beam's council loop depends on this —
      # a generic-error reading would reap every healthy long-running lane.
      assert {:error, :await_timeout} =
               Env.classify_result(504, %{"ok" => false, "error" => "await_timeout"})
    end

    test "404 is :not_found" do
      assert {:error, :not_found} = Env.classify_result(404, %{"error" => "not_found"})
    end

    test "other error-keyed bodies stay typed" do
      assert {:error, {:orchestrator_error, 503, "service_unavailable", _}} =
               Env.classify_result(503, %{"ok" => false, "error" => "service_unavailable"})
    end
  end

  describe "classify_stop/2" do
    test "200 requires ok:true; 404 means already gone" do
      assert :ok = Env.classify_stop(200, %{"ok" => true})
      assert :ok = Env.classify_stop(404, %{"error" => "not_found"})
    end

    test "a 200 without ok:true is not silent success" do
      assert {:error, {:orchestrator_unexpected, 200, _}} =
               Env.classify_stop(200, %{"weird" => true})
    end

    test "error envelopes surface their error name — same reading as spawn/result" do
      assert {:error, {:orchestrator_error, 403, "permission_denied", _}} =
               Env.classify_stop(403, %{"ok" => false, "error" => "permission_denied"})
    end
  end
end
