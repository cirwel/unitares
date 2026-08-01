defmodule UnitaresSentinel.LeaseAdvisoryTest do
  use ExUnit.Case, async: false

  alias UnitaresSentinel.LeaseAdvisory

  @holder_uuid "11111111-1111-1111-1111-111111111111"
  @lease_id "22222222-2222-2222-2222-222222222222"

  test "acquire_cycle mirrors Python Sentinel advisory request" do
    http_post = fn url, body, headers, timeout_ms ->
      assert url == "http://lease.test/v1/lease/acquire"
      assert body["surface_id"] == "resident:/sentinel_cycle"
      assert body["holder_agent_uuid"] == @holder_uuid
      assert body["holder_class"] == "process_instance"
      assert body["holder_kind"] == "remote_heartbeat"
      assert body["ttl_s"] == 300
      assert body["intent"] == "sentinel analysis cycle"
      assert body["audit_session"] == "agent-session-1"
      assert {"Authorization", "Bearer test-token"} in headers
      assert {"Accept", "application/json"} in headers
      assert {"Content-Type", "application/json"} in headers
      assert timeout_ms == 123

      {:ok, 200,
       Jason.encode!(%{
         ok: true,
         idempotent: false,
         lease: %{lease_id: @lease_id},
         drift_warning: []
       })}
    end

    assert %{outcome: :acquired_new, lease_id: @lease_id} =
             LeaseAdvisory.acquire_cycle(
               base_url: "http://lease.test",
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               audit_session: "agent-session-1",
               timeout_ms: 123,
               http_post: http_post
             )
  end

  test "acquire_cycle derives audit_session from session anchor when present" do
    http_post = fn _url, body, _headers, _timeout_ms ->
      assert body["audit_session"] == "anchor-session-1"

      {:ok, 200,
       Jason.encode!(%{
         ok: true,
         idempotent: false,
         lease: %{lease_id: @lease_id},
         drift_warning: []
       })}
    end

    assert %{outcome: :acquired_new, lease_id: @lease_id} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               anchor: %{"agent_uuid" => @holder_uuid, "client_session_id" => "anchor-session-1"},
               http_post: http_post
             )
  end

  test "acquire_cycle uses configured audit_session before session anchor" do
    original = Application.get_env(:unitares_sentinel, :lease_audit_session)
    Application.put_env(:unitares_sentinel, :lease_audit_session, "configured-session-1")

    on_exit(fn ->
      if is_nil(original) do
        Application.delete_env(:unitares_sentinel, :lease_audit_session)
      else
        Application.put_env(:unitares_sentinel, :lease_audit_session, original)
      end
    end)

    http_post = fn _url, body, _headers, _timeout_ms ->
      assert body["audit_session"] == "configured-session-1"

      {:ok, 200,
       Jason.encode!(%{
         ok: true,
         idempotent: false,
         lease: %{lease_id: @lease_id},
         drift_warning: []
       })}
    end

    assert %{outcome: :acquired_new, lease_id: @lease_id} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               anchor: %{"agent_uuid" => @holder_uuid, "client_session_id" => "anchor-session-1"},
               http_post: http_post
             )
  end

  test "missing bearer token disables advisory acquire without HTTP" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      flunk("HTTP should not be called without LEASE_PLANE_BEARER_TOKEN")
    end

    assert %{outcome: :service_unavailable, lease_id: nil} =
             LeaseAdvisory.acquire_cycle(bearer_token: "", http_post: http_post)
  end

  test "missing lease blocks when surface kind is enforced" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 409,
       Jason.encode!(%{
         ok: false,
         error: "held_by_other",
         held_by_uuid: @holder_uuid
       })}
    end

    assert %{outcome: :enforcement_blocked, lease_id: nil} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               enforced_surface_kinds: MapSet.new(["resident"]),
               http_post: http_post
             )
  end

  test "missing bearer token blocks when surface kind is enforced" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      flunk("HTTP should not be called without LEASE_PLANE_BEARER_TOKEN")
    end

    assert %{outcome: :enforcement_blocked, lease_id: nil} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "",
               enforced_surface_kinds: MapSet.new(["resident"]),
               http_post: http_post
             )
  end

  test "acquire_advisory classifies typed absence responses" do
    cases = [
      {409, %{ok: false, error: "held_by_other", held_by_uuid: @holder_uuid}, :held_by_other},
      {200, %{ok: false, error: "permission_denied", reason: "nope"}, :permission_denied},
      {422, %{ok: false, error: "schema_invalid", detail: "bad"}, :schema_invalid},
      {503, %{ok: false, error: "service_unavailable"}, :service_unavailable},
      {200, %{ok: false, error: "something_else"}, :client_error}
    ]

    for {status, response, outcome} <- cases do
      http_post = fn _url, _body, _headers, _timeout_ms ->
        {:ok, status, Jason.encode!(response)}
      end

      assert %{outcome: ^outcome, lease_id: nil} =
               LeaseAdvisory.acquire_advisory(%{"surface_id" => "resident:/sentinel_cycle"},
                 bearer_token: "test-token",
                 http_post: http_post
               )
    end
  end

  test "acquire_advisory classifies HTTP error responses without JSON bodies" do
    cases = [
      {401, :permission_denied},
      {403, :permission_denied},
      {500, :service_unavailable},
      {200, :schema_invalid}
    ]

    for {status, outcome} <- cases do
      http_post = fn _url, _body, _headers, _timeout_ms -> {:ok, status, "not-json"} end

      assert %{outcome: ^outcome, lease_id: nil} =
               LeaseAdvisory.acquire_advisory(%{"surface_id" => "resident:/sentinel_cycle"},
                 bearer_token: "test-token",
                 http_post: http_post
               )
    end
  end

  test "release posts normal release and swallows failures" do
    http_post = fn url, body, headers, timeout_ms ->
      assert url == "http://lease.test/v1/lease/release"
      assert body == %{"lease_id" => @lease_id, "release_reason" => "normal"}
      assert {"Authorization", "Bearer test-token"} in headers
      assert timeout_ms == 456

      {:ok, 200, ~s({"ok":true})}
    end

    assert :ok =
             LeaseAdvisory.release(@lease_id,
               base_url: "http://lease.test",
               bearer_token: "test-token",
               timeout_ms: 456,
               http_post: http_post
             )

    assert :ok =
             LeaseAdvisory.release(@lease_id,
               bearer_token: "test-token",
               http_post: fn _url, _body, _headers, _timeout_ms -> raise "boom" end
             )
  end

  # --- lost-acquire-response recovery (2026-07-29 Sentinel 24h outage) -------
  #
  # An acquire that commits server-side but whose response is lost leaves a
  # lease the client cannot identify, cannot release, and that the lease plane
  # then auto-renews forever. One retry with the same body recovers it via
  # idempotent re-acquire.

  test "acquire retries once on transport error and adopts the committed lease" do
    {:ok, calls} = Agent.start_link(fn -> [] end)

    http_post = fn _url, body, _headers, _timeout_ms ->
      Agent.update(calls, &[body["holder_agent_uuid"] | &1])

      case Agent.get(calls, &length/1) do
        1 ->
          # committed server-side; response lost
          {:error, :timeout}

        _ ->
          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: true,
             lease: %{lease_id: @lease_id},
             drift_warning: []
           })}
      end
    end

    assert %{outcome: :acquired_idempotent, lease_id: @lease_id} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               http_post: http_post
             )

    uuids = Agent.get(calls, & &1)
    assert length(uuids) == 2, "expected exactly one retry"

    # The retry MUST reuse the same holder uuid — that is what makes the
    # re-acquire idempotent rather than a fresh contending acquire.
    assert [@holder_uuid, @holder_uuid] = uuids
  end

  test "acquire gives up after one retry and does not loop" do
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    http_post = fn _url, _body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 + 1))
      {:error, :timeout}
    end

    assert %{outcome: :service_unavailable} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               http_post: http_post
             )

    assert Agent.get(calls, & &1) == 2
  end

  test "a successful acquire is not retried" do
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    http_post = fn _url, _body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 + 1))

      {:ok, 200,
       Jason.encode!(%{
         ok: true,
         idempotent: false,
         lease: %{lease_id: @lease_id},
         drift_warning: []
       })}
    end

    assert %{outcome: :acquired_new, lease_id: @lease_id} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               http_post: http_post
             )

    assert Agent.get(calls, & &1) == 1
  end

  test "held_by_other is a real conflict and is not retried" do
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    http_post = fn _url, _body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 + 1))

      {:ok, 409,
       Jason.encode!(%{
         ok: false,
         error: "held_by_other",
         held_by_uuid: "33333333-3333-3333-3333-333333333333"
       })}
    end

    assert %{outcome: :held_by_other} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               http_post: http_post
             )

    assert Agent.get(calls, & &1) == 1, "a conflict is an answer, not a transport failure"
  end
end

