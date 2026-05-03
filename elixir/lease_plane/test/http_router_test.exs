defmodule UnitaresLeasePlane.HTTPRouterTest do
  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  import LeaseTestHelpers

  alias UnitaresLeasePlane.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  setup do
    Application.put_env(:lease_plane, :bearer_token, @bearer)
    surface = unique_surface_id("http")
    on_exit(fn -> cleanup_surface(surface) end)
    {:ok, surface: surface}
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")

  defp acquire_body(surface, opts \\ []) do
    %{
      surface_id: surface,
      surface_kind: Keyword.get(opts, :surface_kind, "test"),
      holder_agent_uuid: Keyword.get(opts, :holder_agent_uuid, random_uuid()),
      holder_kind: Keyword.get(opts, :holder_kind, "local_beam"),
      holder_class: Keyword.get(opts, :holder_class, "process_instance"),
      ttl_s: Keyword.get(opts, :ttl_s, 30),
      intent: "http test"
    }
  end

  defp post_json(path, body) do
    :post
    |> conn(path, Jason.encode!(body))
    |> put_req_header("content-type", "application/json")
    |> authed()
    |> HTTPRouter.call(@opts)
  end

  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  describe "bearer auth" do
    test "missing Authorization header → 401 permission_denied", ctx do
      resp =
        :post
        |> conn("/v1/lease/acquire", Jason.encode!(acquire_body(ctx.surface)))
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      assert parsed(resp)["error"] == "permission_denied"
    end

    test "wrong bearer → 401 permission_denied", ctx do
      resp =
        :post
        |> conn("/v1/lease/acquire", Jason.encode!(acquire_body(ctx.surface)))
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "Bearer wrong-token")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
    end

    test "no expected token configured → 503 fail-closed", ctx do
      Application.put_env(:lease_plane, :bearer_token, nil)
      on_exit(fn -> Application.put_env(:lease_plane, :bearer_token, @bearer) end)

      resp = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      assert resp.status == 503
      assert parsed(resp)["error"] == "service_unavailable"
    end
  end

  describe "POST /v1/lease/acquire" do
    test "happy path returns ok=true with a fully-populated lease", ctx do
      resp = post_json("/v1/lease/acquire", acquire_body(ctx.surface))

      assert resp.status == 200
      body = parsed(resp)
      assert body["ok"] == true
      assert body["idempotent"] == false
      assert body["drift_warning"] == []

      lease = body["lease"]
      assert lease["surface_id"] == ctx.surface
      assert lease["holder_kind"] == "local_beam"
      assert lease["heartbeat_required"] == false
      assert lease["original_ttl_s"] == 30
      assert lease["earned_status"] == "provisional"
      assert lease["released_at"] == nil
      assert is_binary(lease["lease_id"])
      assert is_binary(lease["expires_at"])
    end

    test "idempotent retry from same holder", ctx do
      body = acquire_body(ctx.surface)

      resp1 = post_json("/v1/lease/acquire", body)
      assert resp1.status == 200
      assert parsed(resp1)["idempotent"] == false

      resp2 = post_json("/v1/lease/acquire", body)
      assert resp2.status == 200
      assert parsed(resp2)["idempotent"] == true
    end

    test "different holder → 409 held_by_other", ctx do
      body_a = acquire_body(ctx.surface)
      body_b = acquire_body(ctx.surface, holder_agent_uuid: random_uuid())

      assert post_json("/v1/lease/acquire", body_a).status == 200

      resp = post_json("/v1/lease/acquire", body_b)
      assert resp.status == 409

      payload = parsed(resp)
      assert payload["error"] == "held_by_other"
      assert payload["held_by_uuid"] == body_a.holder_agent_uuid
      assert is_binary(payload["expires_at"])
      # PR 5 council BLOCK fix: 409 body MUST carry the v0.7 §7.3.2 extended
      # AcquireHeldByOther fields. Without these, the Python Pydantic model
      # rejects the response and acquire_with_retry never retries.
      assert payload["surface_id"] == ctx.surface
      assert is_binary(payload["blocking_lease_id"])
      assert is_integer(payload["retry_after_hint_ms"])
      assert payload["retry_after_hint_ms"] >= 0
      assert payload["retry_after_hint_ms"] <= 5_000
    end

    test "holder_class='role' → 422 schema_invalid (rejected before DB)", ctx do
      body = acquire_body(ctx.surface, holder_class: "role")
      resp = post_json("/v1/lease/acquire", body)
      assert resp.status == 422
      assert parsed(resp)["error"] == "schema_invalid"
    end

    test "missing ttl_s → 422 schema_invalid", ctx do
      body = ctx.surface |> acquire_body() |> Map.delete(:ttl_s)
      resp = post_json("/v1/lease/acquire", body)
      assert resp.status == 422
    end

    test "ttl_s out of (0, 3600] → 422", ctx do
      assert post_json("/v1/lease/acquire", acquire_body(ctx.surface, ttl_s: 0)).status == 422

      assert post_json("/v1/lease/acquire", acquire_body(ctx.surface, ttl_s: 4000)).status ==
               422
    end

    test "acquire succeeds without surface_kind in body (v0.7 drift fix; RFC §7.2.3)", ctx do
      # Post-migration-026, surface_kind is a generated column derived from
      # split_part(surface_id, ':', 1). The router silently ignores caller-supplied
      # surface_kind and never includes it in the Repo INSERT. This test verifies
      # acquire succeeds with surface_kind absent from the body entirely.
      body = ctx.surface |> acquire_body() |> Map.delete(:surface_kind)
      resp = post_json("/v1/lease/acquire", body)

      assert resp.status == 200
      payload = parsed(resp)
      assert payload["ok"] == true
      # Server echoes surface_kind from the generated column on the lease record.
      assert payload["lease"]["surface_kind"] == "dialectic"
    end

    # --- §9 RFC named-gate tests (mirroring docs/proposals/surface-lease-plane-v0.md §9) ---
    #
    # These pin the gates by their §9-named description so the audit
    # (scripts/dev/audit_rfc_section_9_gates.py) reports them as exact rather
    # than missing. Coverage overlaps with the in-house tests above; keeping
    # both makes the §9 audit trustworthy without losing the more descriptive
    # names that aid local debugging.

    test "http_router returns 409 on held_by_other", ctx do
      # RFC §7.3.5 / §9 gate: HTTP 409 on held_by_other.
      body_a = acquire_body(ctx.surface)
      body_b = acquire_body(ctx.surface, holder_agent_uuid: random_uuid())

      assert post_json("/v1/lease/acquire", body_a).status == 200
      resp = post_json("/v1/lease/acquire", body_b)
      assert resp.status == 409
      assert parsed(resp)["error"] == "held_by_other"
    end

    test "http_router rejects surface_kind in acquire body after migration 026", ctx do
      # RFC §7.2.3 / §9 gate: post-migration-026, surface_kind is a generated
      # column derived from split_part(surface_id, ':', 1). The router silently
      # ignores caller-supplied surface_kind in the body — the caller's value
      # cannot override the generated column ("rejected" in the take-effect
      # sense, even though no 4xx is returned). See http_router.ex:240-244.
      body =
        ctx.surface
        |> acquire_body()
        |> Map.put(:surface_kind, "lying_kind_does_not_match_scheme")

      resp = post_json("/v1/lease/acquire", body)

      assert resp.status == 200
      payload = parsed(resp)
      assert payload["ok"] == true
      # The generated column wins; the body's "lying_kind_does_not_match_scheme"
      # is dropped before the INSERT.
      assert payload["lease"]["surface_kind"] == "dialectic"
    end

    test "PR 7 — non-canonical scheme → 422 schema_invalid", _ctx do
      body = acquire_body("ftp://nope")
      resp = post_json("/v1/lease/acquire", body)

      assert resp.status == 422
      payload = parsed(resp)
      assert payload["error"] == "schema_invalid"
      assert payload["detail"] =~ "canonicalization failed"
      assert payload["detail"] =~ "invalid_scheme"
    end

    test "PR 7 — capture:/ unsorted members → server stores canonical (sorted)" do
      # Per PR 7 council BLOCK 2 (reviewer): construct surface_canonical via
      # the Canonicalize helper itself, not by hand. This eliminates the
      # correct-by-coincidence alphabetical-ordering dependency and ensures
      # cleanup targets the same row even if canonicalize ever changes.
      rand = Base.encode16(:crypto.strong_rand_bytes(4), case: :lower)
      surface_in = "capture:/test_zeta_#{rand},test_alpha,test_gamma"
      {:ok, surface_canonical} = UnitaresLeasePlane.Canonicalize.canonicalize(surface_in)
      on_exit(fn -> cleanup_surface(surface_canonical) end)

      body = acquire_body(surface_in)
      resp = post_json("/v1/lease/acquire", body)

      assert resp.status == 200
      lease = parsed(resp)["lease"]
      # Canonicalization happened server-side: stored surface_id is sorted.
      assert lease["surface_id"] == surface_canonical
      # Ditto for the generated surface_kind column.
      assert lease["surface_kind"] == "capture"
    end

    test "PR 7 — dialectic:/ uppercase → server stores canonical (lowercased)" do
      rand = Base.encode16(:crypto.strong_rand_bytes(4), case: :lower)
      surface_in = "dialectic:/Test_Elixir_PR7_#{rand}"
      surface_canonical = String.downcase(surface_in)
      on_exit(fn -> cleanup_surface(surface_canonical) end)

      body = acquire_body(surface_in)
      resp = post_json("/v1/lease/acquire", body)

      assert resp.status == 200
      assert parsed(resp)["lease"]["surface_id"] == surface_canonical
    end
  end

  describe "GET /v1/lease/status" do
    test "unknown surface → ok with lease=nil" do
      resp =
        :get
        |> conn("/v1/lease/status?surface_id=dialectic:/test_elixir_http_never_acquired")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      assert parsed(resp)["lease"] == nil
    end

    test "non-canonical scheme → 422 schema_invalid" do
      # PR 7 — server-side canonicalization rejects schemes outside the v0.8
      # canonical list. Pre-PR-7 this would have hit the underlying status
      # query and returned nil; post-PR-7 it's a typed-absence error.
      resp =
        :get
        |> conn("/v1/lease/status?surface_id=ftp://nope")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 422
      body = parsed(resp)
      assert body["error"] == "schema_invalid"
      assert body["detail"] =~ "canonicalization failed"
      assert body["detail"] =~ "invalid_scheme"
    end

    test "active surface → ok with lease record", ctx do
      assert post_json("/v1/lease/acquire", acquire_body(ctx.surface)).status == 200

      resp =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      assert parsed(resp)["lease"]["surface_id"] == ctx.surface
    end

    test "missing surface_id → 422", _ctx do
      resp =
        :get
        |> conn("/v1/lease/status")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 422
    end
  end

  describe "POST /v1/lease/renew + /heartbeat" do
    test "extends expires_at on a held lease", ctx do
      acquire = post_json("/v1/lease/acquire", acquire_body(ctx.surface, ttl_s: 60))
      assert acquire.status == 200
      lease_id = parsed(acquire)["lease"]["lease_id"]
      original_expiry = parsed(acquire)["lease"]["expires_at"]

      Process.sleep(1100)
      renew = post_json("/v1/lease/renew", %{lease_id: lease_id})
      assert renew.status == 200
      assert parsed(renew) == %{"ok" => true}

      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      new_expiry = parsed(status)["lease"]["expires_at"]
      assert new_expiry > original_expiry
    end

    test "heartbeat alias works the same", ctx do
      acquire = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      lease_id = parsed(acquire)["lease"]["lease_id"]

      hb = post_json("/v1/lease/heartbeat", %{lease_id: lease_id})
      assert hb.status == 200
    end

    test "missing lease_id → 422" do
      resp = post_json("/v1/lease/renew", %{})
      assert resp.status == 422
    end
  end

  describe "POST /v1/lease/release" do
    test "release happy path returns ok=true", ctx do
      acquire = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      lease_id = parsed(acquire)["lease"]["lease_id"]

      resp = post_json("/v1/lease/release", %{lease_id: lease_id, release_reason: "normal"})
      assert resp.status == 200
      assert parsed(resp) == %{"ok" => true}

      # Subsequent status returns nil — released
      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert parsed(status)["lease"] == nil
    end

    test "invalid release_reason → 422 schema_invalid", _ctx do
      resp = post_json("/v1/lease/release", %{lease_id: random_uuid(), release_reason: "bogus"})
      assert resp.status == 422
    end
  end

  describe "POST /v1/lease/handoff/{offer,accept}" do
    test "offer returns a handoff_id for an active lease", ctx do
      acquire = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      lease_id = parsed(acquire)["lease"]["lease_id"]

      resp =
        post_json("/v1/lease/handoff/offer", %{
          lease_id: lease_id,
          to_holder_agent_uuid: random_uuid(),
          ttl_s: 30
        })

      assert resp.status == 200
      body = parsed(resp)
      assert body["ok"] == true
      assert is_binary(body["handoff_id"])
    end

    test "accept closes the old lease and reacquires for the recipient", ctx do
      acquire = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      old_lease_id = parsed(acquire)["lease"]["lease_id"]
      to_holder = random_uuid()

      offer =
        post_json("/v1/lease/handoff/offer", %{
          lease_id: old_lease_id,
          to_holder_agent_uuid: to_holder,
          ttl_s: 45
        })

      handoff_id = parsed(offer)["handoff_id"]
      accept = post_json("/v1/lease/handoff/accept", %{handoff_id: handoff_id})
      assert accept.status == 200
      assert parsed(accept) == %{"ok" => true}

      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      lease = parsed(status)["lease"]
      assert lease["holder_agent_uuid"] == to_holder
      assert lease["holder_kind"] == "remote_heartbeat"
      assert lease["heartbeat_required"] == true
      assert lease["original_ttl_s"] == 45
      refute lease["lease_id"] == old_lease_id
    end

    test "accept of an unknown handoff returns 404", _ctx do
      resp = post_json("/v1/lease/handoff/accept", %{handoff_id: random_uuid()})
      assert resp.status == 404
      assert parsed(resp)["error"] == "not_found"
    end
  end

  describe "404" do
    test "unknown route returns typed-absence not_found" do
      resp =
        :get
        |> conn("/v1/lease/nope")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 404
      assert parsed(resp)["error"] == "not_found"
    end
  end

  describe "auth-bypass and parser hardening (council #253 fixes)" do
    test "malformed JSON WITHOUT auth → 401, not 400 — auth gates first", _ctx do
      resp =
        :post
        |> conn("/v1/lease/acquire", "not json {")
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      assert parsed(resp)["error"] == "permission_denied"
    end

    test "malformed JSON WITH auth → 422 typed-absence, not 400 empty", _ctx do
      resp =
        :post
        |> conn("/v1/lease/acquire", "not json {")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 422
      body = parsed(resp)
      assert body["ok"] == false
      assert body["error"] == "schema_invalid"
      assert is_binary(body["detail"])
    end

    test "unsupported content-type → 415 typed-absence", _ctx do
      resp =
        :post
        |> conn("/v1/lease/acquire", "<xml/>")
        |> put_req_header("content-type", "application/xml")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 415
      assert parsed(resp)["error"] == "schema_invalid"
    end

    test "handle_errors/2 emits typed 503 with redacted reason (no inspect leak)", _ctx do
      # Plug.ErrorHandler sends the 503 response and *then re-raises* the original
      # error so logging middleware further up the chain can record it. Testing
      # the full path through Plug.Test is brittle (the WrapperError carries the
      # pre-handler conn). Direct test of handle_errors/2 is the same coverage —
      # what matters is the response shape, not which integration triggered it.
      conn = conn(:post, "/v1/lease/renew")

      result =
        HTTPRouter.handle_errors(conn, %{
          kind: :error,
          reason: %RuntimeError{message: "postgresql password=hunter2 leaked"},
          stack: []
        })

      assert result.status == 503
      body = Jason.decode!(result.resp_body)

      assert body == %{
               "ok" => false,
               "error" => "service_unavailable",
               "reason" => "internal error"
             }

      # Verify the leaky inspect string never made it to the wire.
      refute result.resp_body =~ "hunter2"
    end
  end
end
