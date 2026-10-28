defmodule PortalWeb.Live.RequireOperator do
  @moduledoc """
  The guard for the one `live_session`: a LiveView mounts, on the HTTP render
  and again on the socket connect, only for a signed-in operator. The socket
  check matters on its own, since a local process can open the socket
  without ever loading the page.

  A connected page lives on after its mount, so the guard asks again every
  `config :portal, :reverify_ms` (default 30 s). The moment the answer is no,
  for instance because the operator signed out of the dashboard, the page is
  sent to `/signin` and the socket's process ends.
  """
  import Phoenix.LiveView

  @default_reverify_ms 30_000

  def on_mount(:default, _params, session, socket) do
    case Portal.Auth.verify(session) do
      {:ok, principal} ->
        socket = Phoenix.Component.assign(socket, :operator, principal)
        {:cont, keep_verifying(socket, session)}

      {:error, _reason} ->
        {:halt, redirect(socket, to: "/signin")}
    end
  end

  defp keep_verifying(socket, session) do
    if connected?(socket) do
      schedule()

      attach_hook(socket, :reverify_operator, :handle_info, fn
        :reverify_operator, socket ->
          case Portal.Auth.verify(session) do
            {:ok, principal} ->
              schedule()
              {:halt, Phoenix.Component.assign(socket, :operator, principal)}

            {:error, _reason} ->
              {:halt, redirect(socket, to: "/signin")}
          end

        _other, socket ->
          {:cont, socket}
      end)
    else
      socket
    end
  end

  defp schedule do
    Process.send_after(
      self(),
      :reverify_operator,
      Application.get_env(:portal, :reverify_ms, @default_reverify_ms)
    )
  end
end
