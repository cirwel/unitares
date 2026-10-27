defmodule DialecticLive.FirehoseTest do
  @moduledoc """
  The first Firehose tests: fan-out, backoff and failure labelling, against a
  fake `/ws/eisv` served by Bandit on a loopback port.
  """
  use ExUnit.Case, async: false

  import ExUnit.CaptureLog

  alias DialecticLive.Firehose
  alias DialecticLive.Test.FakeGovernanceWS

  @moduletag :capture_log

  defp start_firehose(url, opts \\ []) do
    id = System.unique_integer([:positive])
    pubsub = :"firehose_pubsub_#{id}"
    name = :"firehose_#{id}"
    start_supervised!({Phoenix.PubSub, name: pubsub}, id: pubsub)

    for topic <- [Firehose.all_topic(), Firehose.dialectic_topic(), Firehose.status_topic()] do
      Phoenix.PubSub.subscribe(pubsub, topic)
    end

    pid =
      start_supervised!(
        {Firehose,
         Keyword.merge(
           [url: url, name: name, pubsub: pubsub, reconnect_min_ms: 20, reconnect_max_ms: 80],
           opts
         )}
      )

    %{pid: pid, name: name, pubsub: pubsub}
  end

  test "frames fan out: every frame to governance:events, dialectic ones to dialectic:events too" do
    url = FakeGovernanceWS.start!(test: self())
    fh = start_firehose(url)

    assert_receive {:fake_ws_up, ws}, 2_000
    assert_receive {:firehose_status, :connected}, 2_000
    assert Firehose.status(fh.name) == :connected
    assert Firehose.connected?(fh.name)

    send(ws, {:push, Jason.encode!(%{"type" => "eisv_update", "agent_id" => "a"})})

    send(
      ws,
      {:push, Jason.encode!(%{"type" => "dialectic_session_resolved", "session_id" => "s"})}
    )

    send(ws, {:push, "not json"})

    assert_receive {:governance_event, %{"type" => "eisv_update"}}, 1_000
    assert_receive {:governance_event, %{"type" => "dialectic_session_resolved"}}, 1_000
    assert_receive {:governance_event, %{"type" => "dialectic_session_resolved"}}, 1_000
    refute_receive {:governance_event, _}, 100
  end

  test "status never travels on dialectic:events, which the dialectic pane treats as a doorbell" do
    url = FakeGovernanceWS.start!(test: self())
    fh = start_firehose(url)
    assert_receive {:firehose_status, :connected}, 2_000

    Phoenix.PubSub.unsubscribe(fh.pubsub, Firehose.all_topic())
    Phoenix.PubSub.unsubscribe(fh.pubsub, Firehose.status_topic())
    assert_receive {:fake_ws_up, ws}
    # Only dialectic:events is still subscribed: the drop and the reconnect
    # publish status twice, and neither may arrive here.
    send(ws, :close)
    assert_receive {:fake_ws_up, _reconnected}, 2_000
    refute_receive {:firehose_status, _}, 200
  end

  test "an unreachable governance is labelled and retried on a doubling backoff with a ceiling" do
    url = "ws://127.0.0.1:#{FakeGovernanceWS.closed_port()}/ws/eisv"

    log =
      capture_log(fn ->
        fh = start_firehose(url)
        assert_receive {:firehose_status, :unreachable}, 1_000
        # Published once per change of state, not once per attempt.
        refute_receive {:firehose_status, _}, 300
        assert Firehose.backoff_ms(fh.name) == 80
        assert Firehose.status(fh.name) == :unreachable
      end)

    assert log =~ "governance unreachable (connection refused)"
    refute log =~ "config failed"
  end

  test "a server that accepts but never answers the upgrade is retried" do
    {:ok, listener} =
      :gen_tcp.listen(0, [:binary, active: false, reuseaddr: true, ip: {127, 0, 0, 1}])

    {:ok, port} = :inet.port(listener)
    test = self()

    # Accept every connection and say nothing, counting the attempts.
    spawn_link(fn ->
      Stream.repeatedly(fn -> :gen_tcp.accept(listener) end)
      |> Enum.each(fn {:ok, _socket} -> send(test, :accepted) end)
    end)

    log =
      capture_log(fn ->
        start_firehose("ws://127.0.0.1:#{port}/ws/eisv", upgrade_timeout_ms: 100)
        assert_receive :accepted, 1_000
        assert_receive {:firehose_status, :unreachable}, 1_000
        assert_receive :accepted, 1_000
      end)

    assert log =~ "no answer to the upgrade within 100 ms"
  end

  test "a refused upgrade is labelled as refused credentials" do
    url = FakeGovernanceWS.start!(test: self(), mode: {:status, 403})

    log =
      capture_log(fn ->
        fh = start_firehose(url)
        assert_receive {:firehose_status, :refused}, 2_000
        assert Firehose.status(fh.name) == :refused
      end)

    assert log =~ "governance refused credentials (HTTP 403 on upgrade)"
  end

  test "a 401 is refused credentials too" do
    url = FakeGovernanceWS.start!(test: self(), mode: {:status, 401})
    _ = start_firehose(url)
    assert_receive {:firehose_status, :refused}, 2_000
  end

  test "the backoff starts at the floor and resets after a successful upgrade" do
    url = FakeGovernanceWS.start!(test: self())
    # A floor long enough that the test reads the doubled value before the
    # reconnect fires, even on a slow runner.
    fh = start_firehose(url, reconnect_min_ms: 400, reconnect_max_ms: 5_000)
    assert_receive {:firehose_status, :connected}, 2_000
    assert Firehose.backoff_ms(fh.name) == 400

    # Peer closes: one failure doubles the delay, the reconnect resets it.
    assert_receive {:fake_ws_up, ws}
    send(ws, :close)
    assert_receive {:firehose_status, :unreachable}, 1_000
    assert Firehose.backoff_ms(fh.name) == 800
    assert_receive {:firehose_status, :connected}, 3_000
    assert Firehose.backoff_ms(fh.name) == 400
  end

  test "a malformed url backs off instead of spinning" do
    log =
      capture_log(fn ->
        fh = start_firehose("http://127.0.0.1:1/not-ws")
        assert_receive {:firehose_status, :unreachable}, 1_000
        assert Firehose.backoff_ms(fh.name) > 20
      end)

    assert log =~ "config failed"
  end
end
