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

  test "configure_lease_client_from_env records ordered unique governance tokens preferring UNITARES_HTTP_API_TOKEN" do
    saved_http = System.get_env("UNITARES_HTTP_API_TOKEN")
    saved_mcp = System.get_env("UNITARES_MCP_BEARER_TOKEN")
    saved_app = Elixir.Application.fetch_env(:agent_orchestrator, :governance_api_token)
    saved_app_list = Elixir.Application.fetch_env(:agent_orchestrator, :governance_api_tokens)

    on_exit(fn ->
      if saved_http,
        do: System.put_env("UNITARES_HTTP_API_TOKEN", saved_http),
        else: System.delete_env("UNITARES_HTTP_API_TOKEN")

      if saved_mcp,
        do: System.put_env("UNITARES_MCP_BEARER_TOKEN", saved_mcp),
        else: System.delete_env("UNITARES_MCP_BEARER_TOKEN")

      case saved_app do
        {:ok, value} ->
          Elixir.Application.put_env(:agent_orchestrator, :governance_api_token, value)

        :error ->
          Elixir.Application.delete_env(:agent_orchestrator, :governance_api_token)
      end

      case saved_app_list do
        {:ok, value} ->
          Elixir.Application.put_env(:agent_orchestrator, :governance_api_tokens, value)

        :error ->
          Elixir.Application.delete_env(:agent_orchestrator, :governance_api_tokens)
      end
    end)

    System.put_env("UNITARES_HTTP_API_TOKEN", "http-api-tok")
    System.put_env("UNITARES_MCP_BEARER_TOKEN", "mcp-bearer-tok")

    assert :ok = Application.configure_lease_client_from_env()

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_token) ==
             "http-api-tok"

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_tokens) == [
             "http-api-tok",
             "mcp-bearer-tok"
           ]

    System.put_env("UNITARES_HTTP_API_TOKEN", "same-tok")
    System.put_env("UNITARES_MCP_BEARER_TOKEN", "same-tok")
    assert :ok = Application.configure_lease_client_from_env()
    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_tokens) == ["same-tok"]

    System.put_env("UNITARES_HTTP_API_TOKEN", "   ")
    System.put_env("UNITARES_MCP_BEARER_TOKEN", "mcp-bearer-tok")
    assert :ok = Application.configure_lease_client_from_env()

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_token) ==
             "mcp-bearer-tok"

    assert Elixir.Application.get_env(:agent_orchestrator, :governance_api_tokens) == [
             "mcp-bearer-tok"
           ]
  end
end
