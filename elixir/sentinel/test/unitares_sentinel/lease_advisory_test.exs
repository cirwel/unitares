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

  # --- own-orphan reclaim (2026-08-01 double-lost-response incident) ---------
  #
  # 2026-08-01 15:42: a Postgres stall pushed the plane past the client's 2s
  # budget on BOTH the acquire and its recovery retry while the first INSERT
  # had already committed. The attempt's holder uuid was discarded, every later
  # tick minted a fresh uuid, and the poller starved for 1h49m (216 ticks) on
  # held_by_other responses that were naming the orphan's holder uuid — a uuid
  # this process itself had minted — plus the blocking_lease_id needed to free
  # it. With `reclaim_candidates` threaded (see `UnitaresSentinel.LeaseReclaim`),
  # the advisory recognizes such a conflict as its own stranded lease,
  # releases it, and re-acquires in the same call.

  @other_lease_id "44444444-4444-4444-4444-444444444444"

  test "double transport failure carries the attempted holder uuid" do
    http_post = fn _url, _body, _headers, _timeout_ms -> {:error, :timeout} end

    assert %{
             outcome: :service_unavailable,
             lease_id: nil,
             conflict: %{attempted_holder_uuid: @holder_uuid}
           } =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               http_post: http_post
             )
  end

  test "enforcement preserves the attempted holder uuid for reclaim memory" do
    http_post = fn _url, _body, _headers, _timeout_ms -> {:error, :timeout} end

    assert %{
             outcome: :enforcement_blocked,
             conflict: %{
               blocked_outcome: :service_unavailable,
               attempted_holder_uuid: @holder_uuid
             }
           } =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               holder_agent_uuid: @holder_uuid,
               enforced_surface_kinds: MapSet.new(["resident"]),
               http_post: http_post
             )
  end

  test "held_by_other naming our own prior attempt releases the orphan and re-acquires" do
    {:ok, calls} = Agent.start_link(fn -> [] end)

    http_post = fn url, body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 ++ [{url, body}]))

      case Agent.get(calls, &length/1) do
        1 ->
          assert String.ends_with?(url, "/v1/lease/acquire")

          {:ok, 409,
           Jason.encode!(%{
             ok: false,
             error: "held_by_other",
             held_by_uuid: @holder_uuid,
             blocking_lease_id: @lease_id
           })}

        2 ->
          assert String.ends_with?(url, "/v1/lease/release")

          assert body == %{
                   "lease_id" => @lease_id,
                   "release_reason" => "reclaimed_lost_acquire"
                 }

          {:ok, 200, ~s({"ok":true})}

        3 ->
          assert String.ends_with?(url, "/v1/lease/acquire")

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: @other_lease_id},
             drift_warning: []
           })}
      end
    end

    assert %{
             outcome: :acquired_new,
             lease_id: @other_lease_id,
             conflict: %{reclaimed_lease_id: @lease_id}
           } =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               reclaim_candidates: [@holder_uuid],
               http_post: http_post
             )

    recorded = Agent.get(calls, & &1)
    assert length(recorded) == 3

    # The re-acquire is a NEW attempt with a fresh uuid, not a resurrection of
    # the stranded one — per-attempt uuids are the double-grant safety.
    [{_, first_acquire}, _release, {_, reacquire}] = recorded
    refute reacquire["holder_agent_uuid"] == first_acquire["holder_agent_uuid"]
    refute reacquire["holder_agent_uuid"] == @holder_uuid
  end

  test "held_by_other naming a foreign holder is not reclaimed" do
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    http_post = fn _url, _body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 + 1))

      {:ok, 409,
       Jason.encode!(%{
         ok: false,
         error: "held_by_other",
         held_by_uuid: "33333333-3333-3333-3333-333333333333",
         blocking_lease_id: @lease_id
       })}
    end

    assert %{outcome: :held_by_other} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               reclaim_candidates: [@holder_uuid],
               http_post: http_post
             )

    assert Agent.get(calls, & &1) == 1, "no release, no re-acquire for a foreign holder"
  end

  test "reclaim keeps the conflict and retries next tick when the release fails" do
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    http_post = fn url, _body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 + 1))

      case Agent.get(calls, & &1) do
        1 ->
          {:ok, 409,
           Jason.encode!(%{
             ok: false,
             error: "held_by_other",
             held_by_uuid: @holder_uuid,
             blocking_lease_id: @lease_id
           })}

        # Both the reclaimed_lost_acquire attempt and its 'normal'
        # deploy-order fallback die at the transport.
        n when n in [2, 3] ->
          assert String.ends_with?(url, "/v1/lease/release")
          {:error, :timeout}
      end
    end

    assert %{
             outcome: :held_by_other,
             lease_id: nil,
             conflict: %{reclaim_failed: true, blocking_lease_id: @lease_id}
           } =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               reclaim_candidates: [@holder_uuid],
               http_post: http_post
             )

    assert Agent.get(calls, & &1) == 3, "no re-acquire after a failed release"
  end

  test "reclaim release falls back to 'normal' when the plane predates the reason" do
    {:ok, calls} = Agent.start_link(fn -> [] end)

    http_post = fn url, body, _headers, _timeout_ms ->
      Agent.update(calls, &(&1 ++ [{url, body}]))

      case Agent.get(calls, &length/1) do
        1 ->
          {:ok, 409,
           Jason.encode!(%{
             ok: false,
             error: "held_by_other",
             held_by_uuid: @holder_uuid,
             blocking_lease_id: @lease_id
           })}

        2 ->
          assert body["release_reason"] == "reclaimed_lost_acquire"

          {:ok, 422,
           Jason.encode!(%{ok: false, error: "schema_invalid", detail: "invalid release_reason"})}

        3 ->
          assert String.ends_with?(url, "/v1/lease/release")
          assert body["release_reason"] == "normal"
          {:ok, 200, ~s({"ok":true})}

        4 ->
          assert String.ends_with?(url, "/v1/lease/acquire")

          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: @other_lease_id},
             drift_warning: []
           })}
      end
    end

    assert %{outcome: :acquired_new, lease_id: @other_lease_id} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               reclaim_candidates: [@holder_uuid],
               http_post: http_post
             )
  end

  # The deterministic regression for the incident itself: the server commits
  # the acquisition, BOTH client responses (original + recovery retry) are
  # lost, and a later tick recovers via the candidate memory instead of
  # starving until an operator force-releases.
  test "regression: committed acquire with both responses lost is reclaimed on a later tick" do
    {:ok, tick1_calls} = Agent.start_link(fn -> [] end)

    tick1_post = fn _url, body, _headers, _timeout_ms ->
      Agent.update(tick1_calls, &(&1 ++ [body["holder_agent_uuid"]]))
      {:error, :timeout}
    end

    tick1 = LeaseAdvisory.acquire_cycle(bearer_token: "test-token", http_post: tick1_post)

    assert %{outcome: :service_unavailable, conflict: %{attempted_holder_uuid: stranded_uuid}} =
             tick1

    # Both attempts used the SAME uuid — the one the server committed under.
    assert [^stranded_uuid, ^stranded_uuid] = Agent.get(tick1_calls, & &1)

    # Tick 2: the plane answers again; the orphan committed under
    # stranded_uuid blocks the surface. The candidate memory (threaded by
    # LeaseReclaim from the scope above) recognizes and reclaims it.
    {:ok, tick2_calls} = Agent.start_link(fn -> [] end)

    tick2_post = fn url, body, _headers, _timeout_ms ->
      Agent.update(tick2_calls, &(&1 ++ [{url, body}]))

      case Agent.get(tick2_calls, &length/1) do
        1 ->
          {:ok, 409,
           Jason.encode!(%{
             ok: false,
             error: "held_by_other",
             held_by_uuid: stranded_uuid,
             blocking_lease_id: @lease_id
           })}

        2 ->
          assert String.ends_with?(url, "/v1/lease/release")
          assert body["lease_id"] == @lease_id
          {:ok, 200, ~s({"ok":true})}

        3 ->
          {:ok, 200,
           Jason.encode!(%{
             ok: true,
             idempotent: false,
             lease: %{lease_id: @other_lease_id},
             drift_warning: []
           })}
      end
    end

    assert %{outcome: :acquired_new, lease_id: @other_lease_id} =
             LeaseAdvisory.acquire_cycle(
               bearer_token: "test-token",
               reclaim_candidates: [stranded_uuid],
               http_post: tick2_post
             )
  end
end

