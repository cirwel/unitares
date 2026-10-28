defmodule PortalWeb.LiveSocketTest do
  @moduledoc """
  The hub's browser side (portal-hub-v0 H2d-1): the LiveView client is served
  from the pinned dependencies and connects, and a connected page is checked
  again on a timer, ending the moment the session is no longer live.
  """
  use PortalWeb.ConnCase, async: false

  import Phoenix.LiveViewTest

  @moduletag :capture_log
  @app_js Path.expand("../../priv/static/assets/js/app.js", __DIR__)

  setup do
    saved =
      for key <- [:auth, :reverify_ms, :switch_auth_ok],
          into: %{},
          do: {key, Application.get_env(:portal, key)}

    on_exit(fn ->
      Enum.each(saved, fn
        {key, nil} -> Application.delete_env(:portal, key)
        {key, value} -> Application.put_env(:portal, key, value)
      end)
    end)

    Application.put_env(:portal, :auth, Portal.SwitchAuth)
    Application.put_env(:portal, :switch_auth_ok, true)
    :ok
  end

  describe "the LiveView client" do
    test "exactly the two library files are served from the dependencies", %{conn: conn} do
      phoenix = get(conn, "/assets/vendor/phoenix.min.js")
      assert phoenix.status == 200
      assert phoenix.resp_body =~ "Phoenix"

      live_view = get(conn, "/assets/vendor/phoenix_live_view.min.js")
      assert live_view.status == 200
      assert live_view.resp_body =~ "LiveSocket"

      for other <-
            ~w(phoenix.js phoenix.mjs phoenix_live_view.js phoenix_live_view.esm.js favicon.ico) do
        assert get(conn, "/assets/vendor/" <> other).status == 404, other
      end
    end

    test "the library files answer only on the hub's Host, like everything else", %{conn: conn} do
      foreign = conn |> Map.put(:host, "rebind.example") |> get("/assets/vendor/phoenix.min.js")
      assert foreign.status == 421
    end

    test "every page loads the libraries before the script that connects the socket", %{
      conn: conn
    } do
      html = html_response(get(conn, ~p"/"), 200)
      phoenix = :binary.match(html, "/assets/vendor/phoenix.min.js") |> elem(0)
      live_view = :binary.match(html, "/assets/vendor/phoenix_live_view.min.js") |> elem(0)
      app = :binary.match(html, "/assets/js/app.js") |> elem(0)
      assert phoenix < live_view and live_view < app
    end

    test "app.js connects the LiveView socket with the CSRF token" do
      source = File.read!(@app_js)
      assert source =~ ~s|new window.LiveView.LiveSocket("/live", window.Phoenix.Socket|
      assert source =~ "_csrf_token"
      assert source =~ "liveSocket.connect()"
    end
  end

  describe "a connected page is checked again" do
    test "it ends as soon as the session is no longer live", %{conn: conn} do
      Application.put_env(:portal, :reverify_ms, 25)
      {:ok, view, _html} = live(conn, ~p"/")

      Application.put_env(:portal, :switch_auth_ok, false)
      assert_redirect(view, "/signin", 1_000)
    end

    test "it stays while the session stays live", %{conn: conn} do
      Application.put_env(:portal, :reverify_ms, 25)
      {:ok, view, _html} = live(conn, ~p"/")

      Process.sleep(150)
      assert render(view) =~ "Signed in as operator"
    end
  end
end
