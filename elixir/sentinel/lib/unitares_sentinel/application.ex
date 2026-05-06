defmodule UnitaresSentinel.Application do
  @moduledoc """
  OTP entry point for the Sentinel app.

  The supervisor starts with no children in :test mode (`config/test.exs`).
  Runtime mode starts Postgrex, the Finch HTTP client pool used by Surface 2
  findings emission, the in-memory fleet state process, the `/ws/eisv`
  consumer when `:start_websocket` is enabled, and the poller when
  `:start_poller` is enabled.

  Mirrors the `:start_application` gate that `UnitaresLeasePlane.Application`
  uses (`elixir/lease_plane/lib/unitares_lease_plane/application.ex`) so
  ExUnit can boot the OTP app without a full child tree.
  """

  use Application

  @impl true
  def start(_type, _args) do
    if Application.get_env(:unitares_sentinel, :start_application, true) do
      start_full()
    else
      Supervisor.start_link([], strategy: :one_for_one, name: UnitaresSentinel.Supervisor)
    end
  end

  defp start_full do
    children =
      postgrex_children() ++
        finch_children() ++ fleet_state_children() ++ websocket_children() ++ poller_children()

    Supervisor.start_link(children, strategy: :one_for_one, name: UnitaresSentinel.Supervisor)
  end

  defp postgrex_children do
    if Application.get_env(:unitares_sentinel, :start_postgrex, true) do
      [{Postgrex, postgrex_opts()}]
    else
      []
    end
  end

  defp poller_children do
    if Application.get_env(:unitares_sentinel, :start_poller, false) do
      [UnitaresSentinel.ForcedReleasePoller]
    else
      []
    end
  end

  defp finch_children do
    if Application.get_env(:unitares_sentinel, :start_finch, true) do
      [{Finch, name: UnitaresSentinel.Finch}]
    else
      []
    end
  end

  defp fleet_state_children do
    if Application.get_env(:unitares_sentinel, :start_fleet_state, true) do
      [UnitaresSentinel.FleetState]
    else
      []
    end
  end

  defp websocket_children do
    if Application.get_env(:unitares_sentinel, :start_websocket, false) do
      [
        {UnitaresSentinel.EISVWebSocket,
         url: Application.get_env(:unitares_sentinel, :websocket_url),
         reconnect_ms: Application.get_env(:unitares_sentinel, :websocket_reconnect_ms, 10_000),
         fleet_state:
           Application.get_env(:unitares_sentinel, :fleet_state_name, UnitaresSentinel.FleetState)}
      ]
    else
      []
    end
  end

  @doc false
  # Public for test_helper bootstrap; not part of the runtime API.
  def postgrex_opts do
    url =
      Application.get_env(:unitares_sentinel, :database_url) ||
        raise "UNITARES_SENTINEL_DATABASE_URL or :unitares_sentinel database_url config required"

    pool_size = Application.get_env(:unitares_sentinel, :pool_size, 2)
    parsed = parse_database_url(url)

    [
      hostname: parsed.host,
      port: parsed.port,
      username: parsed.username,
      password: parsed.password,
      database: parsed.database,
      pool_size: pool_size,
      name: UnitaresSentinel.DB
    ]
  end

  @doc false
  # URI-parse a libpq-style URL into a Postgrex opts map. Mirrors the
  # `UnitaresLeasePlane.Application.parse_database_url/1` helper —
  # duplicated rather than shared because the two apps are independent
  # deployables. A third caller would warrant a shared lib.
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
