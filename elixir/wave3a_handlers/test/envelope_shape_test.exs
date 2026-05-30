defmodule Wave3aHandlers.EnvelopeShapeTest do
  @moduledoc """
  Load-bearing envelope-shape pin per RFC §2.2.

  This test is the analogue of
  `elixir/lease_plane/test/protocol_version_test.exs` and is the contract
  the Python proxy at `src/wave3a_beam_proxy.py::_validate_success_envelope`
  reads against. If a future PR changes the success/401/503 shape in a way
  that breaks the Python proxy's validator, this test fails before the
  change can land.

  ## What's pinned

  Per RFC §2.2 (verbatim):

      success:   {"ok": true,  "protocol_version": "wave3a.v1", ...}
      401:       {"ok": false, "protocol_version": "wave3a.v1",
                  "error": "permission_denied",
                  "reason": "bearer token missing or invalid"}
      503:       {"ok": false, "protocol_version": "wave3a.v1",
                  "error": "service_unavailable",
                  "reason": "WAVE_3A_BEAM_TOKEN not configured"}

  All top-level keys, never nested under a `data` envelope.

  ## FIND-V5/V7 council finding

  This test also pins `protocol_version == "wave3a.v1"` explicitly so a
  future drift toward the lease plane's `"v1.0"` is caught. The two
  contracts are independent by RFC §2.2 and that independence is what
  makes the Python proxy's `_validate_success_envelope` byte-exact.
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

  describe "protocol_version pin" do
    test "HTTPRouter.protocol_version/0 returns the literal 'wave3a.v1'" do
      # FIND-V5/V7: drift guard. This MUST match `PROTOCOL_VERSION` in
      # `src/wave3a_beam_proxy.py` and the §2.2 spec. Bumping requires
      # touching both sides in the same PR.
      assert HTTPRouter.protocol_version() == "wave3a.v1"
    end

    test "protocol_version is NOT the lease plane's 'v1.0'" do
      # FIND-V5/V7: the two contracts are intentionally distinct. If they
      # ever converge by accident the proxy validator silently accepts
      # lease-plane responses through the wave3a routing table.
      refute HTTPRouter.protocol_version() == "v1.0"
    end
  end

  describe "success envelope (§2.2)" do
    test "GET /health returns top-level {ok: true, protocol_version: 'wave3a.v1'}" do
      resp =
        :get
        |> conn("/health")
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      body = parsed(resp)

      # Top-level keys present, no nesting under 'data'.
      assert body["ok"] == true
      assert body["protocol_version"] == "wave3a.v1"
      refute Map.has_key?(body, "data"),
             "§2.2: envelope MUST be top-level keys, never nested under 'data'"
    end

    test "success body keys are a superset of {ok, protocol_version}" do
      # RFC §2.2 is loose on additional keys (handler-specific fields are
      # additive). The strict requirement is that `ok` and
      # `protocol_version` are top-level on every success response.
      resp =
        :get
        |> conn("/health")
        |> HTTPRouter.call(@opts)

      body = parsed(resp)
      keys = MapSet.new(Map.keys(body))
      required = MapSet.new(["ok", "protocol_version"])

      assert MapSet.subset?(required, keys),
             "§2.2: success envelope missing required top-level keys; got: #{inspect(Map.keys(body))}"
    end
  end

  describe "401 envelope (§2.2)" do
    test "missing Authorization header returns the verbatim 401 shape" do
      resp =
        :post
        |> conn("/v1/handlers/health_check", "{}")
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      body = parsed(resp)

      # Verbatim §2.2 401 shape — top-level keys.
      assert body == %{
               "ok" => false,
               "protocol_version" => "wave3a.v1",
               "error" => "permission_denied",
               "reason" => "bearer token missing or invalid"
             },
             "§2.2 401 envelope must match RFC verbatim; got: #{inspect(body)}"
    end

    test "wrong bearer returns the verbatim 401 shape (council fold: 401 includes 'reason')" do
      # v0.1 of the RFC missed the `reason` field on the 401 path; v0.2
      # restored it after the council fold against the lease plane's live
      # behavior. This test pins that the fix is present and cannot drift.
      resp =
        :post
        |> conn("/v1/handlers/health_check", "{}")
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer wrong-token")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      body = parsed(resp)

      assert Map.has_key?(body, "reason"),
             "council fold: 401 envelope MUST include 'reason' — v0.1 missed this"

      assert body["reason"] == "bearer token missing or invalid"
      assert body["error"] == "permission_denied"
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
    end

    test "even /health is unaffected by 401 envelope changes (it's auth-exempt)" do
      # Sanity check that the auth-exempt route does NOT accidentally return
      # the 401 envelope when no bearer is presented.
      resp =
        :get
        |> conn("/health")
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      assert parsed(resp)["ok"] == true
    end
  end

  describe "503 envelope (§2.2)" do
    test "WAVE_3A_BEAM_TOKEN unset returns the verbatim 503 shape" do
      Application.put_env(:wave3a_handlers, :beam_token, nil)

      resp =
        :post
        |> conn("/v1/handlers/health_check", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 503
      body = parsed(resp)

      # Verbatim §2.2 503 shape — top-level keys.
      assert body == %{
               "ok" => false,
               "protocol_version" => "wave3a.v1",
               "error" => "service_unavailable",
               "reason" => "WAVE_3A_BEAM_TOKEN not configured"
             },
             "§2.2 503 envelope must match RFC verbatim; got: #{inspect(body)}"
    end

    test "WAVE_3A_BEAM_TOKEN unset also 503s without any Authorization header" do
      Application.put_env(:wave3a_handlers, :beam_token, nil)

      resp =
        :post
        |> conn("/v1/handlers/health_check", "{}")
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      # When the expected token isn't configured, the plug returns 503
      # regardless of whether the caller supplied an Authorization header —
      # fail-closed posture wins over the "missing-auth → 401" branch.
      assert resp.status == 503
      assert parsed(resp)["error"] == "service_unavailable"
    end
  end

  describe "501 envelope (PR #4 empty dispatch)" do
    test "any tool name returns 501 with envelope" do
      # PR #4 ships an empty dispatch table; PR #5 cuts over the first real
      # handler. Verify the 501 shape pins now so PR #5's wiring is a
      # contract-shape diff, not an envelope-shape diff.
      resp =
        :post
        |> conn("/v1/handlers/health_check", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 501
      body = parsed(resp)

      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "not_implemented"
      assert body["reason"] == "handler not wired"
      assert body["tool_name"] == "health_check"
    end

    test "501 also fires on arbitrary tool names" do
      resp =
        :post
        |> conn("/v1/handlers/arbitrary_tool_name", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 501
      body = parsed(resp)
      assert body["tool_name"] == "arbitrary_tool_name"
      assert body["error"] == "not_implemented"
    end
  end

  describe "404 envelope" do
    test "unknown route returns typed not_found" do
      resp =
        :get
        |> conn("/no_such_path")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 404
      body = parsed(resp)
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "not_found"
    end
  end
end
