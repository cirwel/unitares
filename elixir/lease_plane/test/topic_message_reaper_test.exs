defmodule UnitaresLeasePlane.TopicMessageReaperTest do
  @moduledoc """
  The reaper is what makes migration 069's "ephemeral BY CONSTRUCTION" true.

  An earlier cut of this change shipped `purge_expired_messages/1` with no
  caller anywhere in `lib/`: expired rows were filtered on read and never
  deleted, so the table accumulated forever — the same defect it indicts the
  governance KG for, restated as rows nothing searches instead of notes nothing
  closes. These tests pin both halves: the reaper deletes, and it is wired.
  """

  use ExUnit.Case, async: false

  import LeaseTestHelpers, only: [random_uuid: 0]

  alias UnitaresLeasePlane.{Repo, TopicMessageReaper}

  @topic "topic:/reaper-test"

  setup do
    on_exit(fn ->
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "DELETE FROM lease_plane.topic_messages WHERE topic = $1",
        [@topic]
      )
    end)

    :ok
  end

  defp send!(ttl_s) do
    {:ok, msg} =
      Repo.send_message(%{
        topic: @topic,
        sender_agent_uuid: random_uuid(),
        recipient_agent_uuid: random_uuid(),
        envelope: %{},
        response_to_id: nil,
        ttl_s: ttl_s
      })

    msg
  end

  defp count do
    %{rows: [[n]]} =
      Postgrex.query!(
        UnitaresLeasePlane.DB,
        "SELECT count(*) FROM lease_plane.topic_messages WHERE topic = $1",
        [@topic]
      )

    n
  end

  test "run_once deletes expired rows and leaves live ones" do
    expired = send!(60)
    _live = send!(3600)

    Postgrex.query!(
      UnitaresLeasePlane.DB,
      # Compare as text: Postgrex binds uuid params as 16-byte binaries, and
      # the repo hands back the 36-char form.
      "UPDATE lease_plane.topic_messages SET expires_at = now() - interval '1 second' " <>
        "WHERE message_id::text = $1",
      [expired.message_id]
    )

    assert count() == 2
    assert :ok = TopicMessageReaper.run_once()
    assert count() == 1
  end

  test "the reaper is actually wired into the supervision tree" do
    # Source-level on purpose: children/0 is a local inside start/2, and the
    # defect being guarded against is precisely a purge function that exists
    # while nothing schedules it.
    source =
      Path.join(__DIR__, "../lib/unitares_lease_plane/application.ex")
      |> Path.expand()
      |> File.read!()

    assert source =~ "worker: UnitaresLeasePlane.TopicMessageReaper",
           "TopicMessageReaper must be a PeriodicWorker child, or expiry never runs"
  end
end
