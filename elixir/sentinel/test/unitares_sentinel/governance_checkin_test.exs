defmodule UnitaresSentinel.GovernanceCheckinTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.GovernanceCheckin

  @agent_uuid "11111111-1111-1111-1111-111111111111"
  @continuity_token "v1.test-token"
  @client_session_id "session-test"

  defp summary do
    %{
      response_text: "Sentinel analysis: Cycle 1 | Fleet: 2 agents | WS: connected",
      complexity: 0.35,
      confidence: 0.85,
      response_mode: "compact"
    }
  end

  test "body targets process_agent_update and carries session anchor identity" do
    body =
      GovernanceCheckin.body(summary(),
        anchor: %{
          "agent_uuid" => @agent_uuid,
          "continuity_token" => @continuity_token,
          "client_session_id" => @client_session_id
        }
      )

    assert body["name"] == "process_agent_update"
    args = body["arguments"]
    assert args["response_text"] == summary().response_text
    assert args["complexity"] == 0.35
    assert args["confidence"] == 0.85
    assert args["response_mode"] == "compact"
    assert args["agent_id"] == @agent_uuid
    assert args["continuity_token"] == @continuity_token
    assert args["client_session_id"] == @client_session_id
  end

  test "checkin posts to the HTTP tool-call endpoint and returns result" do
    parent = self()

    http_post = fn url, body, headers, timeout_ms ->
      send(parent, {:posted, url, body, headers, timeout_ms})

      {:ok, 200,
       Jason.encode!(%{
         "success" => true,
         "result" => %{
           "decision" => %{"action" => "proceed"},
           "metrics" => %{"coherence" => 0.9}
         }
       })}
    end

    assert {:ok, result} =
             GovernanceCheckin.checkin(summary(),
               url: "http://example.test/v1/tools/call",
               timeout_ms: 123,
               http_post: http_post
             )

    assert result["decision"]["action"] == "proceed"
    assert result["metrics"]["coherence"] == 0.9

    assert_receive {:posted, url, body, headers, timeout_ms}
    assert url == "http://example.test/v1/tools/call"
    assert body["name"] == "process_agent_update"
    assert body["arguments"]["response_text"] == summary().response_text
    assert {"Content-Type", "application/json"} in headers
    assert timeout_ms == 123
  end

  test "checkin returns error for tool-level failure envelopes" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 200, ~s({"success":true,"result":{"success":false,"error":"paused"}})}
    end

    assert {:error, {:tool_error, "paused"}} =
             GovernanceCheckin.checkin(summary(), http_post: http_post)
  end

  test "checkin swallows transport exceptions" do
    http_post = fn _url, _body, _headers, _timeout_ms -> raise "connection refused" end

    assert {:error, %RuntimeError{message: "connection refused"}} =
             GovernanceCheckin.checkin(summary(), http_post: http_post)
  end

  test "checkin classifies a circuit-breaker pause distinctly from a generic tool error" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 200,
       Jason.encode!(%{
         "success" => true,
         "result" => %{
           "success" => false,
           "error" => "Agent is paused and cannot process updates",
           "error_code" => "AGENT_PAUSED",
           "paused_at" => "2026-06-13T23:40:11.993752+00:00",
           "status" => "paused",
           "recovery" => %{"action" => "Use self_recovery(action='quick')"}
         }
       })}
    end

    assert {:error, {:agent_paused, detail}} =
             GovernanceCheckin.checkin(summary(), http_post: http_post)

    assert detail["paused_at"] == "2026-06-13T23:40:11.993752+00:00"
    assert detail["status"] == "paused"
    assert detail["recovery"]["action"] =~ "self_recovery"
  end

  test "recover posts self_recovery (quick) carrying the session anchor identity" do
    parent = self()

    http_post = fn url, body, _headers, _timeout_ms ->
      send(parent, {:posted, url, body})

      {:ok, 200,
       Jason.encode!(%{
         "success" => true,
         "result" => %{"lifecycle_status" => "active", "previous_status" => "paused"}
       })}
    end

    assert {:ok, %{"lifecycle_status" => "active"}} =
             GovernanceCheckin.recover(
               url: "http://example.test/v1/tools/call",
               http_post: http_post,
               anchor: %{
                 "agent_uuid" => @agent_uuid,
                 "client_session_id" => @client_session_id
               }
             )

    assert_receive {:posted, "http://example.test/v1/tools/call", body}
    assert body["name"] == "self_recovery"
    assert body["arguments"]["action"] == "quick"
    assert body["arguments"]["agent_id"] == @agent_uuid
    assert body["arguments"]["client_session_id"] == @client_session_id
    assert body["arguments"]["reason"] =~ "bounded recovery"
  end

  test "recover surfaces a governance refusal as an error (never forces resume)" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 200,
       Jason.encode!(%{
         "success" => true,
         "result" => %{"success" => false, "error" => "Recovery thresholds not met"}
       })}
    end

    assert {:error, {:tool_error, "Recovery thresholds not met"}} =
             GovernanceCheckin.recover(http_post: http_post)
  end
end
