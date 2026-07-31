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

  # 2026-07-31 immortal-lease incident. The lease plane has always sent
  # `blocking_lease_id` on the 409 body (http_router.ex:84-92, populated by
  # repo.ex:113-120); this client read only `held_by_uuid` for a log line and
  # dropped the rest one line later. That id is the argument to
  # `POST /v1/lease/force-release`, which is what makes a starvation finding
  # actionable instead of merely informative.
  test "held_by_other scope carries blocking_lease_id for force-release" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 409,
       Jason.encode!(%{
         ok: false,
         error: "held_by_other",
         held_by_uuid: @holder_uuid,
         blocking_lease_id: @lease_id,
         expires_at: "2026-07-31T21:16:03Z",
         retry_after_hint_ms: 500
       })}
    end

    assert %{
             outcome: :held_by_other,
             lease_id: nil,
             conflict: %{
               blocking_lease_id: @lease_id,
               held_by_uuid: @holder_uuid,
               expires_at: "2026-07-31T21:16:03Z"
             }
           } =
             LeaseAdvisory.acquire_advisory(%{"surface_id" => "dialectic:/unenforced"},
               bearer_token: "test-token",
               http_post: http_post
             )
  end

  # `:enforcement_blocked` conflates held_by_other, permission_denied,
  # schema_invalid, client_error AND a missing bearer token. Overwriting
  # `:outcome` destroyed the only record of *why*, so a downstream finding could
  # only ever name the right remedy by luck.
  test "enforcement_blocked scope preserves the pre-enforcement outcome and the surface id" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 409,
       Jason.encode!(%{
         ok: false,
         error: "held_by_other",
         held_by_uuid: @holder_uuid,
         blocking_lease_id: @lease_id
       })}
    end

    assert %{
             outcome: :enforcement_blocked,
             lease_id: nil,
             conflict: %{
               blocked_outcome: :held_by_other,
               surface_id: "resident:/sentinel_cycle",
               blocking_lease_id: @lease_id
             }
           } =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               enforced_surface_kinds: MapSet.new(["resident"]),
               http_post: http_post
             )
  end

  test "enforcement_blocked scope from a missing bearer token reports no blocking lease" do
    scope =
      LeaseAdvisory.acquire_cycle(
        bearer_token: "",
        enforced_surface_kinds: MapSet.new(["resident"]),
        http_post: fn _url, _body, _headers, _timeout_ms -> flunk("no HTTP without a token") end
      )

    assert %{
             outcome: :enforcement_blocked,
             conflict: %{
               blocked_outcome: :service_unavailable,
               surface_id: "resident:/sentinel_cycle"
             }
           } = scope

    refute Map.has_key?(scope.conflict, :blocking_lease_id)
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
