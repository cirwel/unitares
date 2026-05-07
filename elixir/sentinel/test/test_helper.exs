# Try to start Postgrex against the test DB. If the DB is unreachable,
# integration tests tagged `:db` are excluded so the suite stays green
# for environments without local Postgres. Pure-logic tests run regardless.

db_available? =
  try do
    opts = UnitaresSentinel.Application.postgrex_opts()

    case Postgrex.start_link(Keyword.put(opts, :pool_size, 2)) do
      {:ok, _pid} ->
        case Postgrex.query(UnitaresSentinel.DB, "SELECT 1", []) do
          {:ok, _} -> true
          _ -> false
        end

      _ ->
        false
    end
  rescue
    _ -> false
  end

if db_available? do
  ExUnit.start()
else
  IO.puts(
    :stderr,
    "[test_helper] governance_test DB unreachable — excluding :db-tagged tests"
  )

  ExUnit.start(exclude: [:db])
end
