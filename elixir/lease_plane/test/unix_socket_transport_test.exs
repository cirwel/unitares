defmodule UnitaresLeasePlane.UnixSocketTransportTest do
  use ExUnit.Case, async: false

  import Bitwise
  import LeaseTestHelpers

  alias UnitaresLeasePlane.Application, as: LeaseApplication
  alias UnitaresLeasePlane.HTTPRouter
  alias UnitaresLeasePlane.UnixSocket

  @bearer "test-uds-bearer-token-do-not-use-in-prod"

  setup do
    prev_bearer = Application.fetch_env(:lease_plane, :bearer_token)
    prev_mode = Application.fetch_env(:lease_plane, :identity_binding_mode)

    Application.put_env(:lease_plane, :bearer_token, @bearer)
    Application.put_env(:lease_plane, :identity_binding_mode, :off)

    tmp_root =
      Path.join(System.tmp_dir!(), "lp_uds_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmp_root)
    File.chmod!(tmp_root, 0o700)

    on_exit(fn ->
      restore_app_env(:bearer_token, prev_bearer)
      restore_app_env(:identity_binding_mode, prev_mode)
      File.rm_rf!(tmp_root)
    end)

    {:ok, tmp_root: tmp_root}
  end

  defp restore_app_env(key, {:ok, value}), do: Application.put_env(:lease_plane, key, value)
  defp restore_app_env(key, :error), do: Application.delete_env(:lease_plane, key)

  defp restore_sys_env(key, nil), do: System.delete_env(key)
  defp restore_sys_env(key, value), do: System.put_env(key, value)

  defp uds_http_request(socket_path, method, path, opts \\ []) do
    headers = Keyword.get(opts, :headers, [])
    body = Keyword.get(opts, :body)

    payload =
      case body do
        nil -> ""
        map when is_map(map) -> Jason.encode!(map)
        bin when is_binary(bin) -> bin
      end

    base_headers =
      [{"host", "localhost"}, {"connection", "close"}, {"accept", "application/json"}] ++
        if payload != "" do
          [
            {"content-type", "application/json"},
            {"content-length", Integer.to_string(byte_size(payload))}
          ]
        else
          []
        end

    all_headers = base_headers ++ headers

    header_lines =
      Enum.map_join(all_headers, "\r\n", fn {k, v} -> "#{k}: #{v}" end)

    raw_request = "#{method} #{path} HTTP/1.1\r\n#{header_lines}\r\n\r\n#{payload}"

    {:ok, sock} = :gen_tcp.connect({:local, socket_path}, 0, [:binary, active: false], 2_000)

    try do
      :ok = :gen_tcp.send(sock, raw_request)
      raw_response = recv_all(sock, [])
      parse_http_response(raw_response)
    after
      :gen_tcp.close(sock)
    end
  end

  defp recv_all(sock, acc) do
    case :gen_tcp.recv(sock, 0, 2_000) do
      {:ok, chunk} -> recv_all(sock, [chunk | acc])
      {:error, :closed} -> acc |> Enum.reverse() |> IO.iodata_to_binary()
    end
  end

  defp parse_http_response(raw) do
    [head, body] = String.split(raw, "\r\n\r\n", parts: 2)
    [status_line | _header_lines] = String.split(head, "\r\n")
    [_, status_str | _] = String.split(status_line, " ")
    {String.to_integer(status_str), Jason.decode!(body)}
  end

  describe "real UDS HTTP round-trips" do
    test "serves /v1/health, /v1/lease/acquire, /v1/lease/release over 0600 UDS", %{
      tmp_root: tmp_root
    } do
      socket_path = Path.join([tmp_root, "nested", "lease.sock"])
      surface = unique_surface_id("uds")
      holder_uuid = random_uuid()

      on_exit(fn -> cleanup_surface(surface) end)

      pid = start_supervised!({UnixSocket, socket_path: socket_path, plug: HTTPRouter})
      assert UnixSocket.socket_path(pid) == socket_path

      # Newly created parent dir is 0700 and socket itself is 0600.
      parent_stat = File.lstat!(Path.dirname(socket_path))
      assert parent_stat.type == :directory
      assert band(parent_stat.mode, 0o777) == 0o700

      sock_stat = File.lstat!(socket_path)
      assert sock_stat.type == :other
      assert band(sock_stat.mode, 0o170000) == 0o140000
      assert band(sock_stat.mode, 0o777) == 0o600

      auth = [{"authorization", "Bearer #{@bearer}"}]

      # 1. GET /v1/health
      {200, health} = uds_http_request(socket_path, "GET", "/v1/health", headers: auth)
      assert health["ok"] == true
      assert health["status"] == "ok"
      assert health["protocol_version"] == HTTPRouter.protocol_version()

      # 2. POST /v1/lease/acquire
      acquire_req = %{
        surface_id: surface,
        surface_kind: "test",
        holder_agent_uuid: holder_uuid,
        holder_kind: "local_beam",
        holder_class: "process_instance",
        ttl_s: 30,
        intent: "uds round-trip test"
      }

      {200, acquired} =
        uds_http_request(socket_path, "POST", "/v1/lease/acquire",
          headers: auth,
          body: acquire_req
        )

      assert acquired["ok"] == true
      lease_id = acquired["lease"]["lease_id"]
      assert is_binary(lease_id)

      # 3. POST /v1/lease/release
      {200, released} =
        uds_http_request(socket_path, "POST", "/v1/lease/release",
          headers: auth,
          body: %{lease_id: lease_id, release_reason: "normal"}
        )

      assert released["ok"] == true

      # 4. Graceful shutdown removes our socket file and releases the lock.
      stop_supervised!(UnixSocket)
      refute File.exists?(socket_path)
      assert File.exists?(socket_path <> ".lock")

      # 5. Immediate restart on the same path succeeds (lock released synchronously).
      _pid2 = start_supervised!({UnixSocket, socket_path: socket_path, plug: HTTPRouter})
      {200, health2} = uds_http_request(socket_path, "GET", "/v1/health", headers: auth)
      assert health2["ok"] == true
    end

    test "runs alongside a TCP Bandit child in the same supervisor with distinct ids", %{
      tmp_root: tmp_root
    } do
      socket_path = Path.join(tmp_root, "dual.sock")

      children = [
        Supervisor.child_spec(
          {Bandit, plug: HTTPRouter, ip: {127, 0, 0, 1}, port: 0, startup_log: false},
          id: UnitaresLeasePlane.HTTP.TCP
        ),
        Supervisor.child_spec(
          {UnixSocket, socket_path: socket_path, plug: HTTPRouter},
          id: UnitaresLeasePlane.HTTP.UnixSocket
        )
      ]

      {:ok, sup} = Supervisor.start_link(children, strategy: :one_for_one)

      on_exit(fn ->
        if Process.alive?(sup), do: Supervisor.stop(sup, :shutdown, 5_000)
      end)

      {200, body} =
        uds_http_request(socket_path, "GET", "/v1/health",
          headers: [{"authorization", "Bearer #{@bearer}"}]
        )

      assert body["ok"] == true
    end

    test "enforces HTTPAuth bearer 401 and 503 identically on UDS", %{tmp_root: tmp_root} do
      socket_path = Path.join(tmp_root, "auth.sock")
      _pid = start_supervised!({UnixSocket, socket_path: socket_path, plug: HTTPRouter})

      # Missing bearer -> 401
      {401, missing} = uds_http_request(socket_path, "GET", "/v1/health")
      assert missing["ok"] == false
      assert missing["error"] == "permission_denied"
      assert missing["protocol_version"] == HTTPRouter.protocol_version()

      # Wrong bearer -> 401
      {401, wrong} =
        uds_http_request(socket_path, "GET", "/v1/health",
          headers: [{"authorization", "Bearer wrong-token"}]
        )

      assert wrong["ok"] == false
      assert wrong["error"] == "permission_denied"

      # Unconfigured bearer -> 503 fail-closed
      Application.delete_env(:lease_plane, :bearer_token)

      {503, unconfigured} =
        uds_http_request(socket_path, "GET", "/v1/health",
          headers: [{"authorization", "Bearer #{@bearer}"}]
        )

      assert unconfigured["ok"] == false
      assert unconfigured["error"] == "service_unavailable"
      assert unconfigured["protocol_version"] == HTTPRouter.protocol_version()
    end
  end

  describe "path safety, locking, and stale cleanup" do
    test "refuses relative path", %{tmp_root: _tmp_root} do
      Process.flag(:trap_exit, true)

      assert {:error, {:relative_socket_path, "relative/lease.sock"}} =
               UnixSocket.start_link(socket_path: "relative/lease.sock", plug: HTTPRouter)
    end

    test "refuses symlink at target socket path without unlinking target", %{tmp_root: tmp_root} do
      Process.flag(:trap_exit, true)
      victim = Path.join(tmp_root, "victim.txt")
      File.write!(victim, "keep-me")
      symlink_path = Path.join(tmp_root, "evil.sock")
      File.ln_s!(victim, symlink_path)

      assert {:error, {:symlink_refused, ^symlink_path}} =
               UnixSocket.start_link(socket_path: symlink_path, plug: HTTPRouter)

      assert File.read!(victim) == "keep-me"
    end

    test "refuses symlink at existing parent directory leaf", %{tmp_root: tmp_root} do
      Process.flag(:trap_exit, true)
      real_dir = Path.join(tmp_root, "real")
      File.mkdir_p!(real_dir)
      link_dir = Path.join(tmp_root, "link")
      File.ln_s!(real_dir, link_dir)

      socket_path = Path.join(link_dir, "lease.sock")

      assert {:error, {:symlink_refused, ^link_dir}} =
               UnixSocket.start_link(socket_path: socket_path, plug: HTTPRouter)
    end

    test "refuses symlink at lockfile path", %{tmp_root: tmp_root} do
      Process.flag(:trap_exit, true)
      victim = Path.join(tmp_root, "lock_victim.txt")
      File.write!(victim, "untouched")
      socket_path = Path.join(tmp_root, "locked.sock")
      lock_path = socket_path <> ".lock"
      File.ln_s!(victim, lock_path)

      assert {:error, {:symlink_refused, ^lock_path}} =
               UnixSocket.start_link(socket_path: socket_path, plug: HTTPRouter)

      assert File.read!(victim) == "untouched"
    end

    test "refuses regular file at socket path without unlinking it", %{tmp_root: tmp_root} do
      Process.flag(:trap_exit, true)
      socket_path = Path.join(tmp_root, "regular.sock")
      File.write!(socket_path, "important-data")

      assert {:error, {:not_a_socket, ^socket_path, :regular}} =
               UnixSocket.start_link(socket_path: socket_path, plug: HTTPRouter)

      assert File.read!(socket_path) == "important-data"
    end

    test "holds lifetime flock and rejects a second listener on the same path", %{
      tmp_root: tmp_root
    } do
      Process.flag(:trap_exit, true)
      socket_path = Path.join(tmp_root, "shared.sock")
      _pid = start_supervised!({UnixSocket, socket_path: socket_path, plug: HTTPRouter})

      assert {:error, :eaddrinuse} =
               UnixSocket.start_link(socket_path: socket_path, plug: HTTPRouter)

      # First listener must still be serving normally.
      {200, body} =
        uds_http_request(socket_path, "GET", "/v1/health",
          headers: [{"authorization", "Bearer #{@bearer}"}]
        )

      assert body["ok"] == true
    end

    test "cleans up stale socket under lock and binds cleanly", %{tmp_root: tmp_root} do
      socket_path = Path.join(tmp_root, "stale.sock")
      {:ok, stale_sock} = :gen_tcp.listen(0, [:binary, ip: {:local, socket_path}])
      :gen_tcp.close(stale_sock)
      assert File.exists?(socket_path)

      _pid = start_supervised!({UnixSocket, socket_path: socket_path, plug: HTTPRouter})

      sock_stat = File.lstat!(socket_path)
      assert band(sock_stat.mode, 0o777) == 0o600

      {200, body} =
        uds_http_request(socket_path, "GET", "/v1/health",
          headers: [{"authorization", "Bearer #{@bearer}"}]
        )

      assert body["ok"] == true
    end
  end

  describe "Application.http_children/0 and fail-closed DISABLE_TCP guard" do
    setup do
      prev_sys_uds = System.get_env("UNITARES_LEASE_PLANE_UNIX_SOCKET")
      prev_sys_disable = System.get_env("UNITARES_LEASE_PLANE_DISABLE_TCP")
      prev_app_uds = Application.fetch_env(:lease_plane, :http_unix_socket)
      prev_app_disable = Application.fetch_env(:lease_plane, :http_disable_tcp)
      prev_start_http = Application.fetch_env(:lease_plane, :start_http)

      System.delete_env("UNITARES_LEASE_PLANE_UNIX_SOCKET")
      System.delete_env("UNITARES_LEASE_PLANE_DISABLE_TCP")
      Application.delete_env(:lease_plane, :http_unix_socket)
      Application.delete_env(:lease_plane, :http_disable_tcp)
      Application.put_env(:lease_plane, :start_http, true)

      on_exit(fn ->
        restore_sys_env("UNITARES_LEASE_PLANE_UNIX_SOCKET", prev_sys_uds)
        restore_sys_env("UNITARES_LEASE_PLANE_DISABLE_TCP", prev_sys_disable)
        restore_app_env(:http_unix_socket, prev_app_uds)
        restore_app_env(:http_disable_tcp, prev_app_disable)
        restore_app_env(:start_http, prev_start_http)
      end)

      :ok
    end

    test "refuses boot when UNITARES_LEASE_PLANE_DISABLE_TCP=1 without unix socket" do
      System.put_env("UNITARES_LEASE_PLANE_DISABLE_TCP", "1")

      assert_raise ArgumentError, ~r/UNITARES_LEASE_PLANE_DISABLE_TCP=1/, fn ->
        LeaseApplication.http_children()
      end
    end

    test "refuses boot when UNITARES_LEASE_PLANE_DISABLE_TCP=1 and unix socket is blank" do
      System.put_env("UNITARES_LEASE_PLANE_DISABLE_TCP", "1")
      System.put_env("UNITARES_LEASE_PLANE_UNIX_SOCKET", "   ")

      assert_raise ArgumentError, ~r/UNITARES_LEASE_PLANE_DISABLE_TCP=1/, fn ->
        LeaseApplication.http_children()
      end
    end

    test "runs TCP and UDS with distinct child ids when both are enabled" do
      System.put_env("UNITARES_LEASE_PLANE_UNIX_SOCKET", "/run/unitares/lease.sock")
      children = LeaseApplication.http_children()

      assert length(children) == 2
      ids = Enum.map(children, & &1.id)
      assert ids == [UnitaresLeasePlane.HTTP.TCP, UnitaresLeasePlane.HTTP.UnixSocket]
    end

    test "runs UDS-only when DISABLE_TCP=1 and app env :http_unix_socket is set" do
      System.put_env("UNITARES_LEASE_PLANE_DISABLE_TCP", "1")
      Application.put_env(:lease_plane, :http_unix_socket, "/run/unitares/lease.sock")

      children = LeaseApplication.http_children()
      assert [%{id: UnitaresLeasePlane.HTTP.UnixSocket}] = children
      assert LeaseApplication.resolved_http_ip() == {:local, "/run/unitares/lease.sock"}
    end
  end
end
