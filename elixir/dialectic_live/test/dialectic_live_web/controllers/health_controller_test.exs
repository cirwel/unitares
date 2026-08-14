defmodule DialecticLiveWeb.HealthControllerTest do
  use DialecticLiveWeb.ConnCase, async: true

  test "GET /health returns 200 with a machine-readable body", %{conn: conn} do
    conn = get(conn, ~p"/health")
    assert json_response(conn, 200) == %{"status" => "ok", "service" => "dialectic_live"}
  end

  # The point of the route is that deploy-status.sh's unconditional /health
  # probe stops reporting "Not Found" for a perfectly healthy service. A 200 is
  # the whole contract; assert it explicitly so a future router reshuffle that
  # buries /health behind the browser pipeline fails here instead of silently
  # turning the fleet dashboard misleading again.
  test "/health is not behind the browser pipeline", %{conn: conn} do
    conn = get(conn, ~p"/health")
    refute conn.resp_body =~ "<!DOCTYPE html>"
    assert conn.status == 200
  end
end
