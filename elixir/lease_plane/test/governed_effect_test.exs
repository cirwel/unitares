defmodule UnitaresLeasePlane.GovernedEffectTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.GovernedEffect

  defp base(overrides \\ %{}) do
    Map.merge(
      %{
        "idempotency_key" => "idem-#{System.unique_integer([:positive])}",
        "custody_mode" => "record_only",
        "effect_type" => "file_write",
        "surface" => "repo://unitares/doc_update",
        "required_leases" => []
      },
      overrides
    )
  end

  describe "validation" do
    test "missing idempotency_key → schema_invalid detail" do
      assert {:error, detail} = GovernedEffect.handle(Map.delete(base(), "idempotency_key"))
      assert detail =~ "idempotency_key"
    end

    test "invalid custody_mode → schema_invalid" do
      assert {:error, detail} = GovernedEffect.handle(base(%{"custody_mode" => "sideways"}))
      assert detail =~ "custody_mode"
    end

    test "missing effect_type / surface → schema_invalid" do
      assert {:error, _} = GovernedEffect.handle(Map.delete(base(), "effect_type"))
      assert {:error, _} = GovernedEffect.handle(Map.delete(base(), "surface"))
    end

    test "malformed required_leases → schema_invalid" do
      assert {:error, detail} = GovernedEffect.handle(base(%{"required_leases" => [%{"x" => 1}]}))
      assert detail =~ "required_leases"
    end

    test "non-object body → error" do
      assert {:error, _} = GovernedEffect.handle("nope")
    end
  end

  describe "Invariant 7 — credential scrub" do
    test "credential-shaped payload key is rejected" do
      for key <- ~w(client_session_id continuity_token authorization Bearer api_key my_token) do
        assert {:error, detail} =
                 GovernedEffect.handle(base(%{"payload" => %{key => "x"}})),
               "expected #{key} to be rejected"

        assert detail =~ "Invariant 7"
      end
    end

    test "a clean payload is accepted" do
      assert {:ok, _} =
               GovernedEffect.handle(
                 base(%{"payload" => %{"sha256" => "abc", "summary" => "edit"}})
               )
    end
  end

  describe "execute mode is gated" do
    test "execute → execute_not_implemented" do
      assert {:error, :execute_not_implemented} =
               GovernedEffect.handle(base(%{"custody_mode" => "execute"}))
    end
  end

  describe "record_only result" do
    test "no leases → recorded, uuid effect_id, empty observations, no pending custody" do
      assert {:ok, body} = GovernedEffect.handle(base())
      assert body.custody_mode == "record_only"
      assert body.status == "recorded"
      assert body.effect_lane == "governed_effect"
      assert body.observations == []
      assert is_nil(body.custody_expires_at)
      assert body.effect_id =~ ~r/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
    end

    test "effect_id is unique per proposal" do
      {:ok, a} = GovernedEffect.handle(base())
      {:ok, b} = GovernedEffect.handle(base())
      refute a.effect_id == b.effect_id
    end
  end

  describe "idempotency_digest" do
    test "deterministic for identical digest fields" do
      env = %{
        effect_type: "file_write",
        surface: "s",
        custody_mode: "record_only",
        payload: %{"a" => 1}
      }

      assert GovernedEffect.idempotency_digest(env) == GovernedEffect.idempotency_digest(env)
    end

    test "excludes provenance/proposer — extra keys do not change the digest" do
      env = %{effect_type: "file_write", surface: "s", custody_mode: "record_only", payload: %{}}

      with_prov =
        Map.merge(env, %{provenance: %{"session_id" => "z"}, proposer: %{"agent_uuid" => "u"}})

      assert GovernedEffect.idempotency_digest(env) ==
               GovernedEffect.idempotency_digest(with_prov)
    end

    test "differs when payload differs" do
      a = %{
        effect_type: "file_write",
        surface: "s",
        custody_mode: "record_only",
        payload: %{"a" => 1}
      }

      b = %{a | payload: %{"a" => 2}}
      refute GovernedEffect.idempotency_digest(a) == GovernedEffect.idempotency_digest(b)
    end

    test "differs when effect_type or surface differs" do
      a = %{effect_type: "file_write", surface: "s", custody_mode: "record_only", payload: %{}}

      refute GovernedEffect.idempotency_digest(a) ==
               GovernedEffect.idempotency_digest(%{a | effect_type: "repo_commit"})

      refute GovernedEffect.idempotency_digest(a) ==
               GovernedEffect.idempotency_digest(%{a | surface: "s2"})
    end
  end
end
