defmodule DialecticLiveWeb.EndpointBoundaryTest do
  @moduledoc """
  Pins the browser boundary as it stands (portal proposal §3.5). Nothing here
  hardens it; that is the operator-reserved M4. These tests make the two
  generator defaults that form it today visible, so a change to either fails:

    * `config/prod.exs` `force_ssl` excludes only `localhost` and `127.0.0.1`,
      so any other Host is redirected to https. On a plain-http loopback bind
      that works as a Host allowlist against DNS rebinding;
    * `check_origin` is left at its default, which pins the LiveView socket to
      the endpoint url host (`localhost`). So the trial URL is
      `http://localhost:8790/timeline`, and the `127.0.0.1` origin is refused.

  And the node stays undistributed: no clustering child, `nonode@nohost`.
  """
  use ExUnit.Case, async: false

  # Phoenix logs each refused origin at error level.
  @moduletag :capture_log

  @config_dir Path.expand("../../config", __DIR__)

  defp prod_config do
    Config.Reader.read!(Path.join(@config_dir, "prod.exs"), env: :prod, target: :host)
  end

  describe "HTTP Host (force_ssl's exclude list)" do
    setup do
      force_ssl = get_in(prod_config(), [:dialectic_live, DialecticLiveWeb.Endpoint, :force_ssl])
      %{ssl: Plug.SSL.init(force_ssl), force_ssl: force_ssl}
    end

    test "the exclude list is exactly the loopback names", %{force_ssl: force_ssl} do
      assert Enum.sort(force_ssl[:exclude][:hosts]) == ["127.0.0.1", "localhost"]
    end

    test "a foreign Host is redirected away", %{ssl: ssl} do
      conn = Plug.SSL.call(conn_for("rebind.example"), ssl)
      assert conn.halted
      assert conn.status in [301, 307, 308]
    end

    test "localhost and 127.0.0.1 are served", %{ssl: ssl} do
      for host <- ["localhost", "127.0.0.1"] do
        refute Plug.SSL.call(conn_for(host), ssl).halted, host
      end
    end
  end

  defp conn_for(host) do
    %{Plug.Test.conn(:get, "/timeline") | host: host, port: 8790}
  end

  describe "LiveView socket Origin (default check_origin)" do
    test "no config file relaxes check_origin" do
      # config.exs imports prod.exs for env :prod, so one read covers both.
      cfg = Config.Reader.read!(Path.join(@config_dir, "config.exs"), env: :prod, target: :host)
      endpoint = get_in(cfg, [:dialectic_live, DialecticLiveWeb.Endpoint]) || []
      refute Keyword.has_key?(endpoint, :check_origin)
      assert endpoint[:url][:host] == "localhost"

      refute File.read!(Path.join(@config_dir, "runtime.exs")) =~ "check_origin"
    end

    test "Origin http://localhost:8790 connects; 127.0.0.1 and foreign origins are refused" do
      port = start_endpoint_server()

      assert upgrade_status(port, "http://localhost:8790") == 101
      assert upgrade_status(port, "http://127.0.0.1:8790") == 403
      assert upgrade_status(port, "http://rebind.example:8790") == 403
    end
  end

  describe "no distribution" do
    test "the node is not distributed" do
      assert node() == :nonode@nohost
      refute Node.alive?()
    end

    test "no clustering child or dependency" do
      ids = for {id, _, _, _} <- Supervisor.which_children(DialecticLive.Supervisor), do: id
      refute DNSCluster in ids
      refute :dns_cluster in Application.spec(:dialectic_live, :applications)
      refute Code.ensure_loaded?(DNSCluster)
    end
  end

  # The endpoint is started by the app with `server: false`; serve it on an
  # ephemeral loopback port so the socket upgrade runs the real check_origin.
  defp start_endpoint_server do
    pid =
      start_supervised!(
        {Bandit, plug: DialecticLiveWeb.Endpoint, ip: :loopback, port: 0, startup_log: false},
        id: make_ref()
      )

    {:ok, {_ip, port}} = ThousandIsland.listener_info(pid)
    port
  end

  defp upgrade_status(port, origin) do
    {:ok, conn} = Mint.HTTP.connect(:http, "127.0.0.1", port, protocols: [:http1])

    {:ok, conn, ref} =
      Mint.WebSocket.upgrade(:ws, conn, "/live/websocket?vsn=2.0.0", [{"origin", origin}])

    status = await_status(conn, ref)
    Mint.HTTP.close(conn)
    status
  end

  defp await_status(conn, ref) do
    receive do
      message ->
        case Mint.WebSocket.stream(conn, message) do
          {:ok, conn, responses} ->
            case Enum.find_value(responses, fn
                   {:status, ^ref, status} -> status
                   _ -> nil
                 end) do
              nil -> await_status(conn, ref)
              status -> status
            end

          other ->
            flunk("upgrade failed: #{inspect(other)}")
        end
    after
      2_000 -> flunk("no upgrade response")
    end
  end
end
