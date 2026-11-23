defmodule AgentOrchestrator.UnixHTTPClientTest do
  use ExUnit.Case, async: true

  alias AgentOrchestrator.UnixHTTPClient

  defp tmp_socket_path(tag) do
    "/tmp/ao_uds_#{tag}_#{System.unique_integer([:positive])}.sock"
  end

  defp start_uds_server(path, handler) do
    File.rm(path)
    {:ok, lsock} = :gen_tcp.listen(0, [:binary, {:ifaddr, {:local, path}}, {:active, false}])
    :ok = File.chmod(path, 0o600)

    parent = self()

    pid =
      spawn_link(fn ->
        send(parent, {:uds_ready, self()})
        accept_loop(lsock, handler)
      end)

    receive do
      {:uds_ready, ^pid} -> :ok
    after
      1_000 -> flunk("uds server did not start")
    end

    on_exit(fn ->
      if Process.alive?(pid), do: Process.exit(pid, :normal)
      :gen_tcp.close(lsock)
      File.rm(path)
    end)

    {lsock, pid}
  end

  defp accept_loop(lsock, handler) do
    case :gen_tcp.accept(lsock) do
      {:ok, sock} ->
        try do
          handler.(sock)
        after
          :gen_tcp.close(sock)
        end

        accept_loop(lsock, handler)

      {:error, _closed} ->
        :ok
    end
  end

  defp read_raw_http_request(sock, acc \\ <<>>) do
    case :binary.match(acc, "\r\n\r\n") do
      {pos, 4} ->
        head = binary_part(acc, 0, pos)
        rest = binary_part(acc, pos + 4, byte_size(acc) - pos - 4)

        content_length =
          head
          |> String.split("\r\n")
          |> Enum.find_value(0, fn line ->
            case String.split(line, ":", parts: 2) do
              [k, v] ->
                if String.downcase(String.trim(k)) == "content-length",
                  do: String.to_integer(String.trim(v)),
                  else: nil

              _ ->
                nil
            end
          end)

        body = read_exact_bytes(sock, rest, content_length)
        {head, body}

      :nomatch ->
        {:ok, chunk} = :gen_tcp.recv(sock, 0, 2_000)
        read_raw_http_request(sock, acc <> chunk)
    end
  end

  defp read_exact_bytes(_sock, buf, len) when byte_size(buf) >= len,
    do: binary_part(buf, 0, len)

  defp read_exact_bytes(sock, buf, len) do
    {:ok, chunk} = :gen_tcp.recv(sock, 0, 2_000)
    read_exact_bytes(sock, buf <> chunk, len)
  end

  describe "HTTP/1.1 framing over UDS" do
    test "handles Content-Length response and sends exact headers and raw body" do
      path = tmp_socket_path("cl")
      test_pid = self()

      start_uds_server(path, fn sock ->
        {head, body} = read_raw_http_request(sock)
        send(test_pid, {:received, head, body})

        resp_body = Jason.encode!(%{ok: true, transport: "uds"})

        response =
          "HTTP/1.1 200 OK\r\n" <>
            "Content-Type: application/json\r\n" <>
            "Content-Length: #{byte_size(resp_body)}\r\n" <>
            "Connection: close\r\n\r\n" <>
            resp_body

        :ok = :gen_tcp.send(sock, response)
      end)

      raw_req = ~s({"hello":"world"})

      assert {:ok, 200, headers, resp_body} =
               UnixHTTPClient.post(
                 path,
                 "/v1/lease/acquire",
                 [
                   {"authorization", "Bearer tok"},
                   {"content-type", "application/json"},
                   {"x-unitares-identity-proof", "lat.v1.abc.def"}
                 ],
                 raw_req
               )

      assert Jason.decode!(resp_body) == %{"ok" => true, "transport" => "uds"}
      assert headers["content-type"] == ["application/json"]

      assert_receive {:received, head, ^raw_req}
      assert String.starts_with?(head, "POST /v1/lease/acquire HTTP/1.1\r\n")
      assert head =~ "authorization: Bearer tok\r\n"
      assert head =~ "x-unitares-identity-proof: lat.v1.abc.def"
    end

    test "handles Transfer-Encoding: chunked responses with extensions and trailers" do
      path = tmp_socket_path("chunked")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)

        part1 = ~s({"ok":true,)
        part2 = ~s("mode":"chunked"})

        response =
          "HTTP/1.1 200 OK\r\n" <>
            "Transfer-Encoding: chunked\r\n" <>
            "Content-Type: application/json\r\n\r\n" <>
            Integer.to_string(byte_size(part1), 16) <>
            ";ext=1\r\n" <>
            part1 <>
            "\r\n" <>
            Integer.to_string(byte_size(part2), 16) <>
            "\r\n" <>
            part2 <>
            "\r\n" <>
            "0\r\n" <>
            "X-Trailer: done\r\n\r\n"

        :ok = :gen_tcp.send(sock, response)
      end)

      assert {:ok, 200, _headers, resp_body} =
               UnixHTTPClient.post(path, "/v1/lease/release", [], "{}")

      assert Jason.decode!(resp_body) == %{"ok" => true, "mode" => "chunked"}
    end

    test "handles EOF-framed responses when neither Content-Length nor chunked is set" do
      path = tmp_socket_path("eof")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)

        :ok = :gen_tcp.send(sock, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
        :ok = :gen_tcp.send(sock, ~s({"ok":))
        :ok = :gen_tcp.send(sock, ~s(true,"eof":1}))
      end)

      assert {:ok, 200, _headers, resp_body} =
               UnixHTTPClient.post(path, "/v1/lease/acquire", [], "{}")

      assert Jason.decode!(resp_body) == %{"ok" => true, "eof" => 1}
    end

    test "handles 204 No Content without waiting for a body" do
      path = tmp_socket_path("nocontent")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)
        :ok = :gen_tcp.send(sock, "HTTP/1.1 204 No Content\r\n\r\n")
      end)

      assert {:ok, 204, _headers, ""} = UnixHTTPClient.post(path, "/v1/lease/release", [], "{}")
    end

    test "rejects conflicting Content-Length and Transfer-Encoding headers" do
      path = tmp_socket_path("conflict")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)

        :ok =
          :gen_tcp.send(
            sock,
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
          )
      end)

      assert {:error, :conflicting_framing_headers} =
               UnixHTTPClient.post(path, "/v1/lease/acquire", [], "{}")
    end

    test "rejects conflicting duplicate Content-Length headers" do
      path = tmp_socket_path("dupcl")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)

        :ok =
          :gen_tcp.send(
            sock,
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 4\r\n\r\n{}"
          )
      end)

      assert {:error, :invalid_content_length} =
               UnixHTTPClient.post(path, "/v1/lease/acquire", [], "{}")
    end

    test "rejects truncated Content-Length body" do
      path = tmp_socket_path("trunc")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)
        :ok = :gen_tcp.send(sock, "HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort")
      end)

      assert {:error, :truncated_body} =
               UnixHTTPClient.post(path, "/v1/lease/acquire", [], "{}")
    end

    test "enforces max_body_bytes cap across Content-Length, chunked, and EOF responses" do
      cl_path = tmp_socket_path("cap_cl")

      start_uds_server(cl_path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)
        payload = String.duplicate("a", 64)
        :ok = :gen_tcp.send(sock, "HTTP/1.1 200 OK\r\nContent-Length: 64\r\n\r\n" <> payload)
      end)

      assert {:error, :response_too_large} =
               UnixHTTPClient.post(cl_path, "/v1/lease/acquire", [], "{}", max_body_bytes: 32)

      chunked_path = tmp_socket_path("cap_chunked")

      start_uds_server(chunked_path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)
        part = String.duplicate("b", 20)

        wire =
          "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" <>
            "14\r\n" <> part <> "\r\n" <> "14\r\n" <> part <> "\r\n0\r\n\r\n"

        :ok = :gen_tcp.send(sock, wire)
      end)

      assert {:error, :response_too_large} =
               UnixHTTPClient.post(chunked_path, "/v1/lease/acquire", [], "{}",
                 max_body_bytes: 32
               )

      eof_path = tmp_socket_path("cap_eof")

      start_uds_server(eof_path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)
        :ok = :gen_tcp.send(sock, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
        :ok = :gen_tcp.send(sock, String.duplicate("c", 20))
        :ok = :gen_tcp.send(sock, String.duplicate("d", 20))
      end)

      assert {:error, :response_too_large} =
               UnixHTTPClient.post(eof_path, "/v1/lease/acquire", [], "{}", max_body_bytes: 32)
    end

    test "enforces total timeout deadline on slow peer" do
      path = tmp_socket_path("slow")

      start_uds_server(path, fn sock ->
        {_head, _body} = read_raw_http_request(sock)
        Process.sleep(300)
      end)

      assert {:error, :timeout} =
               UnixHTTPClient.post(path, "/v1/lease/acquire", [], "{}", timeout_ms: 80)
    end

    test "enforces send_timeout deadline when peer stops reading" do
      path = tmp_socket_path("send_timeout")
      File.rm(path)

      {:ok, lsock} =
        :gen_tcp.listen(0, [
          :binary,
          {:ifaddr, {:local, path}},
          {:active, false},
          {:recbuf, 1024}
        ])

      :ok = File.chmod(path, 0o600)
      parent = self()

      pid =
        spawn_link(fn ->
          send(parent, {:stalled_ready, self()})

          case :gen_tcp.accept(lsock) do
            {:ok, sock} ->
              # Hold socket open without reading so client write blocks on full buffer.
              Process.sleep(1_000)
              :gen_tcp.close(sock)

            _ ->
              :ok
          end
        end)

      assert_receive {:stalled_ready, ^pid}, 1_000

      on_exit(fn ->
        if Process.alive?(pid), do: Process.exit(pid, :normal)
        :gen_tcp.close(lsock)
        File.rm(path)
      end)

      large_body = :binary.copy("x", 4_000_000)
      started = System.monotonic_time(:millisecond)

      assert {:error, :timeout} =
               UnixHTTPClient.request("POST", path, "/v1/lease/acquire", [], large_body,
                 timeout_ms: 80
               )

      elapsed = System.monotonic_time(:millisecond) - started
      assert elapsed < 500
    end

    test "rejects header injection and malformed request paths" do
      path = tmp_socket_path("inject")
      start_uds_server(path, fn _sock -> :ok end)

      assert {:error, :invalid_header} =
               UnixHTTPClient.post(path, "/v1/lease/acquire", [{"x-bad", "a\r\nb"}], "{}")

      assert {:error, :invalid_request_path} =
               UnixHTTPClient.post(path, "v1/lease/acquire", [], "{}")

      assert {:error, :invalid_request_path} =
               UnixHTTPClient.post(path, "/v1/lease/acquire HTTP/1.1", [], "{}")
    end
  end

  describe "socket path safety validation" do
    test "rejects relative paths, traversal, NUL/CRLF, overly long paths, and missing files" do
      assert {:error, :empty_socket_path} = UnixHTTPClient.validate_socket_path("")
      assert {:error, :relative_socket_path} = UnixHTTPClient.validate_socket_path("plane.sock")

      assert {:error, :path_traversal_not_allowed} =
               UnixHTTPClient.validate_socket_path("/tmp/../tmp/plane.sock")

      assert {:error, :invalid_socket_path} =
               UnixHTTPClient.validate_socket_path("/tmp/bad\0.sock")

      assert {:error, :socket_path_too_long} =
               UnixHTTPClient.validate_socket_path("/tmp/" <> String.duplicate("a", 100))

      assert {:error, {:socket_stat_failed, :enoent}} =
               UnixHTTPClient.validate_socket_path(tmp_socket_path("missing"))
    end

    test "rejects regular files and symlinks pointing to valid sockets" do
      reg_path = tmp_socket_path("reg")
      File.write!(reg_path, "not a socket")
      File.chmod!(reg_path, 0o600)
      on_exit(fn -> File.rm(reg_path) end)

      assert {:error, :not_a_socket} = UnixHTTPClient.validate_socket_path(reg_path)

      real_sock = tmp_socket_path("target")
      start_uds_server(real_sock, fn _sock -> :ok end)

      link_path = tmp_socket_path("link")
      File.rm(link_path)
      :ok = File.ln_s(real_sock, link_path)
      on_exit(fn -> File.rm(link_path) end)

      assert :ok = UnixHTTPClient.validate_socket_path(real_sock)
      assert {:error, :symlink_not_allowed} = UnixHTTPClient.validate_socket_path(link_path)
    end

    test "rejects group- or world-accessible socket permissions and wrong owner UID" do
      path = tmp_socket_path("perms")
      start_uds_server(path, fn _sock -> :ok end)

      for mode <- [0o660, 0o644, 0o666, 0o604] do
        :ok = File.chmod(path, mode)
        assert {:error, :insecure_socket_permissions} = UnixHTTPClient.validate_socket_path(path)
      end

      :ok = File.chmod(path, 0o600)
      assert :ok = UnixHTTPClient.validate_socket_path(path)

      uid = UnixHTTPClient.current_uid()
      assert is_integer(uid)

      assert {:error, :wrong_socket_owner} =
               UnixHTTPClient.validate_socket_path(path, expected_uid: uid + 1)
    end
  end
end
