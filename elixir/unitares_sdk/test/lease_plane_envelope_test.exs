defmodule UnitaresSdk.LeasePlaneEnvelopeTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.LeasePlaneEnvelope, as: Env

  describe "classify_acquire/2" do
    test "200 with a lease" do
      assert {:ok, "lease-1"} =
               Env.classify_acquire(200, %{"ok" => true, "lease" => %{"lease_id" => "lease-1"}})
    end

    test "409 held_by_other carries uuid AND the full payload (reclaim callers read intent)" do
      payload = %{
        "error" => "held_by_other",
        "held_by_uuid" => "uuid-x",
        "intent" => "session"
      }

      assert {:error, {:held_by_other, "uuid-x", ^payload}} =
               Env.classify_acquire(409, payload)
    end

    test "409 held_by_other without a uuid still classifies" do
      assert {:error, {:held_by_other, nil, _}} =
               Env.classify_acquire(409, %{"error" => "held_by_other"})
    end

    test "error-keyed bodies are typed with reason/detail" do
      assert {:error, {:lease_plane_error, 400, "bad_surface", "no scheme"}} =
               Env.classify_acquire(400, %{"error" => "bad_surface", "reason" => "no scheme"})
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

    test "other failures are typed" do
      assert {:error, {:release_failed, 500, _}} =
               Env.classify_release(500, %{"error" => "boom"})
    end
  end
end
