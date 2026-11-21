defmodule AgentOrchestrator.ApplicationTest do
  use ExUnit.Case, async: false

  alias AgentOrchestrator.Application

  test "parses PostgreSQL URLs into driver options" do
    assert %{
             username: "agent user",
             password: "p@ss/word",
             host: "db.example.test",
             port: 5544,
             database: "governance"
           } =
             Application.parse_database_url(
               "postgresql://agent%20user:p%40ss%2Fword@db.example.test:5544/governance"
             )
  end

  test "accepts the postgres scheme and an empty password" do
    assert %{
             username: "postgres",
             password: "",
             host: "localhost",
             port: 5432,
             database: "governance"
           } = Application.parse_database_url("postgres://postgres@localhost/governance")
  end

  test "rejects URLs without a database name" do
    assert_raise ArgumentError, ~r/missing database name/, fn ->
      Application.parse_database_url("postgresql://postgres@localhost/")
    end
  end

  test "parses boolean env flags case-insensitively" do
    for truthy <- ["1", "true", "TRUE", " yes ", "On"] do
      assert Application.parse_bool_env(truthy)
    end

    for falsy <- [nil, "", "0", "false", "no", "off", "maybe"] do
      refute Application.parse_bool_env(falsy)
    end
  end

  @lease_env_keys [
    :lease_plane_base_url,
    :lease_plane_unix_socket_path,
    :lease_plane_bearer_token,
    :governance_url,
    :governance_api_token,
    :governance_mcp_token,
    :governance_api_tokens,
    :governance_rest_strict,
    :lease_identity_proof,
    :lease_holder_agent_uuid,
    :require_lease_identity_proof,
    :insecure_governance_hosts
  ]

  test "configure_lease_client_from_env stores governance API/MCP tokens and strict flag separately" do
    saved_http = System.get_env("UNITARES_HTTP_API_TOKEN")
    saved_mcp = System.get_env("UNITARES_MCP_BEARER_TOKEN")
    saved_strict = System.get_env("UNITARES_REST_STRICT")

    saved_app_env =
      Enum.map(@lease_env_keys, fn key ->
        {key, Elixir.Application.get_env(:agent_orchestrator, key, :__unset__)}
      end)

    on_exit(fn ->
      for {name, saved} <- [
            {"UNITARES_HTTP_API_TOKEN", saved_http},
            {"UNITARES_MCP_BEARER_TOKEN", saved_mcp},
            {"UNITARES_REST_STRICT", saved_strict}
          ] do
        if saved, do: System.put_env(name, saved), else: System.delete_env(name)
      end

      Enum.each(saved_app_env, fn
        {key, :__unset__} ->
          Elixir.Application.delete_env(:agent_orchestrator, key)

        {key, value} ->
          Elixir.Application.put_env(:agent_orchestrator, key, value)
      end)
    end)

    Elixir.Application.delete_env(:agent_orchestrator, :governance_api_token)
    Elixir.Application.delete_env(:agent_orchestrator, :governance_mcp_token)
    Elixir.Application.delete_env(:agent_orchestrator, :governance_api_tokens)
    Elixir.Application.delete_env(:agent_orchestrator, :governance_rest_strict)

    System.put_env("UNITARES_HTTP_API_TOKEN", "http-api-tok")
    System.put_env("UNITARES_MCP_BEARER_TOKEN", "mcp-bearer-tok")
    System.delete_env("UNITARES_REST_STRICT")

    assert :ok = Application.configure_lease_client_from_env()

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_token) ==
             "http-api-tok"

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_mcp_token) ==
             "mcp-bearer-tok"

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_rest_strict) == nil

    System.put_env("UNITARES_REST_STRICT", "1")
    assert :ok = Application.configure_lease_client_from_env()
    assert Elixir.Application.get_env(:agent_orchestrator, :governance_rest_strict) == true

    System.put_env("UNITARES_REST_STRICT", "0")
    assert :ok = Application.configure_lease_client_from_env()
    assert Elixir.Application.get_env(:agent_orchestrator, :governance_rest_strict) == false

    Elixir.Application.delete_env(:agent_orchestrator, :governance_api_token)
    Elixir.Application.delete_env(:agent_orchestrator, :governance_mcp_token)
    Elixir.Application.delete_env(:agent_orchestrator, :governance_rest_strict)

    System.put_env("UNITARES_HTTP_API_TOKEN", "   ")
    System.put_env("UNITARES_MCP_BEARER_TOKEN", "mcp-bearer-tok")
    System.put_env("UNITARES_REST_STRICT", "   ")
    assert :ok = Application.configure_lease_client_from_env()

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_token) == nil

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_mcp_token) ==
             "mcp-bearer-tok"

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_rest_strict) == nil
  end
end
