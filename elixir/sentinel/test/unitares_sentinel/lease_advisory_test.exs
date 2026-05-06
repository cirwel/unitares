defmodule UnitaresSentinel.LeaseAdvisoryTest do
  use ExUnit.Case, async: true

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
               timeout_ms: 123,
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
end
