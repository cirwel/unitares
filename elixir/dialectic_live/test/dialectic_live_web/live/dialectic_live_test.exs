defmodule DialecticLiveWeb.DialecticLiveTest do
  use DialecticLiveWeb.ConnCase

  import Phoenix.LiveViewTest

  # The pane must render its own shell regardless of whether the upstream
  # governance MCP (:8767) is reachable — load_sessions degrades to an error
  # banner + empty list rather than crashing the mount. These assertions hold
  # whether or not a governance server is running on the test host.
  test "GET / dead-renders the dialectic pane", %{conn: conn} do
    conn = get(conn, ~p"/")
    assert html_response(conn, 200) =~ "Dialectic — live sessions"
  end

  test "live mount renders the pane heading", %{conn: conn} do
    {:ok, _view, html} = live(conn, ~p"/")
    assert html =~ "Dialectic — live sessions"
  end
end
