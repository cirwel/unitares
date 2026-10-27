defmodule PortalWeb.SigninTest do
  @moduledoc """
  The hub's half of the sign-in handoff (portal-hub-v0 H2c). The governance
  server is stubbed with `Req.Test`, except in the socket test, which serves
  a real Unix socket.
  """
  use PortalWeb.ConnCase, async: false

  @moduletag :capture_log

  @origin "https://gov.example.org"
  @token String.duplicate("t", 40)
  @config_dir Path.expand("../../config", __DIR__)

  setup do
    saved =
      for key <- [:gov, :gov_req_options, :auth],
          into: %{},
          do: {key, Application.get_env(:portal, key)}

    Application.put_env(:portal, :gov,
      origin: @origin,
      socket: "/nonexistent/gov.sock",
      token: @token
    )

    Application.put_env(:portal, :gov_req_options, plug: {Req.Test, Portal.Gov})
    Application.put_env(:portal, :auth, Portal.Auth.Hub)

    on_exit(fn ->
      Enum.each(saved, fn
        {key, nil} -> Application.delete_env(:portal, key)
        {key, value} -> Application.put_env(:portal, key, value)
      end)
    end)

    {:ok, calls} = Agent.start_link(fn -> [] end)
    %{calls: calls}
  end

  # A fake governance server: records each call, answers from `routes`.
  defp stub_gov(calls, routes) do
    Req.Test.stub(Portal.Gov, fn conn ->
      {:ok, raw, conn} = Plug.Conn.read_body(conn)
      body = Jason.decode!(raw)

      Agent.update(
        calls,
        &[{conn.request_path, body, Plug.Conn.get_req_header(conn, "authorization")} | &1]
      )

      case Map.fetch(routes, conn.request_path) do
        {:ok, {status, reply}} -> conn |> Plug.Conn.put_status(status) |> Req.Test.json(reply)
        :error -> conn |> Plug.Conn.put_status(500) |> Req.Test.json(%{})
      end
    end)
  end

  defp calls(calls), do: calls |> Agent.get(& &1) |> Enum.reverse()

  defp start_signin(conn) do
    conn = get(conn, ~p"/signin/start")
    location = redirected_to(conn, 302)
    %URI{query: query} = uri = URI.parse(location)
    {conn, uri, URI.decode_query(query)}
  end

  defp callback(conn, started, params) do
    started
    |> recycle()
    |> Map.put(:host, conn.host)
    |> Map.put(:port, conn.port)
    |> get(~p"/auth/callback", params)
  end

  @live %{
    "ok" => true,
    "operator_label" => "operator",
    "fresh" => true,
    "authenticated_at" => "2026-10-27T12:00:00+00:00",
    "expires_at" => "2026-10-28T12:00:00+00:00"
  }

  describe "start" do
    test "sends the browser to the dashboard with an S256 challenge and a state", %{conn: conn} do
      {started, uri, query} = start_signin(conn)

      assert "#{uri.scheme}://#{uri.host}" == @origin
      assert uri.path == "/auth/hub/authorize"
      assert query["code_challenge_method"] == "S256"
      assert query["code_challenge"] =~ ~r/\A[A-Za-z0-9_-]{43}\z/
      assert query["state"] =~ ~r/\A[A-Za-z0-9_-]{43}\z/

      cookie = started.resp_cookies["_portal_handoff"]
      assert cookie.same_site == "Lax"
      assert cookie.http_only
      assert cookie.path == "/auth/callback"
      assert cookie.max_age == 300
      # Encrypted: neither the state nor anything readable rides in clear.
      refute cookie.value =~ query["state"]
    end

    test "the verifier sent at exchange is the one the challenge commits to", %{
      conn: conn,
      calls: calls
    } do
      stub_gov(calls, %{"/v1/hub/exchange" => {200, %{"handle" => "h"}}})
      {started, _uri, query} = start_signin(conn)
      callback(conn, started, %{"code" => "c", "state" => query["state"]})

      [{"/v1/hub/exchange", %{"code" => "c", "code_verifier" => verifier}, auth}] = calls(calls)

      assert Base.url_encode64(:crypto.hash(:sha256, verifier), padding: false) ==
               query["code_challenge"]

      assert auth == ["Bearer #{@token}"]
    end

    test "unconfigured, nothing is started", %{conn: conn} do
      Application.put_env(:portal, :gov, [])
      conn = get(conn, ~p"/signin/start")
      assert conn.status == 401
      assert conn.resp_body =~ "Sign-in is not set up"
    end
  end

  describe "callback" do
    test "a matching return signs in with a fresh session holding only the handle",
         %{conn: conn, calls: calls} do
      stub_gov(calls, %{
        "/v1/hub/exchange" => {200, %{"handle" => "the-handle"}},
        "/v1/hub/check" => {200, @live}
      })

      conn = Plug.Test.init_test_session(conn, %{"leftover" => "from before"})
      {started, _uri, query} = start_signin(conn)
      done = callback(conn, started, %{"code" => "c", "state" => query["state"]})

      assert redirected_to(done) == "/"
      assert get_session(done) == %{"hub_handle" => "the-handle"}
      assert done.resp_cookies["_portal_handoff"].max_age == 0
      assert get_resp_header(done, "referrer-policy") == ["no-referrer"]

      home = done |> recycle() |> Map.merge(%{host: conn.host, port: conn.port}) |> get(~p"/")
      assert html_response(home, 200) =~ "Signed in as operator"
    end

    test "a state that does not match is refused without calling the server", %{
      conn: conn,
      calls: calls
    } do
      stub_gov(calls, %{})
      {started, _uri, _query} = start_signin(conn)
      done = callback(conn, started, %{"code" => "c", "state" => String.duplicate("x", 43)})

      assert done.status == 401
      assert done.resp_body =~ "did not complete"
      assert calls(calls) == []
      refute get_session(done, "hub_handle")
    end

    test "a return with no sign-in started in this browser is refused", %{
      conn: conn,
      calls: calls
    } do
      stub_gov(calls, %{})
      done = get(conn, ~p"/auth/callback", %{"code" => "c", "state" => String.duplicate("s", 43)})
      assert done.status == 401
      assert calls(calls) == []
    end

    test "a code the server refuses does not sign in", %{conn: conn, calls: calls} do
      stub_gov(calls, %{"/v1/hub/exchange" => {400, %{"error" => "invalid_grant"}}})
      {started, _uri, query} = start_signin(conn)
      done = callback(conn, started, %{"code" => "c", "state" => query["state"]})
      assert done.status == 401
      refute get_session(done, "hub_handle")
    end

    test "missing parameters are refused", %{conn: conn, calls: calls} do
      stub_gov(calls, %{})
      {started, _uri, query} = start_signin(conn)
      done = callback(conn, started, %{"state" => query["state"]})
      assert done.status == 401
      assert calls(calls) == []
    end
  end

  describe "every request asks the server" do
    test "each page load checks the handle; a signed-out handle is refused", %{
      conn: conn,
      calls: calls
    } do
      stub_gov(calls, %{"/v1/hub/check" => {200, @live}})
      signed_in = Plug.Test.init_test_session(conn, %{"hub_handle" => "h"})
      assert html_response(get(signed_in, ~p"/"), 200)
      assert html_response(get(signed_in, ~p"/"), 200)
      # Twice per load: the request guard, then the LiveView mount.
      assert Enum.count(calls(calls), &match?({"/v1/hub/check", %{"handle" => "h"}, _}, &1)) == 4

      stub_gov(calls, %{"/v1/hub/check" => {401, %{"ok" => false}}})
      refused = get(signed_in, ~p"/")
      assert refused.status == 401
      assert refused.resp_body =~ "Sign in with your passkey"
    end

    test "an unreachable server signs nobody in", %{conn: conn} do
      Req.Test.stub(Portal.Gov, &Req.Test.transport_error(&1, :econnrefused))
      signed_in = Plug.Test.init_test_session(conn, %{"hub_handle" => "h"})
      assert get(signed_in, ~p"/").status == 401
    end

    test "a server error signs nobody in", %{conn: conn, calls: calls} do
      stub_gov(calls, %{"/v1/hub/check" => {503, %{"ok" => false}}})
      signed_in = Plug.Test.init_test_session(conn, %{"hub_handle" => "h"})
      assert get(signed_in, ~p"/").status == 401
    end
  end

  describe "sign-out" do
    test "revokes the handle on the server and drops the session", %{conn: conn, calls: calls} do
      stub_gov(calls, %{"/v1/hub/revoke" => {200, %{"success" => true}}})
      page = conn |> Plug.Test.init_test_session(%{"hub_handle" => "h"}) |> get(~p"/signout")

      [token] =
        Regex.run(~r/name="_csrf_token" value="([^"]+)"/, page.resp_body, capture: :all_but_first)

      done =
        page
        |> recycle()
        |> Map.merge(%{host: conn.host, port: conn.port})
        |> put_private(:plug_skip_csrf_protection, false)
        |> post(~p"/signout", %{"_csrf_token" => token})

      assert redirected_to(done) == "/signin"
      assert [{"/v1/hub/revoke", %{"handle" => "h"}, _}] = calls(calls)
      assert done.private[:plug_session_info] == :drop
    end

    test "a sign-out without the CSRF token is refused", %{conn: conn, calls: calls} do
      stub_gov(calls, %{})
      # Phoenix.ConnTest skips CSRF protection unless told otherwise.
      signed_in =
        conn
        |> Plug.Test.init_test_session(%{"hub_handle" => "h"})
        |> put_private(:plug_skip_csrf_protection, false)

      assert_raise Plug.CSRFProtection.InvalidCSRFTokenError, fn ->
        post(signed_in, ~p"/signout", %{})
      end

      assert calls(calls) == []
    end
  end

  describe "the governance client" do
    test "talks to a real Unix socket with the hub credential" do
      dir = Path.join(System.tmp_dir!(), "portal-gov-#{System.unique_integer([:positive])}")
      File.mkdir_p!(dir)
      socket = Path.join(dir, "g.sock")
      test_pid = self()

      plug = fn conn, _opts ->
        {:ok, raw, conn} = Plug.Conn.read_body(conn)

        send(
          test_pid,
          {:seen, conn.request_path, Plug.Conn.get_req_header(conn, "authorization"), raw}
        )

        conn
        |> Plug.Conn.put_resp_content_type("application/json")
        |> Plug.Conn.send_resp(200, Jason.encode!(@live))
      end

      {:ok, server} =
        Bandit.start_link(plug: plug, ip: {:local, socket}, port: 0, startup_log: false)

      try do
        Application.put_env(:portal, :gov, origin: @origin, socket: socket, token: @token)
        Application.delete_env(:portal, :gov_req_options)

        assert {:ok, %{"operator_label" => "operator"}} = Portal.Gov.check("h")
        assert_received {:seen, "/v1/hub/check", ["Bearer " <> @token], raw}
        assert Jason.decode!(raw) == %{"handle" => "h"}
      after
        Supervisor.stop(server)
        File.rm_rf!(dir)
      end
    end
  end

  describe "configuration" do
    test "the runtime config needs all three settings before it selects the hub backend" do
      vars = ~w(PORTAL_SECRET_KEY_BASE PORTAL_GOV_ORIGIN PORTAL_GOV_SOCKET PORTAL_HUB_TOKEN)
      saved = Map.new(vars, &{&1, System.get_env(&1)})

      read = fn ->
        Config.Reader.read!(Path.join(@config_dir, "runtime.exs"), env: :prod, target: :host)
      end

      try do
        System.put_env("PORTAL_SECRET_KEY_BASE", String.duplicate("k", 64))
        System.put_env("PORTAL_GOV_ORIGIN", @origin)
        System.put_env("PORTAL_GOV_SOCKET", "/run/gov.sock")
        System.delete_env("PORTAL_HUB_TOKEN")
        refute get_in(read.(), [:portal, :auth])

        System.put_env("PORTAL_HUB_TOKEN", @token)
        assert get_in(read.(), [:portal, :auth]) == Portal.Auth.Hub

        for bad <- [
              "http://gov.example.org",
              "https://gov.example.org/path",
              "https://u@gov.example.org",
              "gov.example.org"
            ] do
          System.put_env("PORTAL_GOV_ORIGIN", bad)
          assert_raise RuntimeError, ~r/PORTAL_GOV_ORIGIN/, read
        end

        System.put_env("PORTAL_GOV_ORIGIN", "http://127.0.0.1:8767")
        assert get_in(read.(), [:portal, :auth]) == Portal.Auth.Hub
      after
        Enum.each(saved, fn
          {k, nil} -> System.delete_env(k)
          {k, v} -> System.put_env(k, v)
        end)
      end
    end
  end
end
