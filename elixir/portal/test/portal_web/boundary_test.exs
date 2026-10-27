defmodule PortalWeb.BoundaryTest do
  @moduledoc """
  The hub's boundary (portal-hub-v0 §3.2, H2): one Host and port, one
  Origin, loopback only, and a guard that refuses every page without a
  signed-in operator. The default auth backend refuses everyone, since the
  sign-in handoff is its own pull request.
  """
  use PortalWeb.ConnCase, async: false

  import Phoenix.LiveViewTest

  # Phoenix logs each refused origin at error level.
  @moduletag :capture_log

  @config_dir Path.expand("../../config", __DIR__)

  defp with_auth(backend, fun) do
    previous = Application.get_env(:portal, :auth)
    Application.put_env(:portal, :auth, backend)

    try do
      fun.()
    after
      if previous,
        do: Application.put_env(:portal, :auth, previous),
        else: Application.delete_env(:portal, :auth)
    end
  end

  describe "Host" do
    test "a foreign Host gets nothing, on every route", %{conn: conn} do
      for path <- ["/", "/health", "/signin", "/robots.txt", "/live/websocket"] do
        refused = get(%{conn | host: "rebind.example"}, path)
        assert refused.status == 421, path
        assert refused.halted
      end
    end

    test "the right host on another port gets nothing", %{conn: conn} do
      assert get(%{conn | port: 8790}, "/health").status == 421
    end

    test "localhost is a different name and is refused", %{conn: conn} do
      assert get(%{conn | host: "localhost"}, "/health").status == 421
    end

    test "health answers on the allowed Host with nothing but liveness", %{conn: conn} do
      assert json_response(get(conn, "/health"), 200) == %{"ok" => true}
    end
  end

  describe "the operator guard" do
    test "without a session every page is refused", %{conn: conn} do
      refused = get(conn, "/")
      assert refused.status == 401
      assert refused.resp_body =~ "Sign-in required"
    end

    test "the default backend refuses even a session that carries a token", %{conn: conn} do
      conn = Plug.Test.init_test_session(conn, token: "valid")
      assert get(conn, "/").status == 401
    end

    test "an expired session is refused", %{conn: conn} do
      with_auth(Portal.FakeAuth, fn ->
        conn = Plug.Test.init_test_session(conn, token: "expired")
        assert get(conn, "/").status == 401
      end)
    end

    test "a signed-in operator reaches the home screen", %{conn: conn} do
      with_auth(Portal.FakeAuth, fn ->
        conn = Plug.Test.init_test_session(conn, token: "valid")
        {:ok, _view, html} = live(conn, "/")
        assert html =~ "Signed in"
      end)
    end

    test "the socket mount is guarded on its own" do
      socket = %Phoenix.LiveView.Socket{}
      assert {:halt, _} = PortalWeb.Live.RequireOperator.on_mount(:default, %{}, %{}, socket)

      with_auth(Portal.FakeAuth, fn ->
        expired = %{"token" => "expired"}

        assert {:halt, _} =
                 PortalWeb.Live.RequireOperator.on_mount(:default, %{}, expired, socket)

        valid = %{"token" => "valid"}
        assert {:cont, _} = PortalWeb.Live.RequireOperator.on_mount(:default, %{}, valid, socket)
      end)
    end
  end

  describe "LiveView socket Origin" do
    test "only the hub's own origin may open the socket" do
      port = start_endpoint_server()
      previous = Application.fetch_env!(:portal, :boundary)
      Application.put_env(:portal, :boundary, host: "127.0.0.1", port: port)

      try do
        assert upgrade_status(port, "http://127.0.0.1:4002") == 101
        assert upgrade_status(port, "http://localhost:4002") == 403
        assert upgrade_status(port, "http://rebind.example:4002") == 403
        assert upgrade_status(port, "http://127.0.0.1:4002", "rebind.example") == 421
      after
        Application.put_env(:portal, :boundary, previous)
      end
    end
  end

  describe "bind" do
    test "a non-loopback address refuses to boot" do
      for ip <- [{0, 0, 0, 0}, {0, 0, 0, 0, 0, 0, 0, 0}, {192, 168, 1, 2}, :any] do
        assert_raise ArgumentError, ~r/loopback only/, fn -> Portal.Boundary.check_bind!(ip) end
      end

      for ip <- [{127, 0, 0, 1}, {0, 0, 0, 0, 0, 0, 0, 1}, :loopback] do
        assert Portal.Boundary.check_bind!(ip) == :ok
      end
    end

    test "prod runtime config binds loopback with an exact origin and has no widening setting" do
      vars = ~w(PORTAL_SECRET_KEY_BASE PORTAL_HOST PORTAL_PORT)
      saved = Map.new(vars, &{&1, System.get_env(&1)})

      try do
        System.put_env("PORTAL_SECRET_KEY_BASE", String.duplicate("k", 64))
        System.delete_env("PORTAL_HOST")
        System.delete_env("PORTAL_PORT")

        cfg =
          Config.Reader.read!(Path.join(@config_dir, "runtime.exs"), env: :prod, target: :host)

        endpoint = get_in(cfg, [:portal, PortalWeb.Endpoint])
        assert endpoint[:http][:ip] == {127, 0, 0, 1}
        assert endpoint[:check_origin] == ["http://127.0.0.1:8791"]
        assert get_in(cfg, [:portal, :boundary]) == [host: "127.0.0.1", port: 8791]
      after
        Enum.each(saved, fn
          {k, nil} -> System.delete_env(k)
          {k, v} -> System.put_env(k, v)
        end)
      end

      refute File.read!(Path.join(@config_dir, "runtime.exs")) =~ "BIND_ALL"
    end
  end

  describe "no distribution" do
    test "the node is not distributed and nothing clusters" do
      assert node() == :nonode@nohost
      refute Node.alive?()
      ids = for {id, _, _, _} <- Supervisor.which_children(Portal.Supervisor), do: id
      refute DNSCluster in ids
      refute Code.ensure_loaded?(DNSCluster)
    end
  end

  # The endpoint runs with `server: false` in tests; serve it on an ephemeral
  # loopback port so the socket upgrade runs the real plugs and check_origin.
  defp start_endpoint_server do
    pid =
      start_supervised!(
        {Bandit, plug: PortalWeb.Endpoint, ip: :loopback, port: 0, startup_log: false},
        id: make_ref()
      )

    {:ok, {_ip, port}} = ThousandIsland.listener_info(pid)
    port
  end

  defp upgrade_status(port, origin, host \\ nil) do
    {:ok, conn} = Mint.HTTP.connect(:http, "127.0.0.1", port, protocols: [:http1])
    headers = [{"origin", origin} | if(host, do: [{"host", host}], else: [])]
    {:ok, conn, ref} = Mint.WebSocket.upgrade(:ws, conn, "/live/websocket?vsn=2.0.0", headers)
    status = await_status(conn, ref)
    Mint.HTTP.close(conn)
    status
  end

  defp await_status(conn, ref) do
    receive do
      message ->
        case Mint.WebSocket.stream(conn, message) do
          {:ok, conn, responses} ->
            case Enum.find(responses, &match?({:status, ^ref, _}, &1)) do
              {:status, ^ref, status} -> status
              nil -> await_status(conn, ref)
            end

          {:error, _conn, reason, _} ->
            flunk("upgrade failed: #{inspect(reason)}")
        end
    after
      5_000 -> flunk("no upgrade response")
    end
  end
end
