defmodule PortalWeb.SigninController do
  use PortalWeb, :controller

  def show(conn, _params) do
    conn
    |> put_resp_content_type("text/html")
    |> send_resp(401, PortalWeb.SigninHTML.required())
  end
end
