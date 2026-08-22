defmodule UnitaresSdk.LeasePlaneEnvelopeTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.LeasePlaneEnvelope, as: Env

  describe "classify_acquire/2" do
    test "200 with a lease, ignoring the server's top-level extras" do
      payload = %{
        "ok" => true,
        "protocol_version" => "v1.0",
        "lease" => %{"lease_id" => "lease-1", "holder_kind" => "remote_heartbeat"},
        "idempotent" => false,
        "drift_warning" => []
      }

      assert {:ok, "lease-1"} = Env.classify_acquire(200, payload)
    end

    test "409 held_by_other carries uuid AND the real server payload (retry callers read hints)" do
      # Field set matches the live router's 409 literal — the body does NOT
      # carry the blocking lease's `intent` (that needs GET /v1/lease/status).
      payload = %{
        "ok" => false,
        "error" => "held_by_other",
        "held_by_uuid" => "11111111-1111-1111-1111-111111111111",
        "blocking_lease_id" => "5287905e-df31-40b6-b9da-94645e37309c",
        "expires_at" => "2026-08-22T13:34:34Z",
        "retry_after_hint_ms" => 5000,
        "surface_id" => "file:///private/tmp/x"
      }

      assert {:error, {:held_by_other, "11111111-1111-1111-1111-111111111111", carried}} =
               Env.classify_acquire(409, payload)

      # The tuple must not drop the retry hint — losing 409 detail broke
      # acquire retries in production once already.
      assert carried["retry_after_hint_ms"] == 5000
    end

    test "409 held_by_other without a uuid still classifies" do
      assert {:error, {:held_by_other, nil, _}} =
               Env.classify_acquire(409, %{"error" => "held_by_other"})
    end

    test "error-keyed bodies are typed with reason/detail" do
      assert {:error, {:lease_plane_error, 422, "schema_invalid", "missing required fields"}} =
               Env.classify_acquire(422, %{
                 "error" => "schema_invalid",
                 "detail" => "missing required fields"
               })
    end

    test "a policy refusal on 200 (ok:false + error) is never read as acquired" do
      assert {:error, {:lease_plane_error, 200, "permission_denied", _}} =
               Env.classify_acquire(200, %{"ok" => false, "error" => "permission_denied"})
    end

    test "unexpected shapes never read as acquired" do
      assert {:error, {:lease_plane_unexpected, 200, _}} =
               Env.classify_acquire(200, %{"ok" => true})
    end
  end

  describe "classify_release/2" do
    test "200 ok" do
      assert :ok = Env.classify_release(200, %{"ok" => true})
    end

    test "404 is success — the lease is gone either way" do
      assert :ok = Env.classify_release(404, %{"error" => "not_found"})
    end

    test "an enum-rejected release_reason (422) is a refusal with the name surfaced" do
      # Live-verified 2026-08-22: release_reason is a closed server-side enum;
      # an ad-hoc reason gets 422 and the lease STAYS HELD.
      assert {:error, {:release_refused, 422, "schema_invalid", _}} =
               Env.classify_release(422, %{
                 "ok" => false,
                 "error" => "schema_invalid",
                 "detail" => "invalid release_reason"
               })
    end

    test "a policy refusal on 200 is a typed refusal, not release_failed noise" do
      assert {:error, {:release_refused, 200, "permission_denied", _}} =
               Env.classify_release(200, %{"ok" => false, "error" => "permission_denied"})
    end

    test "shapeless failures stay typed" do
      assert {:error, {:release_failed, 500, _}} =
               Env.classify_release(500, %{"boom" => true})
    end
  end
end
