defmodule UnitaresLeasePlane.HTTPRouter.HealthTest do
  @moduledoc """
  Wave 2 §"Lease-integration boundary hardening" — Phase C (supervised health).

  Pins the server side of the /v1/health contract:
  - 200 with `{ok: true, status: "ok"}` envelope
  - `protocol_version` injected by the shared `json/3` helper
  - Bearer auth applies (consistent with other /v1/* routes)
  - Missing/wrong bearer → 401 (auth-layer 401 carries protocol_version
    too as of Phase A.5; pinned in `protocol_version_test.exs`)
  - GET-only — POST returns 404 (Plug.Router won't match a different verb)

  The Python test (`tests/test_lease_plane_health_check.py`) pins the
  client side of the same handshake (HealthOk parsing, failure-safe
  contract, timeout override).
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias UnitaresLeasePlane.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  setup do
    Application.put_env(:lease_plane, :bearer_token, @bearer)
    :ok
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")
  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  test "GET /v1/health with valid bearer returns 200 + ok+status+protocol_version" do
    resp =
      :get
      |> conn("/v1/health")
      |> authed()
      |> HTTPRouter.call(@opts)

    assert resp.status == 200
    body = parsed(resp)
    # Minimal Phase C envelope.
    assert body["ok"] == true
    assert body["status"] == "ok"
    # protocol_version injected by json/3 — same contract as every other route.
    assert body["protocol_version"] == HTTPRouter.protocol_version()
  end

  test "GET /v1/health without bearer returns 401" do
    # Auth plug runs before route matching — same gating as every other path.
    resp =
      :get
      |> conn("/v1/health")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
    body = parsed(resp)
    assert body["error"] == "permission_denied"
  end

  test "GET /v1/health with wrong bearer returns 401" do
    resp =
      :get
      |> conn("/v1/health")
      |> put_req_header("authorization", "Bearer wrong-token")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
  end

  test "POST /v1/health returns 404 (route is GET-only)" do
    # The Phase C contract is a GET endpoint. A future PR could add POST
    # for richer probes (e.g., active connection check), but that's an
    # additive change. For now, POST should not match — pin that so a
    # future refactor doesn't accidentally accept POST and surprise
    # clients that rely on shape.
    resp =
      :post
      |> conn("/v1/health", "{}")
      |> put_req_header("content-type", "application/json")
      |> authed()
      |> HTTPRouter.call(@opts)

    assert resp.status == 404
  end

  test "GET /v1/health response shape stays minimal (Phase C wedge)" do
    # Pin the literal envelope shape so a future wide change to /v1/health
    # gets reviewed deliberately. Adding new fields is fine (additive,
    # tolerated by clients via Pydantic extra="ignore"); REMOVING fields
    # would break the contract.
    resp =
      :get
      |> conn("/v1/health")
      |> authed()
      |> HTTPRouter.call(@opts)

    body = parsed(resp)
    # Required keys.
    for key <- ~w(ok status protocol_version) do
      assert Map.has_key?(body, key), "missing required health key: #{key}"
    end
  end
end
