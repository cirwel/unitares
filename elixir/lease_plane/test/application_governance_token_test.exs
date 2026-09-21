defmodule UnitaresLeasePlane.ApplicationGovernanceTokenTest do
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.Application, as: LeaseApplication

  @singular "UNITARES_MCP_BEARER_TOKEN"
  @legacy "UNITARES_HTTP_API_TOKEN"

  setup do
    previous_singular = System.get_env(@singular)
    previous_legacy = System.get_env(@legacy)
    previous_config = Application.get_env(:lease_plane, :governance_api_token)

    on_exit(fn ->
      restore_system_env(@singular, previous_singular)
      restore_system_env(@legacy, previous_legacy)
      restore_application_env(previous_config)
    end)

    :ok
  end

  test "the singular MCP client token wins when both credentials are present" do
    System.put_env(@singular, "hosted-bearer")
    System.put_env(@legacy, "local-token")

    LeaseApplication.configure_governance_api_token()

    assert Application.get_env(:lease_plane, :governance_api_token) == "hosted-bearer"
  end

  test "a blank singular token falls back to the legacy HTTP token" do
    System.put_env(@singular, "")
    System.put_env(@legacy, "local-token")

    LeaseApplication.configure_governance_api_token()

    assert Application.get_env(:lease_plane, :governance_api_token) == "local-token"
  end

  test "blank credentials clear stale application configuration" do
    Application.put_env(:lease_plane, :governance_api_token, "stale-token")
    System.put_env(@singular, "")
    System.put_env(@legacy, "   ")

    LeaseApplication.configure_governance_api_token()

    assert Application.get_env(:lease_plane, :governance_api_token) == nil
  end

  defp restore_system_env(name, nil), do: System.delete_env(name)
  defp restore_system_env(name, value), do: System.put_env(name, value)

  defp restore_application_env(nil),
    do: Application.delete_env(:lease_plane, :governance_api_token)

  defp restore_application_env(value),
    do: Application.put_env(:lease_plane, :governance_api_token, value)
end
