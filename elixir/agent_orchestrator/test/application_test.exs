defmodule AgentOrchestrator.ApplicationTest do
  use ExUnit.Case, async: true

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
end
