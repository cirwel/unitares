ExUnit.start()

# Tests in this suite hit a real Postgres governance DB. Boot only the pieces
# we need; the Application module is started in :test mode with no children
# (see config/test.exs and Application.start/2's start_application gate).
url =
  System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
    Application.get_env(:lease_plane, :database_url)

unless url do
  raise "test_helper: UNITARES_LEASE_PLANE_DATABASE_URL or :lease_plane database_url required"
end

parsed =
  url
  |> String.replace_prefix("postgresql://", "")
  |> then(fn rest ->
    [creds_host, db] = String.split(rest, "/", parts: 2)
    [creds, host_port] = String.split(creds_host, "@", parts: 2)
    [user, pass] = String.split(creds, ":", parts: 2)

    {host, port} =
      case String.split(host_port, ":", parts: 2) do
        [h, p] -> {h, String.to_integer(p)}
        [h] -> {h, 5432}
      end

    %{username: user, password: pass, host: host, port: port, database: db}
  end)

{:ok, _} =
  Postgrex.start_link(
    name: UnitaresLeasePlane.DB,
    hostname: parsed.host,
    port: parsed.port,
    username: parsed.username,
    password: parsed.password,
    database: parsed.database,
    pool_size: 2
  )

{:ok, _} = Registry.start_link(keys: :unique, name: UnitaresLeasePlane.HolderRegistry)
{:ok, _} = UnitaresLeasePlane.LeaseSupervisor.start_link(:ok)
{:ok, _} = UnitaresLeasePlane.HandoffServer.start_link(:ok)

{:ok, _} =
  Registry.start_link(keys: :unique, name: UnitaresLeasePlane.DialecticLivenessRegistry)

{:ok, _} = UnitaresLeasePlane.DialecticLivenessSupervisor.start_link(:ok)
