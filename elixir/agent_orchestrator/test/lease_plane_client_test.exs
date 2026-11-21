defmodule AgentOrchestrator.LeasePlaneClientTest do
  use ExUnit.Case, async: false

  import ExUnit.CaptureLog

  alias AgentOrchestrator.AgentRunner.LeaseGovernance
  alias AgentOrchestrator.LeasePlaneClient

  @bearer "test-lease-bearer-token"
  @gov_token "test-gov-api-token"
  @holder_uuid "11111111-1111-4111-8111-111111111111"
  @continuity_token "ct.v1.secret-continuity-token-must-never-leak"

  @env_keys [
    :lease_plane_base_url,
    :lease_plane_unix_socket_path,
    :lease_plane_bearer_token,
    :governance_url,
    :governance_api_token,
    :governance_api_tokens,
    :lease_identity_proof,
    :lease_holder_agent_uuid,
    :require_lease_identity_proof,
    :insecure_governance_hosts
  ]

  setup do
    saved =
      Enum.map(@env_keys, fn key ->
        {key, Application.fetch_env(:agent_orchestrator, key)}
      end)

    Application.put_env(:agent_orchestrator, :lease_plane_bearer_token, @bearer)
    Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, nil)
    Application.put_env(:agent_orchestrator, :lease_plane_base_url, "http://127.0.0.1:8788")
    Application.put_env(:agent_orchestrator, :governance_url, "http://127.0.0.1:8767")
    Application.put_env(:agent_orchestrator, :governance_api_token, nil)
    Application.put_env(:agent_orchestrator, :governance_api_tokens, [])
    Application.put_env(:agent_orchestrator, :lease_identity_proof, nil)
    Application.put_env(:agent_orchestrator, :lease_holder_agent_uuid, nil)
    Application.put_env(:agent_orchestrator, :require_lease_identity_proof, false)
    Application.put_env(:agent_orchestrator, :insecure_governance_hosts, MapSet.new())

    on_exit(fn ->
      Enum.each(saved, fn
        {key, {:ok, val}} -> Application.put_env(:agent_orchestrator, key, val)
        {key, :error} -> Application.delete_env(:agent_orchestrator, key)
      end)

      Enum.each(AgentOrchestrator.list(), &AgentOrchestrator.stop(&1, :test_cleanup))
    end)

    :ok
  end

  defp start_tcp_http_server(handler) do
    {:ok, lsock} =
      :gen_tcp.listen(0, [
        :binary,
        {:ip, {127, 0, 0, 1}},
        {:active, false},
        {:reuseaddr, true}
      ])

    {:ok, port} = :inet.port(lsock)
    pid = spawn_http_loop(lsock, handler)

    on_exit(fn ->
      if Process.alive?(pid), do: Process.exit(pid, :normal)
      :gen_tcp.close(lsock)
    end)

    "http://127.0.0.1:#{port}"
  end

  defp start_uds_http_server(tag, handler) do
    path = "/tmp/ao_lpc_#{tag}_#{System.unique_integer([:positive])}.sock"
    File.rm(path)
    {:ok, lsock} = :gen_tcp.listen(0, [:binary, {:ifaddr, {:local, path}}, {:active, false}])
    :ok = File.chmod(path, 0o600)
    pid = spawn_http_loop(lsock, handler)

    on_exit(fn ->
      if Process.alive?(pid), do: Process.exit(pid, :normal)
      :gen_tcp.close(lsock)
      File.rm(path)
    end)

    path
  end

  defp spawn_http_loop(lsock, handler) do
    parent = self()

    pid =
      spawn_link(fn ->
        send(parent, {:server_ready, self()})
        http_accept_loop(lsock, handler)
      end)

    receive do
      {:server_ready, ^pid} -> pid
    after
      1_000 -> flunk("test HTTP server failed to start")
    end
  end

  defp http_accept_loop(lsock, handler) do
    case :gen_tcp.accept(lsock) do
      {:ok, sock} ->
        try do
          req = read_http_request(sock)
          {status, resp_body} = handler.(req)
          body_bin = if is_binary(resp_body), do: resp_body, else: Jason.encode!(resp_body)

          wire =
            "HTTP/1.1 #{status} OK\r\n" <>
              "Content-Type: application/json\r\n" <>
              "Content-Length: #{byte_size(body_bin)}\r\n" <>
              "Connection: close\r\n\r\n" <>
              body_bin

          :ok = :gen_tcp.send(sock, wire)
        after
          :gen_tcp.close(sock)
        end

        http_accept_loop(lsock, handler)

      {:error, _closed} ->
        :ok
    end
  end

  defp read_http_request(sock, acc \\ <<>>) do
    case :binary.match(acc, "\r\n\r\n") do
      {pos, 4} ->
        head = binary_part(acc, 0, pos)
        rest = binary_part(acc, pos + 4, byte_size(acc) - pos - 4)
        [request_line | header_lines] = String.split(head, "\r\n")
        [method, path, _version] = String.split(request_line, " ", parts: 3)

        headers =
          Map.new(header_lines, fn line ->
            [k, v] = String.split(line, ":", parts: 2)
            {String.downcase(String.trim(k)), String.trim(v)}
          end)

        len =
          case Map.get(headers, "content-length") do
            nil -> 0
            s -> String.to_integer(s)
          end

        raw_body = read_exact(sock, rest, len)
        %{method: method, path: path, headers: headers, raw_body: raw_body}

      :nomatch ->
        {:ok, chunk} = :gen_tcp.recv(sock, 0, 2_000)
        read_http_request(sock, acc <> chunk)
    end
  end

  defp read_exact(_sock, buf, len) when byte_size(buf) >= len, do: binary_part(buf, 0, len)

  defp read_exact(sock, buf, len) do
    {:ok, chunk} = :gen_tcp.recv(sock, 0, 2_000)
    read_exact(sock, buf <> chunk, len)
  end

  defp sha256_hex(bytes), do: :crypto.hash(:sha256, bytes) |> Base.encode16(case: :lower)

  describe "UDS and TCP transport" do
    test "acquire and release round-trip over configured Unix domain socket" do
      test_pid = self()

      socket_path =
        start_uds_http_server("roundtrip", fn req ->
          send(test_pid, {:lease_req, req})

          case req.path do
            "/v1/lease/acquire" ->
              {200, %{ok: true, lease: %{lease_id: "lease-uds-1"}}}

            "/v1/lease/release" ->
              {200, %{ok: true}}
          end
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)

      assert {:ok, "lease-uds-1"} =
               LeasePlaneClient.acquire("agent:/ex-1", @holder_uuid, "remote_heartbeat", 300)

      assert_receive {:lease_req, acq_req}
      assert acq_req.method == "POST"
      assert acq_req.path == "/v1/lease/acquire"
      assert acq_req.headers["authorization"] == "Bearer #{@bearer}"
      assert acq_req.headers["content-type"] == "application/json"
      refute Map.has_key?(acq_req.headers, "x-unitares-identity-proof")

      assert Jason.decode!(acq_req.raw_body) == %{
               "surface_id" => "agent:/ex-1",
               "holder_agent_uuid" => @holder_uuid,
               "holder_kind" => "remote_heartbeat",
               "ttl_s" => 300
             }

      assert :ok = LeasePlaneClient.release("lease-uds-1", "normal")

      assert_receive {:lease_req, rel_req}
      assert rel_req.path == "/v1/lease/release"

      assert Jason.decode!(rel_req.raw_body) == %{
               "lease_id" => "lease-uds-1",
               "release_reason" => "normal"
             }
    end

    test "supports unix:/// base URL and rejects malformed unix:// URLs" do
      socket_path =
        start_uds_http_server("unix_url", fn _req ->
          {200, %{ok: true, lease: %{lease_id: "lease-unix-url"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_base_url, "unix://" <> socket_path)

      assert {:ok, "lease-unix-url"} =
               LeasePlaneClient.acquire("agent:/ex-url", @holder_uuid, "remote_heartbeat", 60)

      Application.put_env(
        :agent_orchestrator,
        :lease_plane_base_url,
        "unix://host/not-local.sock"
      )

      assert {:error, {:unix_socket, :invalid_url}} =
               LeasePlaneClient.acquire("agent:/ex-bad", @holder_uuid, "remote_heartbeat", 60)
    end

    test "does not fall back to TCP when configured Unix socket is missing or unsafe" do
      test_pid = self()

      tcp_url =
        start_tcp_http_server(fn req ->
          send(test_pid, {:unexpected_tcp_req, req})
          {200, %{ok: true, lease_id: "should-not-hit-tcp"}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_base_url, tcp_url)

      Application.put_env(
        :agent_orchestrator,
        :lease_plane_unix_socket_path,
        "/tmp/ao_missing_#{System.unique_integer([:positive])}.sock"
      )

      assert {:error, {:unix_socket, {:socket_stat_failed, :enoent}}} =
               LeasePlaneClient.acquire("agent:/ex-2", @holder_uuid, "remote_heartbeat", 60)

      refute_receive {:unexpected_tcp_req, _}
    end

    test "falls back cleanly to TCP when no Unix socket is configured" do
      tcp_url =
        start_tcp_http_server(fn _req ->
          {200, %{ok: true, lease: %{lease_id: "lease-tcp-1"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_base_url, tcp_url)

      assert {:ok, "lease-tcp-1"} =
               LeasePlaneClient.acquire("agent:/ex-tcp", @holder_uuid, "remote_heartbeat", 120)
    end

    test "preserves SDK held_by_other error classification over UDS" do
      other_uuid = "22222222-2222-4222-8222-222222222222"

      socket_path =
        start_uds_http_server("held", fn _req ->
          {409,
           %{
             ok: false,
             error: "held_by_other",
             held_by_uuid: other_uuid,
             blocking_lease_id: "lease-other-1",
             retry_after_hint_ms: 250
           }}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)

      assert {:error, {:held_by_other, ^other_uuid, %{"blocking_lease_id" => "lease-other-1"}}} =
               LeasePlaneClient.acquire("agent:/ex-held", @holder_uuid, "remote_heartbeat", 60)
    end
  end

  describe "request-bound lat.v1 attestation and raw-body binding" do
    test "mints exact raw-body-bound lat.v1 attestations for acquire and release over UDS" do
      test_pid = self()

      gov_url =
        start_tcp_http_server(fn req ->
          send(test_pid, {:attest_req, req})
          decoded = Jason.decode!(req.raw_body)
          bsha = decoded["body_sha256"]
          path = decoded["path"]

          token = "lat.v1." <> Base.url_encode64("#{path}:#{bsha}", padding: false) <> ".sig"

          {200,
           %{
             ok: true,
             holder_agent_uuid: @holder_uuid,
             attestation: token,
             proof_type: "lease-attestation.v1"
           }}
        end)

      socket_path =
        start_uds_http_server("attest", fn req ->
          send(test_pid, {:lease_req, req})

          case req.path do
            "/v1/lease/acquire" -> {200, %{ok: true, lease: %{lease_id: "lease-attested-1"}}}
            "/v1/lease/release" -> {200, %{ok: true}}
          end
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :governance_url, gov_url)
      Application.put_env(:agent_orchestrator, :governance_api_token, @gov_token)
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)

      assert {:ok, "lease-attested-1"} =
               LeasePlaneClient.acquire("agent:/ex-att", @holder_uuid, "remote_heartbeat", 300)

      assert_receive {:attest_req, acq_att_req}
      assert acq_att_req.method == "POST"
      assert acq_att_req.path == "/v1/lease-holder/attest"
      assert acq_att_req.headers["authorization"] == "Bearer #{@gov_token}"
      acq_att_body = Jason.decode!(acq_att_req.raw_body)
      assert acq_att_body["identity_proof"] == @continuity_token
      assert acq_att_body["holder_agent_uuid"] == @holder_uuid
      assert acq_att_body["method"] == "POST"
      assert acq_att_body["path"] == "/v1/lease/acquire"

      assert_receive {:lease_req, acq_lease_req}
      # Raw wire bytes received by the lease plane MUST match the attested SHA-256 bit-for-bit.
      assert sha256_hex(acq_lease_req.raw_body) == acq_att_body["body_sha256"]

      expected_acq_token =
        "lat.v1." <>
          Base.url_encode64("/v1/lease/acquire:#{acq_att_body["body_sha256"]}", padding: false) <>
          ".sig"

      assert acq_lease_req.headers["x-unitares-identity-proof"] == expected_acq_token
      refute acq_lease_req.headers["x-unitares-identity-proof"] =~ @continuity_token

      assert :ok = LeasePlaneClient.release("lease-attested-1", "normal")

      assert_receive {:attest_req, rel_att_req}
      rel_att_body = Jason.decode!(rel_att_req.raw_body)
      assert rel_att_body["identity_proof"] == @continuity_token
      assert rel_att_body["method"] == "POST"
      assert rel_att_body["path"] == "/v1/lease/release"
      refute Map.has_key?(rel_att_body, "holder_agent_uuid")

      assert_receive {:lease_req, rel_lease_req}
      assert sha256_hex(rel_lease_req.raw_body) == rel_att_body["body_sha256"]
      assert String.starts_with?(rel_lease_req.headers["x-unitares-identity-proof"], "lat.v1.")
    end

    test "normalizes /mcp and /mcp/ suffix on GOVERNANCE_URL before calling /v1/lease-holder/attest" do
      assert {:ok, "http://127.0.0.1:8767"} =
               LeasePlaneClient.validate_governance_url("http://127.0.0.1:8767/mcp/")

      assert {:ok, "http://127.0.0.1:8767"} =
               LeasePlaneClient.validate_governance_url("http://127.0.0.1:8767/mcp")

      assert {:ok, "https://gov.example.test/prefix"} =
               LeasePlaneClient.validate_governance_url("https://gov.example.test/prefix/mcp/")

      assert {:ok, "http://127.0.0.1:8767/mcp2"} =
               LeasePlaneClient.validate_governance_url("http://127.0.0.1:8767/mcp2")

      test_pid = self()

      gov_url =
        start_tcp_http_server(fn req ->
          send(test_pid, {:attest_path, req.path})
          decoded = Jason.decode!(req.raw_body)
          token = "lat.v1." <> Base.url_encode64(decoded["body_sha256"], padding: false) <> ".sig"
          {200, %{ok: true, holder_agent_uuid: @holder_uuid, attestation: token}}
        end)

      socket_path =
        start_uds_http_server("mcp_suffix", fn _req ->
          {200, %{ok: true, lease: %{lease_id: "lease-mcp-norm"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :governance_url, gov_url <> "/mcp/")
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)

      assert {:ok, "lease-mcp-norm"} =
               LeasePlaneClient.acquire("agent:/ex-mcp", @holder_uuid, "remote_heartbeat", 60)

      assert_receive {:attest_path, "/v1/lease-holder/attest"}
    end

    test "falls back to second governance bearer token on 401 (strict mode) but not on 200 or 403" do
      test_pid = self()

      socket_path =
        start_uds_http_server("gov_fallback", fn _req ->
          {200, %{ok: true, lease: %{lease_id: "lease-strict-ok"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)
      Application.put_env(:agent_orchestrator, :governance_api_token, "local-http-tok")

      Application.put_env(:agent_orchestrator, :governance_api_tokens, [
        "local-http-tok",
        "strict-mcp-tok"
      ])

      # Case 1: local mode accepts first token -> single request, no fallback
      local_gov_url =
        start_tcp_http_server(fn req ->
          send(test_pid, {:local_auth, req.headers["authorization"]})
          decoded = Jason.decode!(req.raw_body)
          token = "lat.v1." <> Base.url_encode64(decoded["body_sha256"], padding: false) <> ".sig"
          {200, %{ok: true, holder_agent_uuid: @holder_uuid, attestation: token}}
        end)

      Application.put_env(:agent_orchestrator, :governance_url, local_gov_url)

      assert {:ok, "lease-strict-ok"} =
               LeasePlaneClient.acquire("agent:/ex-local", @holder_uuid, "remote_heartbeat", 60)

      assert_receive {:local_auth, "Bearer local-http-tok"}
      refute_receive {:local_auth, _}

      # Case 2: UNITARES_REST_STRICT=1 rejects HTTP API token with 401 and accepts MCP bearer token
      strict_gov_url =
        start_tcp_http_server(fn req ->
          auth = req.headers["authorization"]
          send(test_pid, {:strict_auth, auth})

          if auth == "Bearer strict-mcp-tok" do
            decoded = Jason.decode!(req.raw_body)

            token =
              "lat.v1." <> Base.url_encode64(decoded["body_sha256"], padding: false) <> ".sig"

            {200, %{ok: true, holder_agent_uuid: @holder_uuid, attestation: token}}
          else
            {401, %{ok: false, error: "unauthorized"}}
          end
        end)

      Application.put_env(:agent_orchestrator, :governance_url, strict_gov_url)

      assert {:ok, "lease-strict-ok"} =
               LeasePlaneClient.acquire("agent:/ex-strict", @holder_uuid, "remote_heartbeat", 60)

      assert_receive {:strict_auth, "Bearer local-http-tok"}
      assert_receive {:strict_auth, "Bearer strict-mcp-tok"}
      refute_receive {:strict_auth, _}

      # Case 3: 403 (identity_proof_invalid) does NOT retry with second token
      forbidden_gov_url =
        start_tcp_http_server(fn req ->
          send(test_pid, {:forbidden_auth, req.headers["authorization"]})
          {403, %{ok: false, error: "identity_proof_invalid"}}
        end)

      Application.put_env(:agent_orchestrator, :governance_url, forbidden_gov_url)

      assert {:error, {:identity_attestation_failed, {:attestation_http_status, 403}}} =
               LeasePlaneClient.acquire("agent:/ex-403", @holder_uuid, "remote_heartbeat", 60)

      assert_receive {:forbidden_auth, "Bearer local-http-tok"}
      refute_receive {:forbidden_auth, _}

      # Case 4: both tokens return 401 -> fails closed without leaking tokens
      all_401_gov_url =
        start_tcp_http_server(fn req ->
          send(test_pid, {:all_401_auth, req.headers["authorization"]})
          {401, %{ok: false, error: "unauthorized"}}
        end)

      Application.put_env(:agent_orchestrator, :governance_url, all_401_gov_url)

      assert {:error, {:identity_attestation_failed, {:attestation_http_status, 401}}} =
               LeasePlaneClient.acquire("agent:/ex-401", @holder_uuid, "remote_heartbeat", 60)

      assert_receive {:all_401_auth, "Bearer local-http-tok"}
      assert_receive {:all_401_auth, "Bearer strict-mcp-tok"}
      refute_receive {:all_401_auth, _}
    end

    test "passes through pre-minted lat.v1 attestation and rejects malformed lat.v1 tokens" do
      test_pid = self()

      socket_path =
        start_uds_http_server("preminted", fn req ->
          send(test_pid, {:lease_req, req})
          {200, %{ok: true, lease: %{lease_id: "lease-pre"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)

      assert {:ok, "lease-pre"} =
               LeasePlaneClient.acquire(
                 "agent:/ex-pre",
                 @holder_uuid,
                 "remote_heartbeat",
                 60,
                 identity_proof: "lat.v1.payload.sig"
               )

      assert_receive {:lease_req, req}
      assert req.headers["x-unitares-identity-proof"] == "lat.v1.payload.sig"

      assert {:error, {:identity_attestation_failed, :malformed_attestation}} =
               LeasePlaneClient.acquire(
                 "agent:/ex-pre",
                 @holder_uuid,
                 "remote_heartbeat",
                 60,
                 identity_proof: "lat.v1.bad\r\nheader"
               )
    end
  end

  describe "AGENT_ORCHESTRATOR_REQUIRE_LEASE_IDENTITY_PROOF fail-closed enforcement" do
    test "refuses before touching lease plane when proof is missing" do
      test_pid = self()

      socket_path =
        start_uds_http_server("req_missing", fn req ->
          send(test_pid, {:unexpected_lease_req, req})
          {200, %{ok: true, lease_id: "nope"}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)

      assert {:error, {:identity_attestation_required, :missing_proof}} =
               LeasePlaneClient.acquire("agent:/ex-req", @holder_uuid, "remote_heartbeat", 60)

      assert {:error, {:identity_attestation_required, :missing_proof}} =
               LeasePlaneClient.release("lease-1", "normal")

      refute_receive {:unexpected_lease_req, _}
    end

    test "refuses before touching lease plane when governance returns non-200, malformed JSON, non-lat.v1, or holder mismatch" do
      test_pid = self()

      socket_path =
        start_uds_http_server("req_bad_gov", fn req ->
          send(test_pid, {:unexpected_lease_req, req})
          {200, %{ok: true, lease_id: "nope"}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)

      cases = [
        {{403, %{ok: false, error: "identity_proof_invalid"}}, {:attestation_http_status, 403}},
        {{503, %{ok: false, error: "attestation_unavailable"}}, {:attestation_http_status, 503}},
        {{200, "not-json"}, :malformed_attestation_response},
        {{200, %{ok: true, attestation: "not-lat-v1"}}, :malformed_attestation_response},
        {{200, %{ok: false, attestation: "lat.v1.a.b"}}, :malformed_attestation_response},
        {{200,
          %{
            ok: true,
            holder_agent_uuid: "22222222-2222-4222-8222-222222222222",
            attestation: "lat.v1.a.b"
          }}, :malformed_attestation_response}
      ]

      for {gov_reply, expected_reason} <- cases do
        gov_url = start_tcp_http_server(fn _req -> gov_reply end)
        Application.put_env(:agent_orchestrator, :governance_url, gov_url)

        assert {:error, {:identity_attestation_failed, ^expected_reason}} =
                 LeasePlaneClient.acquire("agent:/ex-fail", @holder_uuid, "remote_heartbeat", 60)
      end

      refute_receive {:unexpected_lease_req, _}
    end

    test "refuses plain HTTP non-loopback governance URL without leaking credential" do
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)
      Application.put_env(:agent_orchestrator, :governance_url, "http://gov.example.test:8767")

      assert {:error, {:identity_attestation_failed, :insecure_governance_url}} =
               LeasePlaneClient.acquire("agent:/ex-http", @holder_uuid, "remote_heartbeat", 60)
    end

    test "refuses before touching lease plane when governance endpoint is unreachable" do
      test_pid = self()

      socket_path =
        start_uds_http_server("req_unreach", fn req ->
          send(test_pid, {:unexpected_lease_req, req})
          {200, %{ok: true, lease: %{lease_id: "nope"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)
      Application.put_env(:agent_orchestrator, :governance_url, "http://127.0.0.1:1")

      assert {:error, {:identity_attestation_failed, {:attestation_unreachable, _reason}}} =
               LeasePlaneClient.acquire("agent:/ex-unreach", @holder_uuid, "remote_heartbeat", 60)

      refute_receive {:unexpected_lease_req, _}
    end

    test "when optional, failed attestation never forwards raw continuity token and never logs it" do
      test_pid = self()

      gov_url = start_tcp_http_server(fn _req -> {503, %{ok: false}} end)

      socket_path =
        start_uds_http_server("opt_fallback", fn req ->
          send(test_pid, {:lease_req, req})
          {200, %{ok: true, lease: %{lease_id: "lease-opt"}}}
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :governance_url, gov_url)
      Application.put_env(:agent_orchestrator, :lease_identity_proof, @continuity_token)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, false)

      log =
        capture_log(fn ->
          assert {:ok, "lease-opt"} =
                   LeasePlaneClient.acquire("agent:/ex-opt", @holder_uuid, "remote_heartbeat", 60)
        end)

      assert log =~ "lease identity attestation unavailable"
      refute log =~ @continuity_token

      assert_receive {:lease_req, req}
      refute Map.has_key?(req.headers, "x-unitares-identity-proof")
    end
  end

  describe "AgentRunner integration with real LeasePlaneClient" do
    test "acquires and releases with distinct request-bound attestations and hides per-lease secret behind closure" do
      test_pid = self()

      cfg =
        LeaseGovernance.normalize_lease_cfg(
          %{holder_agent_uuid: @holder_uuid, identity_proof: @continuity_token, required: true},
          "ex-secret"
        )

      refute inspect(cfg) =~ @continuity_token

      gov_url =
        start_tcp_http_server(fn req ->
          decoded = Jason.decode!(req.raw_body)
          send(test_pid, {:gov_attest, decoded["path"], decoded["holder_agent_uuid"]})

          token =
            "lat.v1." <>
              Base.url_encode64(decoded["path"] <> ":" <> decoded["body_sha256"], padding: false) <>
              ".sig"

          {200, %{ok: true, holder_agent_uuid: @holder_uuid, attestation: token}}
        end)

      socket_path =
        start_uds_http_server("runner_e2e", fn req ->
          send(test_pid, {:plane_req, req.path, req.headers["x-unitares-identity-proof"]})

          case req.path do
            "/v1/lease/acquire" -> {200, %{ok: true, lease: %{lease_id: "lease-runner-1"}}}
            "/v1/lease/release" -> {200, %{ok: true}}
          end
        end)

      Application.put_env(:agent_orchestrator, :lease_plane_unix_socket_path, socket_path)
      Application.put_env(:agent_orchestrator, :governance_url, gov_url)
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)

      {:ok, id, _pid} =
        AgentOrchestrator.run(%{
          cmd: "echo",
          args: ["attested-ok"],
          lease: %{
            required: true,
            holder_agent_uuid: @holder_uuid,
            identity_proof: @continuity_token
          }
        })

      assert {:ok, %{exit_status: 0, output: ["attested-ok"], lease_id: "lease-runner-1"}} =
               AgentOrchestrator.await(id, 5_000)

      assert_receive {:gov_attest, "/v1/lease/acquire", @holder_uuid}
      assert_receive {:plane_req, "/v1/lease/acquire", "lat.v1." <> _}
      assert_receive {:gov_attest, "/v1/lease/release", @holder_uuid}
      assert_receive {:plane_req, "/v1/lease/release", "lat.v1." <> _}
    end

    test "required lease spawn fails closed when require_lease_identity_proof is true and proof is absent" do
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)

      assert {:error, {:lease_denied, {:identity_attestation_required, :missing_proof}}} =
               AgentOrchestrator.run(%{
                 cmd: "echo",
                 args: ["never-runs"],
                 lease: %{required: true}
               })
    end

    test "LeaseGovernance fails closed when require_lease_identity_proof is true and injected client lacks acquire/5 or release/3" do
      Application.put_env(:agent_orchestrator, :test_pid, self())
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, true)
      on_exit(fn -> Application.delete_env(:agent_orchestrator, :test_pid) end)

      cfg =
        LeaseGovernance.normalize_lease_cfg(
          %{required: true, holder_agent_uuid: @holder_uuid, identity_proof: @continuity_token},
          "ex-stub-req"
        )

      assert {:error, :lease_denied, :lease_client_missing_identity_proof_support} =
               LeaseGovernance.maybe_acquire_lease(%{
                 execution_id: "ex-stub-req",
                 agent_id: "agent-stub-req",
                 lease_cfg: cfg,
                 lease_client: AgentOrchestrator.StubLeaseClient
               })

      log =
        capture_log(fn ->
          assert {:error, :lease_client_missing_identity_proof_support} =
                   LeaseGovernance.maybe_release_lease(
                     %{
                       lease_id: "stub-lease",
                       lease_cfg: cfg,
                       lease_client: AgentOrchestrator.StubLeaseClient
                     },
                     "normal"
                   )
        end)

      assert log =~ "lease_client_missing_identity_proof_support"
      refute log =~ @continuity_token
      refute_receive {:lease_event, _}
    end

    test "LeaseGovernance warns without leaking proof and falls back to acquire/4 and release/2 when optional" do
      Application.put_env(:agent_orchestrator, :test_pid, self())
      Application.put_env(:agent_orchestrator, :stub_acquire_result, {:ok, "stub-fallback-lease"})
      Application.put_env(:agent_orchestrator, :require_lease_identity_proof, false)

      on_exit(fn ->
        Application.delete_env(:agent_orchestrator, :test_pid)
        Application.delete_env(:agent_orchestrator, :stub_acquire_result)
      end)

      cfg =
        LeaseGovernance.normalize_lease_cfg(
          %{required: true, holder_agent_uuid: @holder_uuid, identity_proof: @continuity_token},
          "ex-stub-warn"
        )

      acq_log =
        capture_log(fn ->
          assert {:ok, "stub-fallback-lease", :registered} =
                   LeaseGovernance.maybe_acquire_lease(%{
                     execution_id: "ex-stub-warn",
                     agent_id: "agent-stub-warn",
                     lease_cfg: cfg,
                     lease_client: AgentOrchestrator.StubLeaseClient
                   })
        end)

      assert acq_log =~ "does not implement acquire/5"
      refute acq_log =~ @continuity_token
      assert_receive {:lease_event, {:acquire, "agent:/ex-stub-warn", @holder_uuid, _, _}}

      rel_log =
        capture_log(fn ->
          assert :ok =
                   LeaseGovernance.maybe_release_lease(
                     %{
                       lease_id: "stub-fallback-lease",
                       lease_cfg: cfg,
                       lease_client: AgentOrchestrator.StubLeaseClient
                     },
                     "normal"
                   )
        end)

      assert rel_log =~ "does not implement release/3"
      refute rel_log =~ @continuity_token
      assert_receive {:lease_event, {:release, "stub-fallback-lease", "normal"}}
    end

    test "open_port strips inherited standing orchestrator credentials from raw spawns while allowing explicit spec.env" do
      cred_vars = [
        {"AGENT_ORCHESTRATOR_LEASE_IDENTITY_PROOF", "standing-proof-1"},
        {"UNITARES_LEASE_IDENTITY_PROOF", "standing-proof-2"},
        {"LEASE_PLANE_BEARER_TOKEN", "standing-lease-bearer"},
        {"UNITARES_LEASE_PLANE_BEARER_TOKEN", "standing-unitares-lease-bearer"},
        {"UNITARES_HTTP_API_TOKEN", "standing-http-api-tok"},
        {"UNITARES_MCP_BEARER_TOKEN", "standing-mcp-bearer-tok"},
        {"AGENT_ORCHESTRATOR_BEARER_TOKEN", "standing-orch-bearer-tok"}
      ]

      saved =
        Enum.map(cred_vars, fn {k, _v} -> {k, System.get_env(k)} end)

      on_exit(fn ->
        Enum.each(saved, fn
          {k, nil} -> System.delete_env(k)
          {k, v} -> System.put_env(k, v)
        end)
      end)

      Enum.each(cred_vars, fn {k, v} -> System.put_env(k, v) end)

      {:ok, raw_id, _pid} =
        AgentOrchestrator.run(%{
          cmd: "env",
          args: [],
          lease: false
        })

      assert {:ok, %{exit_status: 0, output: raw_lines}} =
               AgentOrchestrator.await(raw_id, 5_000)

      raw_env_output = Enum.join(raw_lines, "\n")

      for {k, v} <- cred_vars do
        refute raw_env_output =~ "#{k}="
        refute raw_env_output =~ v
      end

      {:ok, explicit_id, _pid} =
        AgentOrchestrator.run(%{
          cmd: "env",
          args: [],
          lease: false,
          env: [{"UNITARES_MCP_BEARER_TOKEN", "explicit-child-mcp-tok"}]
        })

      assert {:ok, %{exit_status: 0, output: explicit_lines}} =
               AgentOrchestrator.await(explicit_id, 5_000)

      explicit_env_output = Enum.join(explicit_lines, "\n")
      assert explicit_env_output =~ "UNITARES_MCP_BEARER_TOKEN=explicit-child-mcp-tok"
      refute explicit_env_output =~ "standing-mcp-bearer-tok"
      refute explicit_env_output =~ "AGENT_ORCHESTRATOR_LEASE_IDENTITY_PROOF="
      refute explicit_env_output =~ "UNITARES_LEASE_IDENTITY_PROOF="
      refute explicit_env_output =~ "UNITARES_HTTP_API_TOKEN="
      refute explicit_env_output =~ "LEASE_PLANE_BEARER_TOKEN="
      refute explicit_env_output =~ "UNITARES_LEASE_PLANE_BEARER_TOKEN="
    end
  end
end
