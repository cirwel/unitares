defmodule UnitaresLeasePlane.TopicMessagesTest do
  @moduledoc """
  Transport tests for `lease_plane.topic_messages` (migration 069).

  These assert the properties the governance-KG channel could not offer:
  a message goes to ONE named recipient, expires, carries a delivery state,
  and cannot be claimed twice.
  """

  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.Repo

  import LeaseTestHelpers, only: [random_uuid: 0]

  setup do
    a = random_uuid()
    b = random_uuid()
    topic = "topic:/pipeline-test-#{System.unique_integer([:positive])}"

    on_exit(fn ->
      Postgrex.query!(UnitaresLeasePlane.DB, "DELETE FROM lease_plane.topic_messages WHERE topic = $1", [topic])
    end)

    {:ok, alice: a, bob: b, topic: topic}
  end

  defp send!(ctx, overrides \\ %{}) do
    params =
      Map.merge(
        %{
          topic: ctx.topic,
          sender_agent_uuid: ctx.alice,
          recipient_agent_uuid: ctx.bob,
          envelope: %{"body" => "hello"},
          response_to_id: nil,
          ttl_s: 3600
        },
        overrides
      )

    Repo.send_message(params)
  end

  describe "send" do
    test "round-trips an envelope", ctx do
      assert {:ok, msg} = send!(ctx)
      assert msg.topic == ctx.topic
      assert msg.sender_agent_uuid == ctx.alice
      assert msg.recipient_agent_uuid == ctx.bob
      assert msg.envelope == %{"body" => "hello"}
      assert msg.delivery_state == "pending"
      assert msg.reply_depth == 0
      assert is_nil(msg.delivered_at)
    end

    test "refuses a message addressed to its own sender", ctx do
      assert {:error, :self_addressed} =
               send!(ctx, %{recipient_agent_uuid: ctx.alice})
    end

    test "refuses a topic outside the topic:/ grammar", ctx do
      assert {:error, :invalid_topic} = send!(ctx, %{topic: "file:///etc/passwd"})
    end

    test "refuses a TTL beyond the ephemerality bound", ctx do
      # 8 days. The point of the table is that nothing lives forever.
      assert {:error, :ttl_out_of_range} = send!(ctx, %{ttl_s: 691_200})
    end

    test "refuses a reply to a message that does not exist", ctx do
      assert {:error, :parent_not_found} =
               send!(ctx, %{response_to_id: random_uuid()})
    end
  end

  describe "reply depth" do
    test "is derived from the parent, not taken from the caller", ctx do
      {:ok, first} = send!(ctx)

      {:ok, second} =
        send!(ctx, %{
          sender_agent_uuid: ctx.bob,
          recipient_agent_uuid: ctx.alice,
          response_to_id: first.message_id
        })

      assert second.reply_depth == 1
    end

    test "caps a mutual-reply loop at 16", ctx do
      # Walk a full chain of alternating replies and assert the transport —
      # not either harness — is what stops it.
      {:ok, first} = send!(ctx)

      last =
        Enum.reduce(1..16, first, fn i, parent ->
          {sender, recipient} =
            if rem(i, 2) == 1, do: {ctx.bob, ctx.alice}, else: {ctx.alice, ctx.bob}

          case send!(ctx, %{
                 sender_agent_uuid: sender,
                 recipient_agent_uuid: recipient,
                 response_to_id: parent.message_id
               }) do
            {:ok, msg} ->
              assert msg.reply_depth == i
              msg

            {:error, :reply_depth_exceeded} ->
              flunk("depth #{i} refused early; the cap is 16")
          end
        end)

      assert last.reply_depth == 16

      assert {:error, :reply_depth_exceeded} =
               send!(ctx, %{
                 sender_agent_uuid: ctx.alice,
                 recipient_agent_uuid: ctx.bob,
                 response_to_id: last.message_id
               })
    end
  end

  describe "inbox" do
    test "returns only the addressee's mail", ctx do
      {:ok, _for_bob} = send!(ctx)

      carol = random_uuid()
      assert {:ok, []} = Repo.inbox(carol, 10)
      assert {:ok, [msg]} = Repo.inbox(ctx.bob, 10)
      assert msg.recipient_agent_uuid == ctx.bob
    end

    test "marks what it returns as delivered, and does not return it twice", ctx do
      {:ok, _} = send!(ctx)

      assert {:ok, [msg]} = Repo.inbox(ctx.bob, 10)
      assert msg.delivery_state == "delivered"
      refute is_nil(msg.delivered_at)

      # The claim is the read. A second poll sees nothing.
      assert {:ok, []} = Repo.inbox(ctx.bob, 10)
    end

    test "concurrent pollers get disjoint sets, never a duplicate", ctx do
      for _ <- 1..20, do: {:ok, _} = send!(ctx)

      results =
        1..4
        |> Task.async_stream(fn _ -> Repo.inbox(ctx.bob, 20) end,
          max_concurrency: 4,
          timeout: 15_000
        )
        |> Enum.flat_map(fn {:ok, {:ok, msgs}} -> msgs end)

      ids = Enum.map(results, & &1.message_id)

      assert length(ids) == 20
      assert length(Enum.uniq(ids)) == 20
    end

    test "never delivers expired mail", ctx do
      {:ok, msg} = send!(ctx, %{ttl_s: 60})

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE lease_plane.topic_messages SET expires_at = now() - interval '1 second' WHERE message_id = $1",
        [uuid_bin(msg.message_id)]
      )

      assert {:ok, []} = Repo.inbox(ctx.bob, 10)
    end

    test "returns a batch in created_at order", ctx do
      # UPDATE ... RETURNING has no defined row order; the ORDER BY inside the
      # claim CTE decides only WHICH rows are taken. Without the outer ordered
      # SELECT a caller reconstructing a thread from one batch can see a reply
      # before the message it answers.
      for _ <- 1..25, do: {:ok, _} = send!(ctx)

      assert {:ok, msgs} = Repo.inbox(ctx.bob, 25)
      assert length(msgs) == 25

      stamps = Enum.map(msgs, & &1.created_at)
      assert stamps == Enum.sort(stamps, &(DateTime.compare(&1, &2) != :gt))
    end

    test "honours the limit and returns oldest first", ctx do
      for _ <- 1..3, do: {:ok, _} = send!(ctx)

      assert {:ok, [first]} = Repo.inbox(ctx.bob, 1)
      assert {:ok, rest} = Repo.inbox(ctx.bob, 10)
      assert length(rest) == 2
      assert Enum.all?(rest, &(DateTime.compare(&1.created_at, first.created_at) != :lt))
    end
  end

  describe "purge" do
    test "removes expired rows regardless of delivery state", ctx do
      {:ok, msg} = send!(ctx)

      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "UPDATE lease_plane.topic_messages SET expires_at = now() - interval '1 second' WHERE message_id = $1",
        [uuid_bin(msg.message_id)]
      )

      assert {:ok, n} = Repo.purge_expired_messages(100)
      assert n >= 1

      assert %{rows: [[0]]} =
               Postgrex.query!(
                 UnitaresLeasePlane.DB,
                 "SELECT count(*) FROM lease_plane.topic_messages WHERE message_id = $1",
                 [uuid_bin(msg.message_id)]
               )
    end
  end

  # Postgrex needs the 16-byte binary form for uuid parameters, the same
  # conversion Repo.uuid_to_binary/1 does on the write path.
  defp uuid_bin(<<a::binary-size(8), "-", b::binary-size(4), "-", c::binary-size(4), "-",
                  d::binary-size(4), "-", e::binary-size(12)>>) do
    Base.decode16!(a <> b <> c <> d <> e, case: :mixed)
  end
end
