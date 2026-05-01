defmodule UnitaresLeasePlane.Application do
  @moduledoc """
  OTP entry point for the lease plane.

  ## HTTP bind discipline

  Defaults to IPv4 `127.0.0.1:8788`. The Python contract anchor uses the
  same dotted-quad literal (`http://127.0.0.1:8788`) so the round-trip is
  consistent without DNS in the path.

  Note: on macOS Sonoma+ with the default `/etc/hosts`, `localhost`
  resolves to `::1` (IPv6) before `127.0.0.1`. A client that uses
  `http://localhost:8788` instead of the dotted-quad will fail with
  connection refused — Bandit binds the IPv4 socket only.

  Operators who need the IPv6 path can override:

      config :lease_plane, http_ip: {0, 0, 0, 0, 0, 0, 0, 1}

  Or run a second listener on the same port via custom supervision —
  Bandit child specs are independent. Off-host exposure is intentionally
  not a built-in option in v0; the bearer-auth fail-closed posture
  assumes a single trust boundary at `localhost`.

  ## Database URL

  `parse_database_url/1` uses `URI.parse/1` plus `URI.decode/1` on the
  username and password components so percent-encoded credentials
  (e.g., `p%40ss` for `p@ss`) survive the round-trip into Postgrex.
  """

  use Application

  @impl true
  def start(_type, _args) do
    if Application.get_env(:lease_plane, :start_application, true) do
      start_full()
    else
      Supervisor.start_link([], strategy: :one_for_one, name: UnitaresLeasePlane.Supervisor)
    end
  end

  defp start_full do
    # Bearer token must be sourced from env at boot. Fails closed (HTTPAuth
    # returns 503) if absent — never silently open.
    if token = System.get_env("LEASE_PLANE_BEARER_TOKEN") do
      Application.put_env(:lease_plane, :bearer_token, token)
    end

    children =
      [
        {Postgrex, postgrex_opts()},
        {Registry, keys: :unique, name: UnitaresLeasePlane.HolderRegistry},
        UnitaresLeasePlane.LeaseSupervisor
      ] ++ http_children()

    opts = [strategy: :one_for_one, name: UnitaresLeasePlane.Supervisor]
    Supervisor.start_link(children, opts)
  end

  defp http_children do
    if Application.get_env(:lease_plane, :start_http, true) do
      port = Application.get_env(:lease_plane, :http_port, 8788)
      ip = Application.get_env(:lease_plane, :http_ip, {127, 0, 0, 1})

      [
        {Bandit, plug: UnitaresLeasePlane.HTTPRouter, ip: ip, port: port}
      ]
    else
      []
    end
  end

  defp postgrex_opts do
    url =
      Application.get_env(:lease_plane, :database_url) ||
        raise "UNITARES_LEASE_PLANE_DATABASE_URL or :lease_plane database_url config required"

    pool_size = Application.get_env(:lease_plane, :pool_size, 4)
    parsed = parse_database_url(url)

    [
      hostname: parsed.host,
      port: parsed.port,
      username: parsed.username,
      password: parsed.password,
      database: parsed.database,
      pool_size: pool_size,
      name: UnitaresLeasePlane.DB
    ]
  end

  @doc false
  # Public for testing only. Parses a libpq-style URL into a Postgrex opts
  # map, with URI.decode/1 applied to user and password so percent-encoded
  # credentials (e.g., "p%40ss" for "p@ss") survive into the driver.
  def parse_database_url("postgresql://" <> _ = url), do: parse_database_url(URI.parse(url))
  def parse_database_url("postgres://" <> _ = url), do: parse_database_url(URI.parse(url))

  def parse_database_url(%URI{scheme: scheme} = uri) when scheme in ["postgresql", "postgres"] do
    {user, pass} =
      case uri.userinfo do
        nil ->
          raise ArgumentError, "database_url must include user:password@host"

        userinfo ->
          case String.split(userinfo, ":", parts: 2) do
            [u, p] -> {URI.decode(u), URI.decode(p)}
            [u] -> {URI.decode(u), ""}
          end
      end

    host = uri.host || raise(ArgumentError, "database_url missing host")
    port = uri.port || 5432

    database =
      case uri.path do
        "/" <> db when db != "" -> db
        _ -> raise ArgumentError, "database_url missing database name"
      end

    %{username: user, password: pass, host: host, port: port, database: database}
  end
end
