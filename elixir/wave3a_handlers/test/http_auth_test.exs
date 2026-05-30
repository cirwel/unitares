defmodule Wave3aHandlers.HTTPAuthTest do
  @moduledoc """
  Fail-closed bearer-auth coverage for the Wave 3a BEAM listener.

  Pins RFC §2.5: missing `WAVE_3A_BEAM_TOKEN` → 503 (not silently open);
  wrong bearer → 401; right bearer → handler dispatch fires. Mirrors the
  lease plane's auth coverage from `http_router_test.exs` "bearer auth"
  describe block.
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias Wave3aHandlers.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  setup do
    Application.put_env(:wave3a_handlers, :beam_token, @bearer)

    on_exit(fn ->
      Application.put_env(:wave3a_handlers, :beam_token, @bearer)
    end)

    :ok
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")
  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  describe "fail-closed: WAVE_3A_BEAM_TOKEN unset" do
    setup do
      Application.put_env(:wave3a_handlers, :beam_token, nil)
      :ok
    end

    test "POST /v1/handlers/:tool_name with correct-looking bearer still returns 503" do
      # Token unset on the server — no token the client could present
      # authorizes. Verifies fail-closed: the absence of configured token
      # never silently opens a route.
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer #{@bearer}")
        |> HTTPRouter.call(@opts)

      assert resp.status == 503
      assert parsed(resp)["error"] == "service_unavailable"
    end

    test "POST /v1/handlers/:tool_name without Authorization header returns 503" do
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      assert resp.status == 503
    end

    test "empty-string token is treated as unset (still 503)" do
      Application.put_env(:wave3a_handlers, :beam_token, "")

      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer ")
        |> HTTPRouter.call(@opts)

      assert resp.status == 503
    end

    test "GET /health is still reachable (auth-exempt liveness)" do
      # Liveness probe must work during a token rotation; gating it would
      # make the rotation indistinguishable from a real outage.
      resp =
        :get
        |> conn("/health")
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      assert parsed(resp)["ok"] == true
    end
  end

  describe "token configured" do
    test "missing Authorization header returns 401" do
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      assert parsed(resp)["error"] == "permission_denied"
    end

    test "wrong bearer returns 401" do
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer wrong-token")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
    end

    test "lowercase 'bearer' scheme accepted (RFC 7235 §2.1)" do
      # Same interoperability rule as the lease plane — Python httpx
      # defaults to lowercase 'bearer' and we shouldn't break it.
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "bearer #{@bearer}")
        |> HTTPRouter.call(@opts)

      assert resp.status == 501
      assert parsed(resp)["error"] == "not_implemented"
    end

    test "right bearer + GET /health returns 200" do
      # Sanity: presenting the right bearer to an auth-exempt route still
      # works (auth plug exits early, route handler dispatches).
      resp =
        :get
        |> conn("/health")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
    end

    test "right bearer + POST /v1/handlers/anything returns 501 (empty dispatch)" do
      resp =
        :post
        |> conn("/v1/handlers/any_tool", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 501
      assert parsed(resp)["error"] == "not_implemented"
    end

    test "malformed Authorization header (no 'Bearer' scheme) returns 401" do
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "NotBearer #{@bearer}")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
    end

    test "Authorization header with token only (no scheme) returns 401" do
      resp =
        :post
        |> conn("/v1/handlers/anything", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", @bearer)
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
    end
  end
end
