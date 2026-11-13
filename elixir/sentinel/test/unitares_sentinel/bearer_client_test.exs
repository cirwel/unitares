defmodule UnitaresSentinel.BearerClientTest do
  use ExUnit.Case, async: false

  alias UnitaresSentinel.{BearerClient, Findings, GovernanceCheckin}

  setup do
    BearerClient.clear_preference_cache()
    on_exit(fn -> BearerClient.clear_preference_cache() end)
    :ok
  end

  defp fake_env(map) do
    fn key -> Map.get(map, key) end
  end

  test "governance_base_url strips endpoint suffixes and preserves reverse-proxy prefixes" do
    assert BearerClient.governance_base_url(
             url: "https://gov.example/prefix/v1/tools/call"
           ) == "https://gov.example/prefix"

    assert BearerClient.governance_base_url(
             url: "https://gov.example/api/findings/"
           ) == "https://gov.example"

    assert BearerClient.governance_base_url(
             url: "wss://gov.example/ws/eisv"
           ) == "https://gov.example"

    env = fake_env(%{"GOV_REST_URL" => "https://gov.example:8443/mcp"})
    assert BearerClient.governance_base_url(env: env) == "https://gov.example:8443"
  end

  test "loopback_url? distinguishes loopback HTTP/WS targets from remote hosts" do
    assert BearerClient.loopback_url?("http://localhost:8767")
    assert BearerClient.loopback_url?("http://127.0.0.1:8767/v1/audit/volume-summary")
    assert BearerClient.loopback_url?("ws://[::1]:8767/ws/eisv")
    refute BearerClient.loopback_url?("https://gov.cirwel.org")
    refute BearerClient.loopback_url?("wss://gov.cirwel.org/ws/eisv")
  end

  test "bearer_candidates prefers MCP token on remote targets and API token on non-strict loopback" do
    env =
      fake_env(%{
        "UNITARES_MCP_BEARER_TOKEN" => "mcp-tok",
        "UNITARES_HTTP_API_TOKEN" => "api-tok"
      })

    assert BearerClient.bearer_candidates(url: "https://gov.example", env: env) == [
             "mcp-tok",
             "api-tok"
           ]

    assert BearerClient.bearer_candidates(url: "http://localhost:8767", env: env) == [
             "api-tok",
             "mcp-tok"
           ]

    strict_env =
      fake_env(%{
        "UNITARES_MCP_BEARER_TOKEN" => "mcp-tok",
        "UNITARES_HTTP_API_TOKEN" => "api-tok",
        "UNITARES_REST_STRICT" => "1"
      })

    assert BearerClient.bearer_candidates(url: "http://localhost:8767", env: strict_env) == [
             "mcp-tok",
             "api-tok"
           ]

    # Cleartext non-loopback http:// and ws:// never emit Bearer tokens.
    assert BearerClient.bearer_candidates(url: "http://gov.example", env: env) == []
    assert BearerClient.bearer_candidates(url: "ws://gov.example/ws/eisv", env: env) == []
    assert {:error, {:insecure_remote_transport, "http://gov.example/api/findings"}} =
             BearerClient.get("http://gov.example/api/findings", env: env)
  end

  test "request_with_fallback retries once on 401, caches winner, and never duplicates Authorization" do
    env =
      fake_env(%{
        "UNITARES_MCP_BEARER_TOKEN" => "mcp-tok",
        "UNITARES_HTTP_API_TOKEN" => "stale-api-tok"
      })

    parent = self()

    send_fn = fn headers ->
      auth_headers =
        Enum.filter(headers, fn {k, _} -> String.downcase(k) == "authorization" end)

      assert length(auth_headers) == 1
      [{_, auth_val}] = auth_headers
      send(parent, {:attempt, auth_val})

      if auth_val == "Bearer mcp-tok" do
        {:ok, 200, ~s({"success":true})}
      else
        {:ok, 401, ~s({"error":"unauthorized"})}
      end
    end

    assert {:ok, 200, _} =
             BearerClient.request_with_fallback(
               "http://localhost:8767/api/findings",
               [{"Content-Type", "application/json"}],
               [env: env],
               send_fn
             )

    assert_received {:attempt, "Bearer stale-api-tok"}
    assert_received {:attempt, "Bearer mcp-tok"}

    # Second call against the same loopback origin goes straight to cached winner ("mcp-tok").
    assert {:ok, 200, _} =
             BearerClient.request_with_fallback(
               "http://localhost:8767/v1/tools/call",
               [{"Content-Type", "application/json"}],
               [env: env],
               send_fn
             )

    assert_received {:attempt, "Bearer mcp-tok"}
    refute_received {:attempt, _}
  end

  test "Findings and GovernanceCheckin use remote MCP token first and loopback 401 fallback" do
    env =
      fake_env(%{
        "UNITARES_MCP_BEARER_TOKEN" => "mcp-live",
        "UNITARES_HTTP_API_TOKEN" => "api-local"
      })

    parent = self()

    # Remote Findings POST sends mcp-live first with zero 401s.
    remote_post = fn _url, _body, headers, _timeout ->
      send(parent, {:remote_headers, headers})
      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    assert :accepted ==
             Findings.post_finding_result(
               %{
                 type: "verdict_shift",
                 violation_class: "ENT",
                 severity: "high",
                 summary: "test"
               },
               url: "https://gov.example/api/findings",
               env: env,
               http_post: remote_post
             )

    assert_received {:remote_headers, remote_hdrs}
    assert {"Authorization", "Bearer mcp-live"} in remote_hdrs
    refute_received {:remote_headers, _}

    # Loopback GovernanceCheckin retries 401 with mcp-live.
    loopback_post = fn _url, _body, headers, _timeout ->
      send(parent, {:checkin_headers, headers})

      if {"Authorization", "Bearer mcp-live"} in headers do
        {:ok, 200, ~s({"success":true,"result":{"decision":{"action":"proceed"}}})}
      else
        {:ok, 401, ~s({"error":"unauthorized"})}
      end
    end

    assert {:ok, %{"decision" => %{"action" => "proceed"}}} =
             GovernanceCheckin.checkin(
               %{response_text: "ok", complexity: 0.2, confidence: 0.9},
               url: "http://127.0.0.1:8767/v1/tools/call",
               env: env,
               http_post: loopback_post
             )

    assert_received {:checkin_headers, first_hdrs}
    assert {"Authorization", "Bearer api-local"} in first_hdrs
    assert_received {:checkin_headers, second_hdrs}
    assert {"Authorization", "Bearer mcp-live"} in second_hdrs
  end
end
