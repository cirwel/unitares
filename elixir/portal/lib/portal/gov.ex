defmodule Portal.Gov do
  @moduledoc """
  The governance server's hub sign-in handoff, from the hub's side
  (portal-hub-v0 H2c; the server half is `src/dashboard_auth.py`).

  The browser leg goes to `authorize_url/2` on the dashboard's origin. The
  three internal calls (`exchange/2`, `check/1`, `revoke/1`) go over the
  server's owner-only Unix socket with the hub credential, because the
  server answers them nowhere else.

  Configured by `config :portal, :gov` with `:origin` (the dashboard, where
  passkeys live), `:socket` (the server's `UNITARES_UDS_SOCKET`) and
  `:token` (the raw hub key, at least 32 characters; the server's
  `UNITARES_HUB_TOKEN` holds only its `sha256:` digest). Without all three the
  hub stays signed out.
  """

  @timeout_ms 5_000

  @doc "Whether the origin, socket and token are all set."
  def configured? do
    config = config()
    Enum.all?([:origin, :socket, :token], &present?(config[&1]))
  end

  @doc "Where the browser goes to approve the hub."
  def authorize_url(challenge, state) do
    query =
      URI.encode_query(%{
        "code_challenge" => challenge,
        "code_challenge_method" => "S256",
        "state" => state
      })

    config()[:origin] <> "/auth/hub/authorize?" <> query
  end

  @doc "Trade a one-time code and its PKCE verifier for a hub session handle."
  def exchange(code, verifier) do
    case post("/v1/hub/exchange", %{"code" => code, "code_verifier" => verifier}) do
      {:ok, 200, %{"handle" => handle} = body} when is_binary(handle) -> {:ok, body}
      {:ok, _status, _body} -> {:error, :refused}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "Whether a hub session is live, with who signed it in and whether that was recent."
  def check(handle) do
    case post("/v1/hub/check", %{"handle" => handle}) do
      {:ok, 200, %{"ok" => true} = body} -> {:ok, body}
      {:ok, 401, _body} -> {:error, :signed_out}
      {:ok, _status, _body} -> {:error, :unavailable}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "End a hub session on the server."
  def revoke(handle) do
    case post("/v1/hub/revoke", %{"handle" => handle}) do
      {:ok, 200, _body} -> :ok
      {:ok, _status, _body} -> {:error, :refused}
      {:error, reason} -> {:error, reason}
    end
  end

  defp post(path, body) do
    config = config()

    options =
      [
        url: "http://localhost" <> path,
        unix_socket: config[:socket],
        json: body,
        auth: {:bearer, config[:token]},
        retry: false,
        redirect: false,
        receive_timeout: @timeout_ms,
        connect_options: [timeout: @timeout_ms]
      ]
      |> Keyword.merge(Application.get_env(:portal, :gov_req_options, []))

    if configured?() do
      case Req.post(options) do
        {:ok, %Req.Response{status: status, body: body}} -> {:ok, status, body}
        {:error, _exception} -> {:error, :unreachable}
      end
    else
      {:error, :not_configured}
    end
  end

  defp config, do: Application.get_env(:portal, :gov, [])

  defp present?(value), do: is_binary(value) and value != ""
end
