defmodule UnitaresLeasePlane.Application do
  @moduledoc false

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
    parsed = parse_url(url)

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

  defp parse_url("postgresql://" <> rest) do
    [creds_host, db] = String.split(rest, "/", parts: 2)
    [creds, host_port] = String.split(creds_host, "@", parts: 2)
    [user, pass] = String.split(creds, ":", parts: 2)

    {host, port} =
      case String.split(host_port, ":", parts: 2) do
        [h, p] -> {h, String.to_integer(p)}
        [h] -> {h, 5432}
      end

    %{username: user, password: pass, host: host, port: port, database: db}
  end
end
