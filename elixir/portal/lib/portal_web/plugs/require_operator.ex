defmodule PortalWeb.Plugs.RequireOperator do
  @moduledoc """
  Refuses a request without a signed-in operator (`Portal.Auth.verify/1`).
  Fails closed: any refusal, whatever its reason, renders the same page and
  leaves the session untouched.
  """
  @behaviour Plug
  import Plug.Conn

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    case Portal.Auth.verify(get_session(conn)) do
      {:ok, principal} ->
        assign(conn, :operator, principal)

      {:error, _reason} ->
        conn
        |> put_resp_content_type("text/html")
        |> send_resp(401, PortalWeb.SigninHTML.required())
        |> halt()
    end
  end
end
