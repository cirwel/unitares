defmodule PortalWeb.HealthController do
  use PortalWeb, :controller

  # Liveness only: nothing about sessions, the operator or the hub's state.
  def index(conn, _params), do: json(conn, %{ok: true})
end
