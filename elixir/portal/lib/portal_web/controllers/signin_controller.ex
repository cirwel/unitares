defmodule PortalWeb.SigninController do
  @moduledoc """
  The hub's half of the sign-in handoff (portal-hub-v0 H2c).

  1. `start/2` makes a PKCE verifier and a state, keeps both in a short-lived
     encrypted cookie, and sends the browser to the dashboard's approval page.
  2. The dashboard returns the browser to `callback/2` with a one-time code and
     the same state. The state must match the cookie, and the code is then
     exchanged, with the verifier, over the server's owner-only socket.
  3. The session then holds only the hub handle. A fresh session id is issued
     at that point, so nothing from before sign-in carries over.

  The handoff cookie is `SameSite=Lax`, unlike the session's `Strict`: the
  return from the dashboard is a navigation another site starts, and a
  `Strict` cookie would not come with it. It holds no credential, only what
  proves the return belongs to the sign-in this browser began, and it is
  scoped to the callback path and deleted there.
  """
  use PortalWeb, :controller

  @handoff_cookie "_portal_handoff"
  @handoff_seconds 300

  def show(conn, _params) do
    case Portal.Auth.verify(get_session(conn)) do
      {:ok, _principal} -> redirect(conn, to: ~p"/")
      {:error, _reason} -> page(conn, 401, PortalWeb.SigninHTML.refusal())
    end
  end

  def start(conn, _params) do
    if Portal.Gov.configured?() do
      verifier = random_token()
      state = random_token()
      challenge = Base.url_encode64(:crypto.hash(:sha256, verifier), padding: false)

      conn
      |> put_resp_cookie(@handoff_cookie, %{"verifier" => verifier, "state" => state},
        encrypt: true,
        max_age: @handoff_seconds,
        same_site: "Lax",
        http_only: true,
        path: ~p"/auth/callback"
      )
      |> redirect(external: Portal.Gov.authorize_url(challenge, state))
    else
      page(conn, 401, PortalWeb.SigninHTML.required())
    end
  end

  def callback(conn, params) do
    conn = fetch_cookies(conn, encrypted: [@handoff_cookie])
    handoff = conn.cookies[@handoff_cookie]

    conn =
      conn
      |> delete_resp_cookie(@handoff_cookie, path: ~p"/auth/callback")
      |> put_resp_header("referrer-policy", "no-referrer")
      |> put_resp_header("cache-control", "no-store")

    with %{"verifier" => verifier, "state" => expected} <- handoff,
         %{"code" => code, "state" => state} when is_binary(code) and is_binary(state) <- params,
         true <- Plug.Crypto.secure_compare(state, expected),
         {:ok, %{"handle" => handle}} <- Portal.Gov.exchange(code, verifier) do
      # Answer with a page of the hub's own, not a redirect: the browser is
      # still on a navigation the dashboard started, and it may withhold the
      # SameSite=Strict session cookie from any redirect in that chain. The
      # page's own navigation home is same-site, so the cookie goes with it.
      conn
      |> clear_session()
      |> configure_session(renew: true)
      |> put_session("hub_handle", handle)
      |> page(200, PortalWeb.SigninHTML.signed_in())
    else
      _refused -> page(conn, 401, PortalWeb.SigninHTML.failed())
    end
  end

  def confirm_signout(conn, _params) do
    page(conn, 200, PortalWeb.SigninHTML.signout(get_csrf_token()))
  end

  def signout(conn, _params) do
    revoked =
      case get_session(conn, "hub_handle") do
        handle when is_binary(handle) -> Portal.Gov.revoke(handle)
        _none -> :ok
      end

    case revoked do
      :ok ->
        conn
        |> configure_session(drop: true)
        |> redirect(to: ~p"/signin")

      # The handle is still live on the server, so keep it: dropping only
      # the cookie would leave a session any copy of that cookie could use.
      {:error, _reason} ->
        page(conn, 503, PortalWeb.SigninHTML.signout_failed(get_csrf_token()))
    end
  end

  defp page(conn, status, body) do
    conn
    |> put_resp_content_type("text/html")
    |> send_resp(status, body)
  end

  defp random_token, do: Base.url_encode64(:crypto.strong_rand_bytes(32), padding: false)
end
