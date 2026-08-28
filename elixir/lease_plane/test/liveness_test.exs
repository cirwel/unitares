defmodule UnitaresLeasePlane.HTTPRouter.LivenessTest do
  @moduledoc """
  Pins the unauthenticated GET /health liveness probe — the deliberate,
  single, static exception to the fail-closed posture. Without it,
  up-and-fail-closed (401 on every path) is byte-for-byte indistinguishable
  from a probe of a nonexistent path, and operators cannot tell "service up"
  from "service down" without a bearer.

  The exemption is exactly one route: GET /health (root, unversioned).
  These tests pin BOTH sides of the contract:
  - GET /health without a bearer returns 200 with a fully static body
    (no identity_binding metrics, no config echo, no DB touch — it must
    stay 200 even when the bearer token is unconfigured or Postgres is
    down).
  - Everything else stays gated: POST /health, /healthz, /health/x, and
    /v1/lease/* without a bearer all still 401. /v1/health keeps its own
    401 contract, pinned separately in `health_test.exs`.
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

  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  test "GET /health with NO bearer returns 200 with static liveness body" do
    resp =
      :get
      |> conn("/health")
      |> HTTPRouter.call(@opts)

    assert resp.status == 200
    body = parsed(resp)
    assert body["ok"] == true
    assert body["status"] == "ok"
    assert body["service"] == "lease-plane"
    assert body["protocol_version"] == HTTPRouter.protocol_version()
    # The payload is static liveness ONLY — never the sensitive
    # identity_binding metrics that /v1/health carries behind auth.
    refute Map.has_key?(body, "identity_binding")
  end

  test "GET /health returns 200 even when no bearer token is configured" do
    # Liveness must not depend on config: an unconfigured token 503s on
    # every gated route (fail-closed), but the probe still answers.
    Application.delete_env(:lease_plane, :bearer_token)
    on_exit(fn -> Application.put_env(:lease_plane, :bearer_token, @bearer) end)

    resp =
      :get
      |> conn("/health")
      |> HTTPRouter.call(@opts)

    assert resp.status == 200
    assert parsed(resp)["ok"] == true
  end

  test "POST /health without bearer returns 401 (verb not exempt)" do
    resp =
      :post
      |> conn("/health", "{}")
      |> put_req_header("content-type", "application/json")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
    assert parsed(resp)["error"] == "permission_denied"
  end

  test "GET /healthz without bearer returns 401 (exact-match only)" do
    resp =
      :get
      |> conn("/healthz")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
    assert parsed(resp)["error"] == "permission_denied"
  end

  test "GET /health/x without bearer returns 401 (no subpaths exempt)" do
    resp =
      :get
      |> conn("/health/x")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
    assert parsed(resp)["error"] == "permission_denied"
  end

  test "GET /v1/lease/status without bearer still returns 401" do
    # The lease surface itself stays fully fail-closed — the liveness
    # exemption must not widen anything under /v1/lease/*.
    resp =
      :get
      |> conn("/v1/lease/status?surface_id=test")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
    assert parsed(resp)["error"] == "permission_denied"
  end

  test "GET /v1/lease/health without bearer still returns 401 (no such route, still gated)" do
    # The probe path operators actually tried (2026-08-27) — it is not a
    # route and must stay indistinguishable from any other gated path.
    resp =
      :get
      |> conn("/v1/lease/health")
      |> HTTPRouter.call(@opts)

    assert resp.status == 401
    assert parsed(resp)["error"] == "permission_denied"
  end
end
