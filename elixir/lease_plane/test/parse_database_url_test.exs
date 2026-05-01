defmodule UnitaresLeasePlane.ParseDatabaseUrlTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.Application, as: App

  describe "parse_database_url/1" do
    test "parses a vanilla libpq URL" do
      assert %{
               username: "postgres",
               password: "postgres",
               host: "localhost",
               port: 5432,
               database: "governance"
             } =
               App.parse_database_url("postgresql://postgres:postgres@localhost:5432/governance")
    end

    test "accepts the postgres:// scheme alias" do
      assert %{username: "u", database: "g"} =
               App.parse_database_url("postgres://u:p@localhost:5432/g")
    end

    test "defaults to port 5432 when omitted" do
      assert %{port: 5432} =
               App.parse_database_url("postgresql://u:p@localhost/g")
    end

    test "URI-decodes percent-encoded password (council finding from #253)" do
      # Real-world: a password like 'p@ss/word' must be encoded as
      # 'p%40ss%2Fword' in the URL, then decoded back into Postgrex.
      assert %{username: "u", password: "p@ss/word"} =
               App.parse_database_url("postgresql://u:p%40ss%2Fword@localhost:5432/g")
    end

    test "URI-decodes percent-encoded username" do
      assert %{username: "weird user"} =
               App.parse_database_url("postgresql://weird%20user:p@localhost:5432/g")
    end

    test "raises on missing userinfo" do
      assert_raise ArgumentError, ~r/user:password@host/, fn ->
        App.parse_database_url("postgresql://localhost:5432/g")
      end
    end

    test "raises on missing host" do
      assert_raise ArgumentError, ~r/missing host/, fn ->
        App.parse_database_url("postgresql://u:p@/g")
      end
    end

    test "raises on missing database name" do
      assert_raise ArgumentError, ~r/missing database name/, fn ->
        App.parse_database_url("postgresql://u:p@localhost:5432/")
      end
    end

    test "raises on unknown scheme" do
      assert_raise FunctionClauseError, fn ->
        App.parse_database_url("mysql://u:p@localhost/g")
      end
    end
  end
end
