defmodule UnitaresSentinel.EISVWebSocketTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.BearerClient
  alias UnitaresSentinel.EISVWebSocket
  alias UnitaresSentinel.FleetState

  test "ingest_text decodes JSON text frames into FleetState" do
    fleet_state = unique_name("fleet")

    start_supervised!({FleetState, name: fleet_state, clock: fn :millisecond -> 1_000 end})

    message =
      Jason.encode!(%{
        type: "eisv_update",
        agent_id: "agent-a",
        agent_name: "Agent A",
        eisv: %{E: 0.1, I: 0.2, S: 0.3, V: 0.4},
        coherence: 0.91,
        decision: %{action: "proceed"}
      })

    assert :ok = EISVWebSocket.ingest_text(message, fleet_state)
    assert :ignored = EISVWebSocket.ingest_text("{not-json", fleet_state)
    assert :ignored = EISVWebSocket.ingest_text(~s(["not", "an", "object"]), fleet_state)

    snapshot = FleetState.snapshot(fleet_state)

    assert snapshot.event_count == 1
    assert snapshot.agents["agent-a"].last_coherence == 0.91
    assert [%{E: 0.1, verdict: "proceed"}] = snapshot.agents["agent-a"].eisv_history
  end

  test "consumer can be supervised without opening a socket" do
    fleet_state = unique_name("fleet")
    websocket = unique_name("ws")

    start_supervised!({FleetState, name: fleet_state})

    start_supervised!(
      {EISVWebSocket,
       name: websocket,
       fleet_state: fleet_state,
       connect_on_init?: false,
       url: "ws://localhost:8767/ws/eisv"}
    )

    refute EISVWebSocket.connected?(websocket)
  end

  test "select_upgrade_auth sends remote MCP bearer first and rotates on token_index" do
    BearerClient.clear_preference_cache()

    env = fn
      "UNITARES_MCP_BEARER_TOKEN" -> "mcp-ws"
      "UNITARES_HTTP_API_TOKEN" -> "api-ws"
      _ -> nil
    end

    assert {[{"authorization", "Bearer mcp-ws"}], "mcp-ws"} =
             EISVWebSocket.select_upgrade_auth("wss://gov.example/ws/eisv", [env: env], 0)

    assert {[{"authorization", "Bearer api-ws"}], "api-ws"} =
             EISVWebSocket.select_upgrade_auth("wss://gov.example/ws/eisv", [env: env], 1)

    assert {[{"authorization", "Bearer api-ws"}], "api-ws"} =
             EISVWebSocket.select_upgrade_auth("ws://localhost:8767/ws/eisv", [env: env], 0)
  end

  test "WebSocket handshake sends Authorization header and rotates token after 401 rejection" do
    BearerClient.clear_preference_cache()
    parent = self()

    {:ok, listen_sock} =
      :gen_tcp.listen(0, [:binary, packet: :raw, active: false, reuseaddr: true])

    {:ok, port} = :inet.port(listen_sock)

    server_task =
      Task.async(fn ->
        # Connection 1: expect api-tok on loopback, reject with 401.
        {:ok, sock1} = :gen_tcp.accept(listen_sock, 2_000)
        req1 = recv_http_headers(sock1, "")
        send(parent, {:ws_req, 1, req1})
        :ok = :gen_tcp.send(sock1, "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        :gen_tcp.close(sock1)

        # Connection 2: expect rotated mcp-tok, accept with 101 Switching Protocols.
        {:ok, sock2} = :gen_tcp.accept(listen_sock, 2_000)
        req2 = recv_http_headers(sock2, "")
        send(parent, {:ws_req, 2, req2})
        [_, key] = Regex.run(~r/sec-websocket-key:\s*([^\r\n]+)/i, req2)
        accept = :crypto.hash(:sha, String.trim(key) <> "258EAFA5-E914-47DA-95CA-C5AB0DC85B11") |> Base.encode64()

        resp =
          "HTTP/1.1 101 Switching Protocols\r\n" <>
            "Upgrade: websocket\r\n" <>
            "Connection: Upgrade\r\n" <>
            "Sec-WebSocket-Accept: #{accept}\r\n\r\n"

        :ok = :gen_tcp.send(sock2, resp)
        Process.sleep(150)
        :gen_tcp.close(sock2)
        :gen_tcp.close(listen_sock)
      end)

    env = fn
      "UNITARES_MCP_BEARER_TOKEN" -> "mcp-tok"
      "UNITARES_HTTP_API_TOKEN" -> "api-tok"
      _ -> nil
    end

    fleet_state = unique_name("fleet")
    websocket = unique_name("ws")
    start_supervised!({FleetState, name: fleet_state})

    start_supervised!(
      {EISVWebSocket,
       name: websocket,
       fleet_state: fleet_state,
       reconnect_ms: 25,
       url: "ws://127.0.0.1:#{port}/ws/eisv",
       auth_opts: [env: env]}
    )

    assert_receive {:ws_req, 1, req1}, 2_000
    assert String.downcase(req1) =~ "authorization: bearer api-tok"

    assert_receive {:ws_req, 2, req2}, 2_000
    assert String.downcase(req2) =~ "authorization: bearer mcp-tok"

    Task.await(server_task, 3_000)
    BearerClient.clear_preference_cache()
  end

  defp recv_http_headers(sock, acc) do
    if String.contains?(acc, "\r\n\r\n") do
      acc
    else
      {:ok, chunk} = :gen_tcp.recv(sock, 0, 2_000)
      recv_http_headers(sock, acc <> chunk)
    end
  end

  defp unique_name(prefix) do
    :"#{prefix}_#{System.unique_integer([:positive])}"
  end
end
