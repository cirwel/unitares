defmodule UnitaresLeasePlane.IdentityBindingTest do
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.IdentityBinding

  setup do
    previous_mode = Application.get_env(:lease_plane, :identity_binding_mode)
    previous_verifier = Application.get_env(:lease_plane, :identity_verifier)
    previous_kinds = Application.get_env(:lease_plane, :identity_bound_surface_kinds)

    on_exit(fn ->
      restore_env(:identity_binding_mode, previous_mode)
      restore_env(:identity_verifier, previous_verifier)
      restore_env(:identity_bound_surface_kinds, previous_kinds)
    end)

    :ok
  end

  test "off preserves the compatibility path without consulting governance" do
    Application.put_env(:lease_plane, :identity_binding_mode, :off)
    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> flunk("called") end)

    assert :ok = IdentityBinding.authorize("11111111-1111-4111-8111-111111111111", nil)
  end

  test "enforce accepts only an explicitly verified matching proof" do
    holder = "11111111-1111-4111-8111-111111111111"
    proof = "v1.payload.signature"
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(:lease_plane, :identity_verifier, fn
      ^holder, ^proof -> :ok
      _, _ -> {:error, :invalid}
    end)

    assert :ok = IdentityBinding.authorize(holder, proof)
    assert {:error, :identity_proof_invalid} = IdentityBinding.authorize(holder, nil)

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize(holder, "v1.other.signature")
  end

  test "enforce distinguishes invalid proof from verifier unavailability" do
    holder = "11111111-1111-4111-8111-111111111111"
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> {:error, :invalid} end)
    assert {:error, :identity_proof_invalid} = IdentityBinding.authorize(holder, "bad")

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> {:error, :timeout} end)

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize(holder, "proof")

    Application.put_env(:lease_plane, :identity_verifier, fn _, _ -> raise "boom" end)

    assert {:error, :identity_verification_unavailable} =
             IdentityBinding.authorize(holder, "proof")
  end

  test "mode parser fails closed on a misspelled rollout value" do
    assert IdentityBinding.parse_mode(nil) == :off
    assert IdentityBinding.parse_mode("LOG") == :log
    assert IdentityBinding.parse_mode(" enforce ") == :enforce

    assert_raise ArgumentError, fn -> IdentityBinding.parse_mode("enabled") end
  end

  test "surface-kind rollout can enforce maintenance without blocking agent presence" do
    holder = "11111111-1111-4111-8111-111111111111"
    Application.put_env(:lease_plane, :identity_binding_mode, :enforce)

    Application.put_env(
      :lease_plane,
      :identity_bound_surface_kinds,
      IdentityBinding.parse_surface_kinds("maintenance,file")
    )

    assert IdentityBinding.required_for_surface?("maintenance")
    assert IdentityBinding.required_for_surface?("file")
    refute IdentityBinding.required_for_surface?("agent")

    assert {:error, :identity_proof_invalid} =
             IdentityBinding.authorize(holder, nil, "maintenance")

    assert :ok = IdentityBinding.authorize(holder, nil, "agent")
  end

  defp restore_env(key, nil), do: Application.delete_env(:lease_plane, key)
  defp restore_env(key, value), do: Application.put_env(:lease_plane, key, value)
end
