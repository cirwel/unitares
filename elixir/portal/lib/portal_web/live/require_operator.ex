defmodule PortalWeb.Live.RequireOperator do
  @moduledoc """
  The guard for the one `live_session`: a LiveView mounts, on the HTTP render
  and again on the socket connect, only for a signed-in operator. The socket
  check matters on its own, since a local process can open the socket
  without ever loading the page.
  """
  import Phoenix.LiveView

  def on_mount(:default, _params, session, socket) do
    case Portal.Auth.verify(session) do
      {:ok, principal} -> {:cont, Phoenix.Component.assign(socket, :operator, principal)}
      {:error, _reason} -> {:halt, redirect(socket, to: "/signin")}
    end
  end
end
