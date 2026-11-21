defmodule UnitaresLeasePlane.HTTPRouterTest do
  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn
  import ExUnit.CaptureLog

  import LeaseTestHelpers

  alias UnitaresLeasePlane.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"
  @force_release_token "test-force-release-token-do-not-use-in-prod"

  setup do
    Application.put_env(:lease_plane, :bearer_token, @bearer)
    Application.put_env(:lease_plane, :force_release_token, @force_release_token)
    Application.put_env(:lease_plane, :identity_binding_mode, :off)

    Application.put_env(:lease_plane, :identity_verifier, fn holder, proof ->
      if proof == "proof:" <> holder, do: :ok, else: {:error, :invalid}
    end)

    surface = unique_surface_id("http")

    on_exit(fn ->
      cleanup_surface(surface)
      Application.put_env(:lease_plane, :identity_binding_mode, :off)
      Application.delete_env(:lease_plane, :identity_verifier)
    end)

    {:ok, surface: surface}
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")

  defp authed_force_release(conn),
    do: put_req_header(conn, "authorization", "Bearer #{@force_release_token}")

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
    {body, principal} = bind_test_principal(path, body)

    conn =
      :post
      |> conn(path, Jason.encode!(body))
      |> put_req_header("content-type", "application/json")
      |> authed()

    conn =
      if principal,
        do: put_req_header(conn, "x-unitares-identity-proof", "proof:" <> principal),
        else: conn

    HTTPRouter.call(conn, @opts)
  end

  defp post_json_without_proof(path, body) do
    :post
    |> conn(path, Jason.encode!(body))
    |> put_req_header("content-type", "application/json")
    |> authed()
    |> HTTPRouter.call(@opts)
  end

  defp bind_test_principal("/v1/lease/acquire", body),
    do: {body, Map.get(body, :holder_agent_uuid) || Map.get(body, "holder_agent_uuid")}

  defp bind_test_principal(path, body)
       when path in [
              "/v1/lease/renew",
              "/v1/lease/heartbeat",
              "/v1/lease/release",
              "/v1/lease/handoff/offer"
            ] do
    lease_id = Map.get(body, :lease_id) || Map.get(body, "lease_id")

    principal =
      if is_binary(lease_id) do
        case UnitaresLeasePlane.Repo.active_lease(lease_id) do
          {:ok, lease} -> lease.holder_agent_uuid
          _ -> nil
        end
      else
        nil
      end

    {body, principal}
  end

  defp bind_test_principal("/v1/lease/handoff/accept", body) do
    handoff_id = Map.get(body, :handoff_id) || Map.get(body, "handoff_id")

    principal =
      case UnitaresLeasePlane.HandoffServer.authorization_context(handoff_id) do
        {:ok, %{recipient_uuid: recipient_uuid}} -> recipient_uuid
        _ -> nil
      end

    {body, principal}
  end

  defp bind_test_principal("/v1/dialectic/session", body),
    do: {body, Map.get(body, :paused_agent_id) || Map.get(body, "paused_agent_id")}

  defp bind_test_principal(path, body)
       when path in [
              "/v1/dialectic/phase",
              "/v1/dialectic/reviewer",
              "/v1/dialectic/reviewer/claim"
            ] do
    session_id = Map.get(body, :session_id) || Map.get(body, "session_id")

    principals =
      case UnitaresLeasePlane.DialecticSaga.get_session_principals(session_id) do
        {:ok, value} -> value
        _ -> nil
      end

    claim =
      path == "/v1/dialectic/reviewer/claim" or
        Map.get(body, :claim_if_unassigned) == true or
        Map.get(body, "claim_if_unassigned") == true

    principal =
      if claim do
        Map.get(body, :reviewer_agent_id) || Map.get(body, "reviewer_agent_id")
      else
        principals && principals.paused_agent_id
      end

    body = Map.put(body, :actor_agent_id, principal || "unknown-test-actor")
    {body, principal}
  end

  defp bind_test_principal(path, body)
       when path in [
              "/v1/dialectic/resolve",
              "/v1/dialectic/resolve-with-effect",
              "/v2/dialectic/resolve-with-effect"
            ],
       do: {body, Map.get(body, :reviewer_agent_id) || Map.get(body, "reviewer_agent_id")}

  defp bind_test_principal(_path, body), do: {body, nil}

  defp post_json_with_proof(path, body, proof) do
    :post
    |> conn(path, Jason.encode!(body))
    |> put_req_header("content-type", "application/json")
    |> put_req_header("x-unitares-identity-proof", proof)
    |> authed()
    |> HTTPRouter.call(@opts)
  end

  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  defp reviewed_work_authorship(author, declared_by) do
    %{
      "_dialectic_reviewed_work_authorship" => %{
        "schema" => "unitares.dialectic-reviewed-work-authorship.v1",
        "status" => "declared",
        "reviewed_work_author_agent_ids" => [author],
        "declared_by_agent_id" => declared_by,
        "authorship_verified" => false
      }
    }
  end

  describe "/v1/dialectic/phase" do
    test "advances a non-terminal phase (200)" do
      session_id = insert_dialectic_session(phase: "thesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      resp = post_json("/v1/dialectic/phase", %{session_id: session_id, phase: "antithesis"})
      assert resp.status == 200
      assert parsed(resp)["phase"] == "antithesis"
    end

    test "invalid phase -> 422" do
      resp = post_json("/v1/dialectic/phase", %{session_id: "x", phase: "resolved"})
      assert resp.status == 422
    end

    test "missing session -> 404" do
      resp =
        post_json("/v1/dialectic/phase", %{
          session_id: "test_elixir_nope_#{System.unique_integer([:positive])}",
          phase: "antithesis"
        })

      assert resp.status == 404
    end
  end

  describe "/v1/dialectic/session" do
    test "creates a session (201) then idempotent replay (200)" do
      sid = "test_elixir_httpcreate_" <> Integer.to_string(System.unique_integer([:positive]))
      on_exit(fn -> cleanup_dialectic_session(sid) end)

      r1 =
        post_json("/v1/dialectic/session", %{
          session_id: sid,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          reason: "test"
        })

      assert r1.status == 201
      assert parsed(r1)["created"] == true

      r2 = post_json("/v1/dialectic/session", %{session_id: sid, paused_agent_id: "p"})
      assert r2.status == 200
      assert parsed(r2)["created"] == false
    end

    test "missing fields → 422 schema_invalid" do
      assert post_json("/v1/dialectic/session", %{session_id: "x"}).status == 422
    end
  end

  describe "/v1/dialectic/presence" do
    test "lists live sessions; resolved ones drop off" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      live =
        :get
        |> conn("/v1/dialectic/presence")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert live.status == 200
      body = parsed(live)
      assert body["ok"] == true
      assert Enum.any?(body["sessions"], &(&1["session_id"] == session_id))

      # Resolve it, then it should no longer appear.
      post_json("/v1/dialectic/resolve", %{
        session_id: session_id,
        paused_agent_id: "p",
        reviewer_agent_id: "r",
        resolution: %{verdict: "resume"}
      })

      after_resolve =
        :get |> conn("/v1/dialectic/presence") |> authed() |> HTTPRouter.call(@opts)

      refute Enum.any?(parsed(after_resolve)["sessions"], &(&1["session_id"] == session_id))
    end
  end

  describe "/v1/dialectic/resolve" do
    test "missing fields → 422 schema_invalid" do
      resp = post_json("/v1/dialectic/resolve", %{session_id: "x"})
      assert resp.status == 422
      assert parsed(resp)["error"] == "schema_invalid"
    end

    test "resolves a synthesis session and commits row + saga" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{verdict: "resume", conditions: ["monitor"]}
        })

      assert resp.status == 200
      body = parsed(resp)
      assert body["ok"] == true
      assert body["status"] == "resolved"
      assert is_binary(body["saga_id"])
    end

    test "second resolve of a resolved session is idempotent (200, already_terminal)" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      body = %{
        session_id: session_id,
        paused_agent_id: "p",
        reviewer_agent_id: "r",
        resolution: %{verdict: "resume"}
      }

      assert post_json("/v1/dialectic/resolve", body).status == 200
      resp2 = post_json("/v1/dialectic/resolve", body)
      assert resp2.status == 200
      assert parsed(resp2)["origin"] == "already_terminal"
    end

    test "status=failed commits a failed terminal transition" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{reason: "safety_violation"},
          status: "failed"
        })

      assert resp.status == 200
      assert parsed(resp)["status"] == "failed"
    end

    test "recused reviewer -> 409 reviewer_recused" do
      proxy = "11111111-1111-4111-8111-111111111111"
      author = "22222222-2222-4222-8222-222222222222"

      session_id =
        insert_dialectic_session(
          paused_agent_id: proxy,
          reviewer_agent_id: author
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(reviewed_work_authorship(author, proxy))]
      )

      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: session_id,
          paused_agent_id: proxy,
          reviewer_agent_id: author,
          resolution: %{verdict: "resume"}
        })

      assert resp.status == 409
      assert parsed(resp)["error"] == "reviewer_recused"
    end

    test "pause-bound self-review -> 409 self_review_not_authorizing" do
      session_id =
        insert_dialectic_session(
          paused_agent_id: "self-reviewer",
          reviewer_agent_id: "self-reviewer"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [
          session_id,
          Jason.encode!(%{
            "pause_effect_required" => true,
            "pause_token" => "pause-self"
          })
        ]
      )

      resp =
        post_json("/v1/dialectic/resolve-with-effect", %{
          session_id: session_id,
          paused_agent_id: "self-reviewer",
          reviewer_agent_id: "self-reviewer",
          resolution: %{verdict: "resume"}
        })

      assert resp.status == 409
      assert parsed(resp)["error"] == "self_review_not_authorizing"
    end

    test "caller participant mismatch -> 409 session_identity_mismatch" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "wrong",
          resolution: %{verdict: "resume"}
        })

      assert resp.status == 409
      assert parsed(resp)["error"] == "session_identity_mismatch"
    end

    test "resolved transition outside synthesis -> 409 session_phase_mismatch" do
      session_id =
        insert_dialectic_session(
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          phase: "antithesis"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{verdict: "resume"}
        })

      assert resp.status == 409
      assert parsed(resp)["error"] == "session_phase_mismatch"
    end

    test "stale transcript frontier -> 409 session_revision_mismatch" do
      session_id =
        insert_dialectic_session(
          paused_agent_id: "p",
          reviewer_agent_id: "r"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE core.dialectic_sessions SET transcript_revision = 3 WHERE session_id = $1",
        [session_id]
      )

      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{verdict: "resume"},
          expected_phase: "synthesis",
          expected_transcript_revision: 2
        })

      assert resp.status == 409
      assert parsed(resp)["error"] == "session_revision_mismatch"
    end

    test "negative transcript frontier -> 422 schema_invalid" do
      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: "x",
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{},
          expected_transcript_revision: -1
        })

      assert resp.status == 422
      assert parsed(resp)["error"] == "schema_invalid"
    end

    test "invalid status → 422 schema_invalid" do
      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: "x",
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{},
          status: "bogus"
        })

      assert resp.status == 422
    end

    test "unknown session → 404 session_not_found" do
      resp =
        post_json("/v1/dialectic/resolve", %{
          session_id: "test_elixir_nope_#{System.unique_integer([:positive])}",
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{verdict: "resume"}
        })

      assert resp.status == 404
      assert parsed(resp)["error"] == "session_not_found"
    end
  end

  describe "/v1/dialectic/resolve-with-effect" do
    test "commits terminal row and the matching v1 effect in one request" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [
          session_id,
          Jason.encode!(%{
            "pause_effect_required" => true,
            "pause_token" => "pause-http-v1"
          })
        ]
      )

      resolution = %{
        action: "resume",
        conditions: ["monitor"],
        root_cause: "independent review",
        reasoning: "the parties agreed",
        signature_a: "",
        signature_b: "",
        timestamp: "2026-10-16T00:00:00+00:00",
        signature_version: 3
      }

      resp =
        post_json("/v1/dialectic/resolve-with-effect", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: resolution
        })

      assert resp.status == 200
      assert parsed(resp)["status"] == "resolved"

      assert %{rows: [["pending", "pause-http-v1", true]]} =
               Postgrex.query!(
                 UnitaresLeasePlane.DB,
                 """
                 SELECT state, pause_token, effect_required
                 FROM coordination.dialectic_resolution_effects
                 WHERE session_id = $1
                 """,
                 [session_id]
               )
    end

    test "malformed required binding rolls back both terminal row and effect" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [session_id, Jason.encode!(%{"pause_effect_required" => true})]
      )

      resolution = %{
        action: "resume",
        conditions: [],
        root_cause: "independent review",
        reasoning: "the parties agreed",
        signature_a: "",
        signature_b: "",
        timestamp: "2026-10-16T00:00:00+00:00",
        signature_version: 3
      }

      resp =
        post_json("/v1/dialectic/resolve-with-effect", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: resolution
        })

      assert resp.status == 503
      assert parsed(resp)["error"] == "service_unavailable"

      assert %{rows: [["active"]]} =
               Postgrex.query!(
                 UnitaresLeasePlane.DB,
                 "SELECT status FROM core.dialectic_sessions WHERE session_id = $1",
                 [session_id]
               )

      assert %{rows: [[false]]} =
               Postgrex.query!(
                 UnitaresLeasePlane.DB,
                 "SELECT EXISTS(SELECT 1 FROM coordination.dialectic_resolution_effects WHERE session_id = $1)",
                 [session_id]
               )
    end
  end

  describe "/v2/dialectic/resolve-with-effect" do
    test "requires a transcript frontier before invoking the saga" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      resp =
        post_json("/v2/dialectic/resolve-with-effect", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: %{verdict: "resume"}
        })

      assert resp.status == 422
      assert parsed(resp)["error"] == "schema_invalid"

      assert %{rows: [["active"]]} =
               Postgrex.query!(
                 UnitaresLeasePlane.DB,
                 "SELECT status FROM core.dialectic_sessions WHERE session_id = $1",
                 [session_id]
               )
    end

    test "commits only at the supplied transcript frontier" do
      session_id = insert_dialectic_session(paused_agent_id: "p", reviewer_agent_id: "r")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE core.dialectic_sessions SET paused_agent_state_json = ($2::text)::jsonb WHERE session_id = $1",
        [
          session_id,
          Jason.encode!(%{
            "pause_effect_required" => true,
            "pause_token" => "pause-http-v2"
          })
        ]
      )

      resolution = %{
        action: "resume",
        conditions: ["monitor"],
        root_cause: "independent review",
        reasoning: "the parties agreed",
        signature_a: "",
        signature_b: "",
        timestamp: "2026-10-16T00:00:00+00:00",
        signature_version: 3
      }

      resp =
        post_json("/v2/dialectic/resolve-with-effect", %{
          session_id: session_id,
          paused_agent_id: "p",
          reviewer_agent_id: "r",
          resolution: resolution,
          expected_phase: "synthesis",
          expected_transcript_revision: 0,
          expected_awaiting_facilitation: false
        })

      assert resp.status == 200
      assert parsed(resp)["status"] == "resolved"

      assert %{rows: [["pending", "pause-http-v2", true]]} =
               Postgrex.query!(
                 UnitaresLeasePlane.DB,
                 """
                 SELECT state, pause_token, effect_required
                 FROM coordination.dialectic_resolution_effects
                 WHERE session_id = $1
                 """,
                 [session_id]
               )
    end
  end

  describe "/v1/dialectic/reviewer" do
    test "claim_if_unassigned refuses a later first responder" do
      session_id = insert_dialectic_session(phase: "antithesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      first =
        post_json("/v1/dialectic/reviewer", %{
          session_id: session_id,
          reviewer_agent_id: "rev-first",
          claim_if_unassigned: true
        })

      assert first.status == 200

      late =
        post_json("/v1/dialectic/reviewer", %{
          session_id: session_id,
          reviewer_agent_id: "rev-late",
          claim_if_unassigned: true
        })

      assert late.status == 409
      assert parsed(late)["error"] == "reviewer_write_refused"
    end
  end

  describe "/v1/dialectic/reviewer/claim" do
    test "versioned open-slot route refuses a later first responder" do
      session_id = insert_dialectic_session(phase: "antithesis", status: "active")
      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      first =
        post_json("/v1/dialectic/reviewer/claim", %{
          session_id: session_id,
          reviewer_agent_id: "rev-first"
        })

      assert first.status == 200
      assert parsed(first)["claim_if_unassigned"] == true

      late =
        post_json("/v1/dialectic/reviewer/claim", %{
          session_id: session_id,
          reviewer_agent_id: "rev-late"
        })

      assert late.status == 409
      assert parsed(late)["error"] == "reviewer_write_refused"
    end
  end

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

    test "lowercase 'bearer' scheme accepted (RFC 7235 §2.1)", ctx do
      # RFC 7235 says auth scheme tokens are case-insensitive. Several
      # mainstream HTTP clients (e.g. Python httpx defaults) send lowercase
      # "bearer". Reject those and you break interoperability for no security
      # gain. The token value itself is still compared via secure_compare.
      body = acquire_body(ctx.surface)
      holder = body.holder_agent_uuid

      resp =
        :post
        |> conn("/v1/lease/acquire", Jason.encode!(body))
        |> put_req_header("content-type", "application/json")
        |> put_req_header("authorization", "bearer #{@bearer}")
        |> put_req_header("x-unitares-identity-proof", "proof:" <> holder)
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      assert parsed(resp)["ok"] == true
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

    test "a keyed client session id is stored as its audit digest", ctx do
      body =
        acquire_body(ctx.surface)
        |> Map.put(:audit_session, "agent-5e728ecb-123-xjc4uauir6jvdvqxylny")

      resp = post_json("/v1/lease/acquire", body)

      assert resp.status == 200
      assert parsed(resp)["lease"]["audit_session"] == "csid:96ac49cb17c64d38ad2468be"
    end

    test "agent:/ surface routes to the remote_heartbeat self-healing path (migration 042)",
         _ctx do
      agent_surface = "agent:/ag-" <> binary_part(random_uuid(), 0, 8)
      on_exit(fn -> cleanup_surface(agent_surface) end)

      # Body asks for local_beam (the acquire_body default); the plane routes by
      # SCHEME, so an agent:/ surface is coerced to the remote_heartbeat path —
      # a pure TTL row that self-heals, not a renewing local_beam holder.
      resp = post_json("/v1/lease/acquire", acquire_body(agent_surface))

      assert resp.status == 200
      lease = parsed(resp)["lease"]
      assert lease["surface_id"] == agent_surface
      assert lease["holder_kind"] == "remote_heartbeat"
      assert lease["heartbeat_required"] == true
    end

    test "maintenance:/ surface routes to the remote_heartbeat self-healing path",
         _ctx do
      maintenance_surface = "maintenance:/http-test-" <> binary_part(random_uuid(), 0, 8)
      on_exit(fn -> cleanup_surface(maintenance_surface) end)

      # Body asks for local_beam (the acquire_body default); maintenance jobs
      # are short-lived cleanup/repair surfaces, so the plane coerces them to a
      # TTL row that self-heals if the caller dies before release.
      resp = post_json("/v1/lease/acquire", acquire_body(maintenance_surface))

      assert resp.status == 200
      lease = parsed(resp)["lease"]
      assert lease["surface_id"] == maintenance_surface
      assert lease["surface_kind"] == "maintenance"
      assert lease["holder_kind"] == "remote_heartbeat"
      assert lease["heartbeat_required"] == true
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

    test "holder_class='role' → 200 permission_denied (RFC §4.4)", ctx do
      # RFC §4.4 line 481 + §7.3.5: roles cannot hold leases — application-
      # layer policy rejection, not schema_invalid. Per §7.3.5 the typed-
      # absence shape is 200 + ok:false. The previous behavior (422
      # schema_invalid) was deliberate implementation drift that the §9 gate
      # `test http_router returns 200 on permission_denied` now closes.
      body = acquire_body(ctx.surface, holder_class: "role")
      resp = post_json("/v1/lease/acquire", body)
      assert resp.status == 200
      payload = parsed(resp)
      assert payload["ok"] == false
      assert payload["error"] == "permission_denied"
      assert payload["reason"] == "role_holders_unsupported"
    end

    test "holder_class='unknown_class' → 422 schema_invalid", ctx do
      # Unknown holder_class values that aren't the policy-rejected "role"
      # remain schema_invalid — they're structurally malformed input, not a
      # recognized but unsupported policy case.
      body = acquire_body(ctx.surface, holder_class: "unknown_class")
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

    # --- §9 RFC named-gate tests (mirroring docs/proposals/active/surface-lease-plane-v0.md §9) ---
    #
    # These pin the gates by their §9-named description so the audit
    # (scripts/dev/audit_rfc_section_9_gates.py) reports them as exact rather
    # than missing. Coverage overlaps with the in-house tests above; keeping
    # both makes the §9 audit trustworthy without losing the more descriptive
    # names that aid local debugging.

    test "http_router returns 200 on permission_denied", ctx do
      # RFC §7.3.5 / §9 gate: 200 + ok:false on application-layer
      # permission_denied. The role-holder rejection path (RFC §4.4) is
      # the canonical trigger; any future application-layer permission_denied
      # must follow the same envelope shape.
      body = acquire_body(ctx.surface, holder_class: "role")
      resp = post_json("/v1/lease/acquire", body)
      assert resp.status == 200
      payload = parsed(resp)
      assert payload["ok"] == false
      assert payload["error"] == "permission_denied"
      assert is_binary(payload["reason"])
      assert payload["reason"] != ""
    end

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

    # --- self-heal routing (acquire_for_surface) ---
    # file:// edit-leases, agent:/ presence rows, and maintenance:/ cleanup jobs
    # must NOT spawn an auto-renewing local_beam holder. They route to the
    # remote_heartbeat path — a pure DB row reaped at TTL — so a dead caller
    # self-heals. resident:/ and other coordination surfaces MUST stay
    # local_beam (long-lived auto-renew presence), even when the caller sends
    # holder_kind="remote_heartbeat" (as the resident advisory path does).

    @tag :tmp_dir
    test "file:// surface routes to remote_heartbeat (self-healing, no holder)", ctx do
      path = Path.join(ctx.tmp_dir, "edit-target.txt")
      File.write!(path, "x")

      resp =
        post_json(
          "/v1/lease/acquire",
          acquire_body("file://" <> path, holder_kind: "remote_heartbeat")
        )

      assert resp.status == 200
      lease = parsed(resp)["lease"]
      on_exit(fn -> cleanup_surface(lease["surface_id"]) end)
      assert lease["surface_kind"] == "file"
      assert lease["holder_kind"] == "remote_heartbeat"
    end

    test "resident:/ surface stays local_beam even when remote_heartbeat requested", _ctx do
      surface = "resident:/lease-routing-test-#{random_uuid()}"

      resp =
        post_json(
          "/v1/lease/acquire",
          acquire_body(surface, holder_kind: "remote_heartbeat")
        )

      assert resp.status == 200
      lease = parsed(resp)["lease"]
      on_exit(fn -> cleanup_surface(lease["surface_id"]) end)
      # Routing is scoped away from resident:/; residents keep auto-renew presence.
      assert lease["holder_kind"] == "local_beam"
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
      assert Map.delete(parsed(renew), "protocol_version") == %{"ok" => true}

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
      assert Map.delete(parsed(resp), "protocol_version") == %{"ok" => true}

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

    # 2026-08-01 double-lost-response incident: a client that recognizes an
    # active lease as its own stranded acquire (held_by_uuid matches a holder
    # uuid it minted) releases it with this reason so span-based analyses of
    # release_reason='normal' don't absorb orphan spans as legitimate holds.
    test "release_reason='reclaimed_lost_acquire' is accepted", ctx do
      acquire = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      lease_id = parsed(acquire)["lease"]["lease_id"]

      resp =
        post_json("/v1/lease/release", %{
          lease_id: lease_id,
          release_reason: "reclaimed_lost_acquire"
        })

      assert resp.status == 200
      assert parsed(resp)["ok"] == true
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
      assert Map.delete(parsed(accept), "protocol_version") == %{"ok" => true}

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

  describe "enforced governance identity binding" do
    setup do
      Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

      Application.put_env(:lease_plane, :identity_verifier, fn holder, proof ->
        if proof == "proof:" <> holder, do: :ok, else: {:error, :invalid}
      end)

      :ok
    end

    test "acquire binds the claimed holder and later mutations require that holder's proof",
         ctx do
      holder = random_uuid()
      other = random_uuid()
      body = acquire_body(ctx.surface, holder_agent_uuid: holder)

      missing = post_json_without_proof("/v1/lease/acquire", body)
      assert missing.status == 403
      assert parsed(missing)["reason"] == "identity_proof_invalid"

      mismatched = post_json_with_proof("/v1/lease/acquire", body, "proof:" <> other)
      assert mismatched.status == 403
      assert parsed(mismatched)["reason"] == "identity_proof_invalid"

      acquired = post_json_with_proof("/v1/lease/acquire", body, "proof:" <> holder)
      assert acquired.status == 200
      lease_id = parsed(acquired)["lease"]["lease_id"]

      renew_without_proof = post_json_without_proof("/v1/lease/renew", %{lease_id: lease_id})
      assert renew_without_proof.status == 403

      renew_with_proof =
        post_json_with_proof("/v1/lease/renew", %{lease_id: lease_id}, "proof:" <> holder)

      assert renew_with_proof.status == 200

      release_as_other =
        post_json_with_proof(
          "/v1/lease/release",
          %{lease_id: lease_id, release_reason: "normal"},
          "proof:" <> other
        )

      assert release_as_other.status == 403

      released =
        post_json_with_proof(
          "/v1/lease/release",
          %{lease_id: lease_id, release_reason: "normal"},
          "proof:" <> holder
        )

      assert released.status == 200
    end

    test "handoff offer requires the current holder and accept requires the recipient", ctx do
      holder = random_uuid()
      recipient = random_uuid()

      acquired =
        post_json_with_proof(
          "/v1/lease/acquire",
          acquire_body(ctx.surface, holder_agent_uuid: holder),
          "proof:" <> holder
        )

      lease_id = parsed(acquired)["lease"]["lease_id"]
      offer_body = %{lease_id: lease_id, to_holder_agent_uuid: recipient, ttl_s: 30}

      refused_offer =
        post_json_with_proof("/v1/lease/handoff/offer", offer_body, "proof:" <> recipient)

      assert refused_offer.status == 403

      offer = post_json_with_proof("/v1/lease/handoff/offer", offer_body, "proof:" <> holder)
      assert offer.status == 200
      handoff_id = parsed(offer)["handoff_id"]

      refused_accept =
        post_json_with_proof(
          "/v1/lease/handoff/accept",
          %{handoff_id: handoff_id},
          "proof:" <> holder
        )

      assert refused_accept.status == 403

      accepted =
        post_json_with_proof(
          "/v1/lease/handoff/accept",
          %{handoff_id: handoff_id},
          "proof:" <> recipient
        )

      assert accepted.status == 200

      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      receiving_lease = parsed(status)["lease"]
      assert receiving_lease["holder_agent_uuid"] == recipient

      assert post_json_with_proof(
               "/v1/lease/release",
               %{lease_id: receiving_lease["lease_id"], release_reason: "normal"},
               "proof:" <> recipient
             ).status == 200
    end

    test "governance verifier outage fails closed with typed service absence", ctx do
      Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> {:error, :timeout} end)

      response =
        post_json_with_proof(
          "/v1/lease/acquire",
          acquire_body(ctx.surface),
          "proof:anything"
        )

      assert response.status == 503
      assert parsed(response)["reason"] == "identity_verification_unavailable"
    end

    test "verifier receives method, path, and exact wire-body digest", ctx do
      test_pid = self()

      Application.put_env(:lease_plane, :identity_verifier, fn _holder, _proof, context ->
        send(test_pid, {:identity_request_context, context})
        :ok
      end)

      body = acquire_body(ctx.surface)
      response = post_json_with_proof("/v1/lease/acquire", body, "proof:anything")
      assert response.status == 200

      expected_sha =
        body
        |> Jason.encode!()
        |> then(&:crypto.hash(:sha256, &1))
        |> Base.encode16(case: :lower)

      assert_receive {:identity_request_context,
                      %{
                        method: "POST",
                        path: "/v1/lease/acquire",
                        body_sha256: ^expected_sha
                      }}
    end

    test "replayed signed proof is a typed 403 refusal", ctx do
      Application.put_env(:lease_plane, :identity_verifier, fn _, _, _ ->
        {:error, :replayed}
      end)

      response =
        post_json_with_proof(
          "/v1/lease/acquire",
          acquire_body(ctx.surface),
          "lat.v1.payload.signature"
        )

      assert response.status == 403
      assert parsed(response)["reason"] == "identity_proof_replayed"
    end
  end

  describe "principal-bound lease and dialectic mutations" do
    setup do
      Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
      :ok
    end

    test "explicit compatibility mode permits a missing lease proof", ctx do
      Application.put_env(:lease_plane, :identity_binding_mode, :off)

      response = post_json_without_proof("/v1/lease/acquire", acquire_body(ctx.surface))

      assert response.status == 200
      assert parsed(response)["ok"] == true
    end

    test "explicit compatibility mode permits a non-participant dialectic actor" do
      Application.put_env(:lease_plane, :identity_binding_mode, :off)

      session_id =
        insert_dialectic_session(
          paused_agent_id: "paused",
          reviewer_agent_id: "reviewer",
          phase: "thesis",
          status: "active"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      response =
        post_json_without_proof("/v1/dialectic/phase", %{
          session_id: session_id,
          phase: "antithesis",
          actor_agent_id: "background-reconciler"
        })

      assert response.status == 200
    end

    test "dialectic session creation binds proof to the paused participant" do
      session_id = "test_elixir_identity_create_#{System.unique_integer([:positive])}"
      on_exit(fn -> cleanup_dialectic_session(session_id) end)
      body = %{session_id: session_id, paused_agent_id: "paused", reviewer_agent_id: "reviewer"}

      assert post_json_without_proof("/v1/dialectic/session", body).status == 403

      mismatched =
        post_json_with_proof("/v1/dialectic/session", body, "proof:reviewer")

      assert mismatched.status == 403

      authorized =
        post_json_with_proof("/v1/dialectic/session", body, "proof:paused")

      assert authorized.status == 201
    end

    test "dialectic phase loads participants before accepting the actor" do
      session_id =
        insert_dialectic_session(
          paused_agent_id: "paused",
          reviewer_agent_id: "reviewer",
          phase: "thesis",
          status: "active"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)
      body = %{session_id: session_id, phase: "antithesis", actor_agent_id: "paused"}

      assert post_json_without_proof("/v1/dialectic/phase", body).status == 403
      assert post_json_with_proof("/v1/dialectic/phase", body, "proof:reviewer").status == 403
      assert post_json_with_proof("/v1/dialectic/phase", body, "proof:paused").status == 200
    end

    test "reviewer claims and terminal resolutions require a named participant proof" do
      session_id =
        insert_dialectic_session(
          paused_agent_id: "paused",
          reviewer_agent_id: nil,
          phase: "synthesis",
          status: "active"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      claim = %{
        session_id: session_id,
        reviewer_agent_id: "reviewer",
        actor_agent_id: "reviewer"
      }

      assert post_json_without_proof("/v1/dialectic/reviewer/claim", claim).status == 403

      assert post_json_with_proof(
               "/v1/dialectic/reviewer/claim",
               claim,
               "proof:paused"
             ).status == 403

      assert post_json_with_proof(
               "/v1/dialectic/reviewer/claim",
               claim,
               "proof:reviewer"
             ).status == 200

      resolve = %{
        session_id: session_id,
        paused_agent_id: "paused",
        reviewer_agent_id: "reviewer",
        actor_agent_id: "paused",
        resolution: %{"reason" => "test"}
      }

      assert post_json_without_proof("/v1/dialectic/resolve", resolve).status == 403

      assert post_json_with_proof(
               "/v1/dialectic/resolve",
               resolve,
               "proof:paused"
             ).status == 200
    end

    test "only the current reviewer may reassign a filled reviewer slot" do
      session_id =
        insert_dialectic_session(
          paused_agent_id: "paused",
          reviewer_agent_id: "reviewer",
          phase: "antithesis",
          status: "active"
        )

      on_exit(fn -> cleanup_dialectic_session(session_id) end)

      paused_change = %{
        session_id: session_id,
        reviewer_agent_id: "paused-choice",
        actor_agent_id: "paused"
      }

      assert post_json_with_proof(
               "/v1/dialectic/reviewer",
               paused_change,
               "proof:paused"
             ).status == 403

      reviewer_change = %{
        session_id: session_id,
        reviewer_agent_id: "reviewer-choice",
        actor_agent_id: "reviewer"
      }

      assert post_json_with_proof(
               "/v1/dialectic/reviewer",
               reviewer_change,
               "proof:reviewer"
             ).status == 200
    end
  end

  describe "/v1/health identity observability" do
    test "reports bounded identity verifier counters additively", _ctx do
      UnitaresLeasePlane.IdentityMetrics.record(:attestation, :verified, 125)

      response =
        :get
        |> conn("/v1/health")
        |> authed()
        |> HTTPRouter.call(@opts)

      identity = parsed(response)["identity_binding"]
      assert response.status == 200
      assert identity["metrics"]["total"] >= 1
      assert is_map(identity["metrics"]["outcomes"])
      assert identity["metrics"]["outcomes"]["verified"] >= 1
      assert identity["metrics"]["proof_types"]["attestation"] >= 1
      assert identity["metrics"]["latency"]["max_us"] >= 125
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
      parent = self()

      log =
        capture_log(fn ->
          result =
            HTTPRouter.handle_errors(conn, %{
              kind: :error,
              reason: %RuntimeError{message: "postgresql password=hunter2 leaked"},
              stack: []
            })

          send(parent, {:handle_errors_result, result})
        end)

      assert_receive {:handle_errors_result, result}

      assert result.status == 503
      body = Jason.decode!(result.resp_body)

      assert Map.delete(body, "protocol_version") == %{
               "ok" => false,
               "error" => "service_unavailable",
               "reason" => "internal error"
             }

      assert body["protocol_version"] == "v1.0"

      # Verify the leaky inspect string never made it to the wire or logs.
      refute result.resp_body =~ "hunter2"
      refute log =~ "hunter2"
      refute log =~ "password=hunter2"
    end
  end

  # ---------- /v1/lease/force-release (RFC §7.10) ----------
  #
  # Contract-layer enforcement of the elevated-bearer requirement. The regular
  # LEASE_PLANE_BEARER_TOKEN cannot authorize force-release; only
  # LEASE_FORCE_RELEASE_TOKEN succeeds. Mutual exclusion is enforced in
  # HTTPAuth's per-path token mapping.

  describe "POST /v1/lease/force-release — auth (RFC §7.10)" do
    test "regular bearer → 401 permission_denied", _ctx do
      resp =
        :post
        |> conn("/v1/lease/force-release", Jason.encode!(%{lease_id: random_uuid()}))
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      payload = parsed(resp)
      assert payload["error"] == "permission_denied"
      assert payload["reason"] =~ "force-release"
    end

    test "missing Authorization header → 401 permission_denied", _ctx do
      resp =
        :post
        |> conn("/v1/lease/force-release", Jason.encode!(%{lease_id: random_uuid()}))
        |> put_req_header("content-type", "application/json")
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      assert parsed(resp)["error"] == "permission_denied"
    end

    test "force-release token → 200 happy path", ctx do
      # Acquire a lease via the regular bearer, then force-release it with the
      # elevated bearer. The acquire side proves the regular bearer's normal
      # scope is unaffected; the force-release side proves the elevated path
      # works end-to-end.
      acq = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      assert acq.status == 200
      lease_id = parsed(acq)["lease"]["lease_id"]

      resp =
        :post
        |> conn("/v1/lease/force-release", Jason.encode!(%{lease_id: lease_id}))
        |> put_req_header("content-type", "application/json")
        |> authed_force_release()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      assert parsed(resp)["ok"] == true
    end

    test "force-release token cannot authorize /v1/lease/acquire (mutual exclusion)", ctx do
      # The force-release bearer is operator-only; presenting it on a
      # non-force-release route must fail. Otherwise the elevated token's
      # exposure surface widens for no contract-layer reason.
      resp =
        :post
        |> conn("/v1/lease/acquire", Jason.encode!(acquire_body(ctx.surface)))
        |> put_req_header("content-type", "application/json")
        |> authed_force_release()
        |> HTTPRouter.call(@opts)

      assert resp.status == 401
      assert parsed(resp)["error"] == "permission_denied"
    end

    test "force-release token unconfigured → 503 fail-closed", _ctx do
      Application.put_env(:lease_plane, :force_release_token, nil)

      on_exit(fn ->
        Application.put_env(:lease_plane, :force_release_token, @force_release_token)
      end)

      resp =
        :post
        |> conn("/v1/lease/force-release", Jason.encode!(%{lease_id: random_uuid()}))
        |> put_req_header("content-type", "application/json")
        |> authed_force_release()
        |> HTTPRouter.call(@opts)

      assert resp.status == 503
      assert parsed(resp)["error"] == "service_unavailable"
    end

    test "missing lease_id → 422 schema_invalid", _ctx do
      resp =
        :post
        |> conn("/v1/lease/force-release", Jason.encode!(%{}))
        |> put_req_header("content-type", "application/json")
        |> authed_force_release()
        |> HTTPRouter.call(@opts)

      assert resp.status == 422
      assert parsed(resp)["error"] == "schema_invalid"
    end

    test "nonexistent lease → 404 not_found", _ctx do
      resp =
        :post
        |> conn("/v1/lease/force-release", Jason.encode!(%{lease_id: random_uuid()}))
        |> put_req_header("content-type", "application/json")
        |> authed_force_release()
        |> HTTPRouter.call(@opts)

      assert resp.status == 404
      assert parsed(resp)["error"] == "not_found"
    end
  end

  describe "POST /v1/lease/release — release_reason='forced' rejected (RFC §7.10)" do
    test "release_reason='forced' on /release → 200 permission_denied", ctx do
      # release_reason='forced' must arrive at /v1/lease/force-release so the
      # elevated bearer gates it at the contract layer. Accepting it on
      # /release would route around the §7.10 authority check.
      acq = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      lease_id = parsed(acq)["lease"]["lease_id"]

      resp = post_json("/v1/lease/release", %{lease_id: lease_id, release_reason: "forced"})

      assert resp.status == 200
      payload = parsed(resp)
      assert payload["ok"] == false
      assert payload["error"] == "permission_denied"
      assert payload["reason"] == "forced_release_requires_force_release_endpoint"
    end

    test "regular release reasons still succeed on /release", ctx do
      acq = post_json("/v1/lease/acquire", acquire_body(ctx.surface))
      lease_id = parsed(acq)["lease"]["lease_id"]

      resp = post_json("/v1/lease/release", %{lease_id: lease_id, release_reason: "normal"})

      assert resp.status == 200
      assert parsed(resp)["ok"] == true
    end
  end

  describe "persisted lease principals and multi-issuer attestation (#1983)" do
    setup do
      {pub_a, priv_a} = :crypto.generate_key(:eddsa, :ed25519)
      {pub_b, priv_b} = :crypto.generate_key(:eddsa, :ed25519)
      now = System.system_time(:second)

      previous = %{
        mode: Application.get_env(:lease_plane, :identity_binding_mode),
        format: Application.get_env(:lease_plane, :identity_proof_format),
        verifier: Application.get_env(:lease_plane, :identity_verifier),
        trusted: Application.get_env(:lease_plane, :trusted_identity_issuers),
        fetcher: Application.get_env(:lease_plane, :operator_key_fetcher),
        consumer: Application.get_env(:lease_plane, :identity_nonce_consumer),
        audience: Application.get_env(:lease_plane, :identity_attestation_audience),
        clock: Application.get_env(:lease_plane, :identity_clock),
        kinds: Application.get_env(:lease_plane, :identity_bound_surface_kinds)
      }

      Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
      Application.put_env(:lease_plane, :identity_proof_format, :hybrid)
      Application.delete_env(:lease_plane, :identity_verifier)
      Application.put_env(:lease_plane, :identity_bound_surface_kinds, :all)
      Application.put_env(:lease_plane, :identity_attestation_audience, "lease-plane-test")
      Application.put_env(:lease_plane, :identity_clock, fn -> now end)
      Application.put_env(:lease_plane, :identity_nonce_consumer, fn _, _, _ -> :ok end)

      Application.put_env(:lease_plane, :trusted_identity_issuers, %{
        "operator-a" => "https://operator-a.example/keys",
        "operator-b" => "https://operator-b.example/keys"
      })

      Application.put_env(:lease_plane, :operator_key_fetcher, fn
        "operator-a", "kid-a", _ -> {:ok, pub_a}
        "operator-b", "kid-b", _ -> {:ok, pub_b}
        _, _, _ -> {:error, :invalid}
      end)

      UnitaresLeasePlane.OperatorKeyCache.evict_issuer("operator-a")
      UnitaresLeasePlane.OperatorKeyCache.evict_issuer("operator-b")

      on_exit(fn ->
        restore_app_env(:identity_binding_mode, previous.mode)
        restore_app_env(:identity_proof_format, previous.format)
        restore_app_env(:identity_verifier, previous.verifier)
        restore_app_env(:trusted_identity_issuers, previous.trusted)
        restore_app_env(:operator_key_fetcher, previous.fetcher)
        restore_app_env(:identity_nonce_consumer, previous.consumer)
        restore_app_env(:identity_attestation_audience, previous.audience)
        restore_app_env(:identity_clock, previous.clock)
        restore_app_env(:identity_bound_surface_kinds, previous.kinds)
      end)

      {:ok, priv_a: priv_a, priv_b: priv_b, now: now}
    end

    defp restore_app_env(key, nil), do: Application.delete_env(:lease_plane, key)
    defp restore_app_env(key, value), do: Application.put_env(:lease_plane, key, value)

    defp post_attested(path, body, issuer, kid, priv_key, subject, now) do
      raw_body = Jason.encode!(body)
      body_sha256 = :crypto.hash(:sha256, raw_body) |> Base.encode16(case: :lower)

      claims = %{
        "v" => 1,
        "typ" => "lease-attestation",
        "alg" => "EdDSA",
        "iss" => issuer,
        "kid" => kid,
        "aud" => "lease-plane-test",
        "sub" => subject,
        "mth" => "POST",
        "pth" => path,
        "bsha" => body_sha256,
        "iat" => now,
        "nbf" => now,
        "exp" => now + 30,
        "jti" => Base.url_encode64(:crypto.strong_rand_bytes(16), padding: false)
      }

      payload_b64 = claims |> Jason.encode!() |> Base.url_encode64(padding: false)
      signing_input = "lat.v1." <> payload_b64
      sig = :crypto.sign(:eddsa, :none, signing_input, [priv_key, :ed25519])
      token = signing_input <> "." <> Base.url_encode64(sig, padding: false)

      :post
      |> conn(path, raw_body)
      |> put_req_header("content-type", "application/json")
      |> put_req_header("x-unitares-identity-proof", token)
      |> authed()
      |> HTTPRouter.call(@opts)
    end

    test "persists (holder_issuer, holder_subject) with distinct holder_subject, rejects cross-issuer and legacy mutations, and conflicts on same UUID with different issuer",
         ctx do
      attempt_nonce = random_uuid()
      durable_subject = "sentinel-resident-principal"

      acq_body =
        acquire_body(ctx.surface, holder_agent_uuid: attempt_nonce)
        |> Map.put(:holder_subject, durable_subject)

      # Acquire with operator-a attestation bound to durable_subject
      acq =
        post_attested(
          "/v1/lease/acquire",
          acq_body,
          "operator-a",
          "kid-a",
          ctx.priv_a,
          durable_subject,
          ctx.now
        )

      assert acq.status == 200
      lease = parsed(acq)["lease"]
      assert lease["holder_agent_uuid"] == attempt_nonce
      assert lease["holder_issuer"] == "operator-a"
      assert lease["holder_subject"] == durable_subject
      lease_id = lease["lease_id"]

      # Status returns persisted principal
      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert status.status == 200
      assert parsed(status)["lease"]["holder_issuer"] == "operator-a"
      assert parsed(status)["lease"]["holder_subject"] == durable_subject

      # Same holder_agent_uuid + same (issuer, subject) -> idempotent
      idem =
        post_attested(
          "/v1/lease/acquire",
          acq_body,
          "operator-a",
          "kid-a",
          ctx.priv_a,
          durable_subject,
          ctx.now
        )

      assert idem.status == 200
      assert parsed(idem)["idempotent"] == true

      # Same holder_agent_uuid + same subject from DIFFERENT issuer -> 409 held_by_other
      conflict =
        post_attested(
          "/v1/lease/acquire",
          acq_body,
          "operator-b",
          "kid-b",
          ctx.priv_b,
          durable_subject,
          ctx.now
        )

      assert conflict.status == 409
      assert parsed(conflict)["error"] == "held_by_other"

      # Conflict event payload records both requested and held principals
      %{rows: [[conflict_payload]]} =
        Postgrex.query!(
          UnitaresLeasePlane.DB,
          "SELECT payload FROM lease_plane.lease_plane_events " <>
            "WHERE surface_id = $1 AND event_type = 'conflict_held_by_other' " <>
            "ORDER BY ts DESC LIMIT 1",
          [ctx.surface]
        )

      assert conflict_payload["requested_holder_issuer"] == "operator-b"
      assert conflict_payload["requested_holder_subject"] == durable_subject
      assert conflict_payload["held_by_issuer"] == "operator-a"
      assert conflict_payload["held_by_subject"] == durable_subject

      # Renew refused for different issuer (operator-b) with same subject
      cross_issuer_renew =
        post_attested(
          "/v1/lease/renew",
          %{lease_id: lease_id},
          "operator-b",
          "kid-b",
          ctx.priv_b,
          durable_subject,
          ctx.now
        )

      assert cross_issuer_renew.status == 403
      assert parsed(cross_issuer_renew)["reason"] == "identity_proof_invalid"

      # Renew refused for legacy proof even in :hybrid mode
      legacy_renew =
        post_json_with_proof("/v1/lease/renew", %{lease_id: lease_id}, "v1.legacy.token")

      assert legacy_renew.status == 403
      assert parsed(legacy_renew)["reason"] == "identity_proof_invalid"

      # Renew refused if attested subject is attempt_nonce instead of holder_subject
      wrong_subject_renew =
        post_attested(
          "/v1/lease/renew",
          %{lease_id: lease_id},
          "operator-a",
          "kid-a",
          ctx.priv_a,
          attempt_nonce,
          ctx.now
        )

      assert wrong_subject_renew.status == 403

      # Renew succeeds with matching (operator-a, durable_subject)
      good_renew =
        post_attested(
          "/v1/lease/renew",
          %{lease_id: lease_id},
          "operator-a",
          "kid-a",
          ctx.priv_a,
          durable_subject,
          ctx.now
        )

      assert good_renew.status == 200

      # Release succeeds with matching (operator-a, durable_subject)
      good_release =
        post_attested(
          "/v1/lease/release",
          %{lease_id: lease_id, release_reason: "normal"},
          "operator-a",
          "kid-a",
          ctx.priv_a,
          durable_subject,
          ctx.now
        )

      assert good_release.status == 200
    end

    test "cross-issuer handoff pins to_holder_issuer and to_holder_subject and persists verified recipient principal",
         ctx do
      holder_a = random_uuid()
      holder_b = random_uuid()
      subject_a = "principal-a"
      subject_b = "principal-b"

      acq =
        post_attested(
          "/v1/lease/acquire",
          acquire_body(ctx.surface, holder_agent_uuid: holder_a)
          |> Map.put(:holder_subject, subject_a),
          "operator-a",
          "kid-a",
          ctx.priv_a,
          subject_a,
          ctx.now
        )

      assert acq.status == 200
      lease_id = parsed(acq)["lease"]["lease_id"]

      offer_body = %{
        lease_id: lease_id,
        to_holder_agent_uuid: holder_b,
        to_holder_issuer: "operator-b",
        to_holder_subject: subject_b,
        ttl_s: 45
      }

      # Offer requires current holder (operator-a, subject_a)
      bad_offer =
        post_attested(
          "/v1/lease/handoff/offer",
          offer_body,
          "operator-b",
          "kid-b",
          ctx.priv_b,
          subject_a,
          ctx.now
        )

      assert bad_offer.status == 403

      offer =
        post_attested(
          "/v1/lease/handoff/offer",
          offer_body,
          "operator-a",
          "kid-a",
          ctx.priv_a,
          subject_a,
          ctx.now
        )

      assert offer.status == 200
      handoff_id = parsed(offer)["handoff_id"]

      # Accept refuses wrong issuer (operator-a) even with subject_b
      wrong_issuer_accept =
        post_attested(
          "/v1/lease/handoff/accept",
          %{handoff_id: handoff_id},
          "operator-a",
          "kid-a",
          ctx.priv_a,
          subject_b,
          ctx.now
        )

      assert wrong_issuer_accept.status == 403

      # Accept succeeds with recipient's (operator-b, subject_b)
      accept =
        post_attested(
          "/v1/lease/handoff/accept",
          %{handoff_id: handoff_id},
          "operator-b",
          "kid-b",
          ctx.priv_b,
          subject_b,
          ctx.now
        )

      assert accept.status == 200

      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      new_lease = parsed(status)["lease"]
      assert new_lease["holder_agent_uuid"] == holder_b
      assert new_lease["holder_issuer"] == "operator-b"
      assert new_lease["holder_subject"] == subject_b

      # Release of handed-off lease requires (operator-b, subject_b)
      assert post_attested(
               "/v1/lease/release",
               %{lease_id: new_lease["lease_id"], release_reason: "normal"},
               "operator-b",
               "kid-b",
               ctx.priv_b,
               subject_b,
               ctx.now
             ).status == 200
    end

    test "distinct holder_subject is refused when no attested principal is verified (hybrid legacy, log, off)",
         ctx do
      holder_a = random_uuid()
      holder_b = random_uuid()

      distinct_body =
        acquire_body(ctx.surface, holder_agent_uuid: holder_b)
        |> Map.put(:holder_subject, holder_a)

      # :enforce + :hybrid with a legacy proof -> 403 identity_proof_invalid
      Application.put_env(:lease_plane, :identity_binding_mode, :enforce)
      Application.put_env(:lease_plane, :identity_proof_format, :hybrid)

      legacy_resp =
        post_json_with_proof("/v1/lease/acquire", distinct_body, "v1.legacy-for-a.sig")

      assert legacy_resp.status == 403
      assert parsed(legacy_resp)["reason"] == "identity_proof_invalid"

      # :enforce + mock verifier returning :ok (no issuer/subject) for holder_a -> 403
      Application.put_env(:lease_plane, :identity_verifier, fn
        ^holder_a, "proof:" <> ^holder_a -> :ok
        _, _ -> {:error, :invalid}
      end)

      mock_legacy_resp =
        post_json_with_proof("/v1/lease/acquire", distinct_body, "proof:" <> holder_a)

      assert mock_legacy_resp.status == 403
      assert parsed(mock_legacy_resp)["reason"] == "identity_proof_invalid"
      Application.delete_env(:lease_plane, :identity_verifier)

      # :log with missing proof + distinct holder_subject -> 403 (never stores holder_agent_uuid=B with subject=nil)
      Application.put_env(:lease_plane, :identity_binding_mode, :log)
      log_distinct = post_json_without_proof("/v1/lease/acquire", distinct_body)
      assert log_distinct.status == 403
      assert parsed(log_distinct)["reason"] == "identity_proof_invalid"

      # :off with distinct holder_subject -> 403
      Application.put_env(:lease_plane, :identity_binding_mode, :off)
      off_distinct = post_json_without_proof("/v1/lease/acquire", distinct_body)
      assert off_distinct.status == 403
      assert parsed(off_distinct)["reason"] == "identity_proof_invalid"

      # :log with matching holder_subject == holder_agent_uuid and missing proof -> 200, unverified principal NOT persisted
      Application.put_env(:lease_plane, :identity_binding_mode, :log)

      same_body =
        acquire_body(ctx.surface, holder_agent_uuid: holder_a)
        |> Map.put(:holder_subject, holder_a)

      same_resp = post_json_without_proof("/v1/lease/acquire", same_body)
      assert same_resp.status == 200
      lease = parsed(same_resp)["lease"]
      assert lease["holder_issuer"] == nil
      assert lease["holder_subject"] == nil
    end

    test "compatibility-mode (:log and :off) idempotent reacquire preserves recorded principal while wrong issuer still conflicts",
         ctx do
      rand = :crypto.strong_rand_bytes(6) |> Base.encode16(case: :lower)
      resident_surface = "resident:/test_elixir_compat_#{rand}"
      on_exit(fn -> cleanup_surface(resident_surface) end)

      holder = random_uuid()
      base_body = acquire_body(resident_surface, holder_agent_uuid: holder)

      # Initial attested acquire in :log mode records ("operator-a", holder)
      Application.put_env(:lease_plane, :identity_binding_mode, :log)

      acq =
        post_attested(
          "/v1/lease/acquire",
          base_body,
          "operator-a",
          "kid-a",
          ctx.priv_a,
          holder,
          ctx.now
        )

      assert acq.status == 200
      assert parsed(acq)["lease"]["holder_issuer"] == "operator-a"
      assert parsed(acq)["lease"]["holder_subject"] == holder

      # Unauthenticated idempotent retry in :log mode with substrate_state update succeeds and preserves principal
      observed_at = DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601()

      retry_body =
        base_body
        |> Map.put(:substrate_state, %{"sensor" => %{"status" => "healthy"}})
        |> Map.put(:substrate_state_observed_at, observed_at)

      log_retry = post_json_without_proof("/v1/lease/acquire", retry_body)
      assert log_retry.status == 200
      log_retry_body = parsed(log_retry)
      assert log_retry_body["idempotent"] == true
      assert log_retry_body["lease"]["holder_issuer"] == "operator-a"
      assert log_retry_body["lease"]["holder_subject"] == holder
      assert log_retry_body["lease"]["substrate_state"] == %{"sensor" => %{"status" => "healthy"}}

      # Wrong-issuer attestation in :log mode STILL conflicts (409 held_by_other)
      wrong_issuer_retry =
        post_attested(
          "/v1/lease/acquire",
          base_body,
          "operator-b",
          "kid-b",
          ctx.priv_b,
          holder,
          ctx.now
        )

      assert wrong_issuer_retry.status == 409
      assert parsed(wrong_issuer_retry)["error"] == "held_by_other"

      # :off mode idempotent retry on attested lease succeeds and preserves principal
      Application.put_env(:lease_plane, :identity_binding_mode, :off)
      off_retry = post_json_without_proof("/v1/lease/acquire", base_body)
      assert off_retry.status == 200
      assert parsed(off_retry)["idempotent"] == true
      assert parsed(off_retry)["lease"]["holder_issuer"] == "operator-a"
      assert parsed(off_retry)["lease"]["holder_subject"] == holder

      # Direct Repo.acquire/1 without allow_unverified_principal_fallback remains fail-closed
      assert {:error, :held_by_other, _info} =
               UnitaresLeasePlane.Repo.acquire(%{
                 surface_id: resident_surface,
                 holder_agent_uuid: holder,
                 holder_class: "process_instance",
                 holder_kind: "local_beam",
                 ttl_s: 30
               })
    end

    test "pre-084 issuerless rows and unpinned handoffs fail closed under multi-issuer config while single-issuer stays compatible",
         ctx do
      holder = random_uuid()
      recipient = random_uuid()
      recipient_subject = "recipient-durable-principal"

      # Seed an active pre-084 (holder_issuer=nil, holder_subject=nil) lease directly via Repo
      assert {:ok, legacy_lease, :new} =
               UnitaresLeasePlane.Repo.acquire(%{
                 surface_id: ctx.surface,
                 holder_agent_uuid: holder,
                 holder_class: "process_instance",
                 holder_kind: "remote_heartbeat",
                 ttl_s: 60
               })

      assert legacy_lease.holder_issuer == nil
      assert legacy_lease.holder_subject == nil

      # With 2 trusted issuers configured, mutating an issuerless existing row fails closed (503)
      multi_renew_b =
        post_attested(
          "/v1/lease/renew",
          %{lease_id: legacy_lease.lease_id},
          "operator-b",
          "kid-b",
          ctx.priv_b,
          holder,
          ctx.now
        )

      assert multi_renew_b.status == 503
      assert parsed(multi_renew_b)["reason"] == "identity_verification_unavailable"

      # With 2 trusted issuers configured, /v1/lease/handoff/offer omitting to_holder_issuer
      # returns 422 schema_invalid before touching auth/nonces
      missing_issuer_offer =
        post_json_without_proof("/v1/lease/handoff/offer", %{
          lease_id: legacy_lease.lease_id,
          to_holder_agent_uuid: recipient,
          ttl_s: 45
        })

      assert missing_issuer_offer.status == 422
      assert parsed(missing_issuer_offer)["error"] == "schema_invalid"

      assert parsed(missing_issuer_offer)["detail"] ==
               "to_holder_issuer required when multiple trusted issuers are configured"

      # Defense-in-depth: an unpinned offer created directly via HandoffServer.offer/3
      # fails closed (503) on /v1/lease/handoff/accept under 2 trusted issuers
      assert {:ok, direct_handoff_id} =
               UnitaresLeasePlane.HandoffServer.offer(legacy_lease.lease_id, recipient, 45)

      multi_unpinned_accept =
        post_attested(
          "/v1/lease/handoff/accept",
          %{handoff_id: direct_handoff_id},
          "operator-b",
          "kid-b",
          ctx.priv_b,
          recipient,
          ctx.now
        )

      assert multi_unpinned_accept.status == 503
      assert parsed(multi_unpinned_accept)["reason"] == "identity_verification_unavailable"

      # With 1 trusted issuer configured, mutating the same issuerless row and unpinned handoff succeed
      Application.put_env(:lease_plane, :trusted_identity_issuers, %{
        "operator-a" => "https://operator-a.example/keys"
      })

      single_renew_a =
        post_attested(
          "/v1/lease/renew",
          %{lease_id: legacy_lease.lease_id},
          "operator-a",
          "kid-a",
          ctx.priv_a,
          holder,
          ctx.now
        )

      assert single_renew_a.status == 200

      # Create an unpinned (no to_holder_issuer) subject-only handoff offer under single-issuer config
      offer =
        post_attested(
          "/v1/lease/handoff/offer",
          %{
            lease_id: legacy_lease.lease_id,
            to_holder_agent_uuid: recipient,
            to_holder_subject: recipient_subject,
            ttl_s: 45
          },
          "operator-a",
          "kid-a",
          ctx.priv_a,
          holder,
          ctx.now
        )

      assert offer.status == 200
      handoff_id = parsed(offer)["handoff_id"]

      # Distinct to_holder_subject refuses unattested accept in :enforce/:hybrid (legacy proof), :log, and :off
      legacy_accept =
        post_json_with_proof(
          "/v1/lease/handoff/accept",
          %{handoff_id: handoff_id},
          "v1.legacy-recipient.sig"
        )

      assert legacy_accept.status == 403
      assert parsed(legacy_accept)["reason"] == "identity_proof_invalid"

      Application.put_env(:lease_plane, :identity_binding_mode, :log)

      log_unattested_accept =
        post_json_without_proof("/v1/lease/handoff/accept", %{handoff_id: handoff_id})

      assert log_unattested_accept.status == 403
      assert parsed(log_unattested_accept)["reason"] == "identity_proof_invalid"

      Application.put_env(:lease_plane, :identity_binding_mode, :off)

      off_unattested_accept =
        post_json_without_proof("/v1/lease/handoff/accept", %{handoff_id: handoff_id})

      assert off_unattested_accept.status == 403
      assert parsed(off_unattested_accept)["reason"] == "identity_proof_invalid"

      # Under :enforce with 1 trusted issuer, unpinned / subject-only handoff accept succeeds
      # and persists the accepting recipient's verified (issuer, subject) on the new lease row
      Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

      single_accept =
        post_attested(
          "/v1/lease/handoff/accept",
          %{handoff_id: handoff_id},
          "operator-a",
          "kid-a",
          ctx.priv_a,
          recipient_subject,
          ctx.now
        )

      assert single_accept.status == 200

      status =
        :get
        |> conn("/v1/lease/status?surface_id=#{URI.encode_www_form(ctx.surface)}")
        |> authed()
        |> HTTPRouter.call(@opts)

      new_lease = parsed(status)["lease"]
      assert new_lease["holder_agent_uuid"] == recipient
      assert new_lease["holder_issuer"] == "operator-a"
      assert new_lease["holder_subject"] == recipient_subject
    end

    test "pre-084 issuerless row allows attested idempotent reacquire (and substrate update) only under single-issuer config",
         ctx do
      rand = :crypto.strong_rand_bytes(6) |> Base.encode16(case: :lower)
      resident_surface = "resident:/test_elixir_pre084_#{rand}"
      on_exit(fn -> cleanup_surface(resident_surface) end)

      holder = random_uuid()

      # Seed pre-084 (nil, nil) resident lease
      assert {:ok, legacy_lease, :new} =
               UnitaresLeasePlane.Repo.acquire(%{
                 surface_id: resident_surface,
                 holder_agent_uuid: holder,
                 holder_class: "process_instance",
                 holder_kind: "local_beam",
                 ttl_s: 60
               })

      assert legacy_lease.holder_issuer == nil
      assert legacy_lease.holder_subject == nil

      observed_at = DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601()

      reacquire_body =
        acquire_body(resident_surface, holder_agent_uuid: holder)
        |> Map.put(:substrate_state, %{"sensor" => %{"status" => "healthy"}})
        |> Map.put(:substrate_state_observed_at, observed_at)

      # Multi-issuer (2 trusted issuers): attested reacquire on pre-084 (nil, nil) row -> 409 held_by_other
      multi_reacq =
        post_attested(
          "/v1/lease/acquire",
          reacquire_body,
          "operator-b",
          "kid-b",
          ctx.priv_b,
          holder,
          ctx.now
        )

      assert multi_reacq.status == 409
      assert parsed(multi_reacq)["error"] == "held_by_other"

      # Single-issuer (1 trusted issuer): attested reacquire with distinct subject STILL conflicts (409)
      Application.put_env(:lease_plane, :trusted_identity_issuers, %{
        "operator-a" => "https://operator-a.example/keys"
      })

      distinct_reacq =
        post_attested(
          "/v1/lease/acquire",
          Map.put(reacquire_body, :holder_subject, "different-subject"),
          "operator-a",
          "kid-a",
          ctx.priv_a,
          "different-subject",
          ctx.now
        )

      assert distinct_reacq.status == 409
      assert parsed(distinct_reacq)["error"] == "held_by_other"

      # Single-issuer: attested reacquire for (operator-a, holder_uuid) succeeds :idempotent and updates substrate
      single_reacq =
        post_attested(
          "/v1/lease/acquire",
          reacquire_body,
          "operator-a",
          "kid-a",
          ctx.priv_a,
          holder,
          ctx.now
        )

      assert single_reacq.status == 200
      single_body = parsed(single_reacq)
      assert single_body["idempotent"] == true
      assert single_body["lease"]["lease_id"] == legacy_lease.lease_id
      assert single_body["lease"]["holder_issuer"] == nil
      assert single_body["lease"]["holder_subject"] == nil
      assert single_body["lease"]["substrate_state"] == %{"sensor" => %{"status" => "healthy"}}

      # Direct Repo.acquire/1 without allow_legacy_row_attested_reacquire remains fail-closed
      assert {:error, :held_by_other, _info} =
               UnitaresLeasePlane.Repo.acquire(%{
                 surface_id: resident_surface,
                 holder_agent_uuid: holder,
                 holder_issuer: "operator-a",
                 holder_subject: holder,
                 holder_class: "process_instance",
                 holder_kind: "local_beam",
                 ttl_s: 60
               })
    end

    test "invalid holder_subject and handoff principal fields return 422 schema_invalid", ctx do
      for bad <- ["has space", "ctrl\x1csep", "bell\x07char", "nel\u0085char"] do
        bad_acquire =
          post_json_without_proof(
            "/v1/lease/acquire",
            acquire_body(ctx.surface) |> Map.put(:holder_subject, bad)
          )

        assert bad_acquire.status == 422, "expected 422 for holder_subject=#{inspect(bad)}"
        assert parsed(bad_acquire)["error"] == "schema_invalid"
      end

      bad_offer =
        post_json_without_proof("/v1/lease/handoff/offer", %{
          lease_id: random_uuid(),
          to_holder_agent_uuid: random_uuid(),
          to_holder_issuer: "bad issuer",
          ttl_s: 30
        })

      assert bad_offer.status == 422
      assert parsed(bad_offer)["error"] == "schema_invalid"
    end

    test "non-lease /v1/msg/send stays fail-closed when multiple trusted issuers are configured",
         ctx do
      sender = random_uuid()
      recipient = random_uuid()

      msg_body = %{
        topic: "topic:/multi-issuer-fail-closed-#{System.unique_integer([:positive])}",
        sender_agent_uuid: sender,
        recipient_agent_uuid: recipient,
        envelope: %{"kind" => "ping"},
        ttl_s: 60
      }

      resp =
        post_attested(
          "/v1/msg/send",
          msg_body,
          "operator-a",
          "kid-a",
          ctx.priv_a,
          sender,
          ctx.now
        )

      assert resp.status == 503
      assert parsed(resp)["reason"] == "identity_verification_unavailable"
    end
  end
end
