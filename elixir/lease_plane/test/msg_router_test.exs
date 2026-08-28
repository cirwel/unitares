defmodule UnitaresLeasePlane.MsgRouterTest do
  @moduledoc """
  HTTP contract for `/v1/msg/*`.

  The property under test that matters most is the one the governance-KG
  channel never had: a `to-<agent>` tag is advisory and anyone can read it,
  whereas an inbox read here must PROVE it is the addressee.
  """

  use ExUnit.Case, async: false

  import Plug.Test
  import Plug.Conn
  import LeaseTestHelpers, only: [random_uuid: 0]

  alias UnitaresLeasePlane.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  setup do
    Application.put_env(:lease_plane, :bearer_token, @bearer)
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(:lease_plane, :identity_verifier, fn holder, proof ->
      if proof == "proof:" <> holder, do: :ok, else: {:error, :invalid}
    end)

    topic = "topic:/msg-router-test-#{System.unique_integer([:positive])}"

    on_exit(fn ->
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "DELETE FROM lease_plane.topic_messages WHERE topic = $1",
        [topic]
      )

      Application.put_env(:lease_plane, :identity_binding_mode, :off)
      Application.delete_env(:lease_plane, :identity_verifier)
    end)

    {:ok, topic: topic, alice: random_uuid(), bob: random_uuid()}
  end

  defp post_json(path, body, proof \\ nil) do
    :post
    |> conn(path, Jason.encode!(body))
    |> put_req_header("content-type", "application/json")
    |> put_req_header("authorization", "Bearer #{@bearer}")
    |> then(fn c ->
      if proof, do: put_req_header(c, "x-unitares-identity-proof", proof), else: c
    end)
    |> HTTPRouter.call(@opts)
  end

  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  defp send_body(ctx, overrides \\ %{}) do
    Map.merge(
      %{
        topic: ctx.topic,
        sender_agent_uuid: ctx.alice,
        recipient_agent_uuid: ctx.bob,
        envelope: %{"kind" => "finding", "body" => "PR #14 head SHA moved"},
        ttl_s: 3600
      },
      overrides
    )
  end

  describe "identity is verified regardless of the lease rollout mode" do
    # The mailbox's confidentiality boundary must not be a function of how far
    # along the LEASE identity rollout happens to be. The live plane runs
    # `:log`, and docker-compose ships identity_bound_surface_kinds defaulting
    # to "maintenance" -- under an earlier cut that gated on
    # `mode == :enforce`, these routes were either dead (503 on every call) or,
    # with the documented remedy applied, fail-OPEN. authorize_strict/3 removes
    # the coupling: verification always runs.
    setup do
      on_exit(fn ->
        Application.put_env(:lease_plane, :identity_binding_mode, :off)
        Application.delete_env(:lease_plane, :identity_bound_surface_kinds)
      end)

      :ok
    end

    for mode <- [:off, :log, :enforce] do
      test "under mode #{mode}, a valid proof is accepted and a wrong one is refused", ctx do
        Application.put_env(:lease_plane, :identity_binding_mode, unquote(mode))
        # Scoped away from "topic" on purpose: this is the shipped default
        # shape, and it must not weaken the mailbox.
        Application.put_env(:lease_plane, :identity_bound_surface_kinds, ["maintenance"])

        assert post_json("/v1/msg/send", send_body(ctx), "proof:" <> ctx.alice).status == 200

        forged = post_json("/v1/msg/send", send_body(ctx), "proof:" <> ctx.bob)
        assert forged.status == 403
        assert parsed(forged)["reason"] == "identity_proof_invalid"

        stolen =
          post_json("/v1/msg/inbox", %{recipient_agent_uuid: ctx.bob}, "proof:" <> ctx.alice)

        assert stolen.status == 403

        owned =
          post_json("/v1/msg/inbox", %{recipient_agent_uuid: ctx.bob}, "proof:" <> ctx.bob)

        assert owned.status == 200
        assert length(parsed(owned)["messages"]) == 1
      end
    end
  end

  describe "send" do
    test "accepts a proof-bound sender and canonicalizes the topic server-side", ctx do
      shouty = String.upcase(ctx.topic) |> String.replace("TOPIC:/", "topic:/")

      resp =
        post_json("/v1/msg/send", send_body(ctx, %{topic: shouty}), "proof:" <> ctx.alice)

      assert resp.status == 200
      body = parsed(resp)
      assert body["ok"] == true
      # The client does not get to decide topic identity.
      assert body["message"]["topic"] == ctx.topic
      assert body["message"]["delivery_state"] == "pending"
      assert body["message"]["reply_depth"] == 0
    end

    test "refuses a sender who cannot prove they are the sender", ctx do
      resp = post_json("/v1/msg/send", send_body(ctx), "proof:" <> ctx.bob)

      assert resp.status == 403
      assert parsed(resp)["reason"] == "identity_proof_invalid"
    end

    test "refuses a missing proof", ctx do
      resp = post_json("/v1/msg/send", send_body(ctx))
      assert resp.status == 403
    end

    test "rejects a malformed body with 422, not 500", ctx do
      resp = post_json("/v1/msg/send", %{topic: ctx.topic}, "proof:" <> ctx.alice)

      assert resp.status == 422
      assert parsed(resp)["error"] == "schema_invalid"
    end

    test "rejects a topic outside the grammar", ctx do
      resp =
        post_json(
          "/v1/msg/send",
          send_body(ctx, %{topic: "resident:/lumen"}),
          "proof:" <> ctx.alice
        )

      assert resp.status == 422
    end

    test "rejects a TTL past the ephemerality ceiling", ctx do
      resp =
        post_json("/v1/msg/send", send_body(ctx, %{ttl_s: 691_200}), "proof:" <> ctx.alice)

      assert resp.status == 422
    end

    test "a malformed uuid is a 422, not a 503 outage", ctx do
      # uuid_to_binary/1 raises FunctionClauseError on anything that is not a
      # well-formed UUID, and Plug.ErrorHandler renders that as 503
      # service_unavailable -- reporting a caller's typo as an outage.
      for bad <- ["bob", "gggggggg-gggg-gggg-gggg-gggggggggggg", "1234"] do
        resp =
          post_json(
            "/v1/msg/send",
            send_body(ctx, %{recipient_agent_uuid: bad}),
            "proof:" <> ctx.alice
          )

        assert resp.status == 422, "expected 422 for recipient #{inspect(bad)}"
        assert parsed(resp)["error"] == "schema_invalid"
      end

      inbox = post_json("/v1/msg/inbox", %{recipient_agent_uuid: "bob"}, "proof:" <> ctx.bob)
      assert inbox.status == 422
    end

    test "refuses to relocate a thread onto another topic", ctx do
      first = post_json("/v1/msg/send", send_body(ctx), "proof:" <> ctx.alice)
      parent_id = parsed(first)["message"]["message_id"]

      # A reply that names a different topic would silently move the whole
      # conversation somewhere its participants are not reading.
      resp =
        post_json(
          "/v1/msg/send",
          send_body(ctx, %{
            topic: "topic:/somewhere-else-#{System.unique_integer([:positive])}",
            sender_agent_uuid: ctx.bob,
            recipient_agent_uuid: ctx.alice,
            response_to_id: parent_id
          }),
          "proof:" <> ctx.bob
        )

      assert resp.status == 409
      assert parsed(resp)["error"] == "topic_mismatch"
    end

    test "rejects an envelope past the size bound", ctx do
      huge = %{"body" => String.duplicate("x", 65_000)}

      resp =
        post_json("/v1/msg/send", send_body(ctx, %{envelope: huge}), "proof:" <> ctx.alice)

      assert resp.status == 422
    end

    test "404s a reply to a message that does not exist", ctx do
      resp =
        post_json(
          "/v1/msg/send",
          send_body(ctx, %{response_to_id: random_uuid()}),
          "proof:" <> ctx.alice
        )

      assert resp.status == 404
      assert parsed(resp)["reason"] == "response_to_id"
    end
  end

  describe "inbox" do
    test "delivers to the addressee and marks the message delivered", ctx do
      assert post_json("/v1/msg/send", send_body(ctx), "proof:" <> ctx.alice).status == 200

      resp = post_json("/v1/msg/inbox", %{recipient_agent_uuid: ctx.bob}, "proof:" <> ctx.bob)

      assert resp.status == 200
      assert [msg] = parsed(resp)["messages"]
      assert msg["delivery_state"] == "delivered"
      assert msg["envelope"]["kind"] == "finding"

      # Claimed once; a second poll is empty.
      again = post_json("/v1/msg/inbox", %{recipient_agent_uuid: ctx.bob}, "proof:" <> ctx.bob)
      assert parsed(again)["messages"] == []
    end

    test "REFUSES to hand one agent's mail to another", ctx do
      assert post_json("/v1/msg/send", send_body(ctx), "proof:" <> ctx.alice).status == 200

      # Alice holds the same bearer token and asks for Bob's inbox.
      resp = post_json("/v1/msg/inbox", %{recipient_agent_uuid: ctx.bob}, "proof:" <> ctx.alice)

      assert resp.status == 403
      assert parsed(resp)["reason"] == "identity_proof_invalid"

      # And the message is still pending for its actual addressee.
      ok = post_json("/v1/msg/inbox", %{recipient_agent_uuid: ctx.bob}, "proof:" <> ctx.bob)
      assert length(parsed(ok)["messages"]) == 1
    end

    test "rejects an out-of-range limit with 422", ctx do
      resp =
        post_json(
          "/v1/msg/inbox",
          %{recipient_agent_uuid: ctx.bob, limit: 5000},
          "proof:" <> ctx.bob
        )

      assert resp.status == 422
    end
  end
end
