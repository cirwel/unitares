defmodule PortalWeb.Plugs.HostAllowlist do
  @moduledoc """
  Answers only for the configured Host and port (`Portal.Boundary.allowed/0`),
  before any other plug, so a DNS-rebinding page or a request for another
  name gets nothing. Exact match: no subdomains, no alternative port.
  """
  @behaviour Plug
  import Plug.Conn

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    if {conn.host, conn.port} == Portal.Boundary.allowed() do
      conn
    else
      conn
      |> put_resp_content_type("text/plain")
      |> send_resp(421, "misdirected request\n")
      |> halt()
    end
  end
end
