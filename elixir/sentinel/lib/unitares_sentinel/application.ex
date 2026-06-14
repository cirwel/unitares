defmodule UnitaresSentinel.Application do
  @moduledoc """
  OTP entry point for the Sentinel app.

  The supervisor starts with no children in :test mode (`config/test.exs`).
  Runtime mode starts Postgrex, the Finch HTTP client pool used by Surface 2
  findings/check-in emission, the in-memory fleet state process, the
  `/ws/eisv` consumer when `:start_websocket` is enabled, the fleet finding
  emitter when `:start_fleet_finding_emitter` is enabled, and the poller when
  `:start_poller` is enabled. Governance check-ins remain gated by
  `:emit_checkins` inside the emitter.

  Mirrors the `:start_application` gate that `UnitaresLeasePlane.Application`
  uses (`elixir/lease_plane/lib/unitares_lease_plane/application.ex`) so
  ExUnit can boot the OTP app without a full child tree.
  """

  use Application

  require Logger

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
        finch_children() ++
        fleet_state_children() ++
        websocket_children() ++ fleet_finding_emitter_children() ++ poller_children()

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

  defp fleet_finding_emitter_children do
    if Application.get_env(:unitares_sentinel, :start_fleet_finding_emitter, false) do
      [{UnitaresSentinel.FleetFindingEmitter, fleet_finding_emitter_opts()}]
    else
      []
    end
  end

  @doc false
  # Public for the runtime-boundary regression test.
  def fleet_finding_emitter_opts do
    [
      fleet_state:
        Application.get_env(:unitares_sentinel, :fleet_state_name, UnitaresSentinel.FleetState),
      interval_ms: Application.get_env(:unitares_sentinel, :analysis_interval_ms, 300_000),
      initial_delay_ms:
        Application.get_env(:unitares_sentinel, :analysis_initial_delay_ms, 5_000),
      jitter_ms: Application.get_env(:unitares_sentinel, :analysis_jitter_ms, 5_000),
      tick_timeout_ms: Application.get_env(:unitares_sentinel, :analysis_tick_timeout_ms, 45_000),
      # Distinct surface from ForcedReleasePoller's resident:/sentinel_cycle so
      # the two GenServers don't collide as held_by_other when their tick
      # windows overlap (KG 2026-05-08T02:14:43.822544+00:00).
      lease_opts: [surface_id: "resident:/sentinel_fleet_emit"]
    ]
    |> maybe_add_self_agent_id()
    |> maybe_add_checkin_anchor()
  end

  # Thread the Sentinel's persisted agent_uuid into the fleet-finding emit
  # path so cross-runtime finding fingerprints key on the SAME id the Python
  # Sentinel uses — `compute_fingerprint(["sentinel", type, vclass, agent_uuid])`
  # at agents/sentinel/agent.py:590. Without this the emitter falls back to the
  # "sentinel" literal default and fleet findings would not dedup against the
  # Python Sentinel across the direct-flip cutover gap (2026-06-14 condition-2
  # parity audit, GAP 2). Identity continuity is otherwise wired only into the
  # check-in path, not the finding-emit path. Additive + graceful: any
  # anchor-load failure leaves the existing config/env/default resolution in
  # place, and `put_new` lets an explicit opt override.
  defp maybe_add_self_agent_id(opts) do
    case UnitaresSentinel.SessionAnchor.load() do
      {:ok, %{"agent_uuid" => uuid}} when is_binary(uuid) and uuid != "" ->
        Keyword.put_new(opts, :self_agent_id, uuid)

      _ ->
        opts
    end
  end

  defp maybe_add_checkin_anchor(opts) do
    if Application.get_env(:unitares_sentinel, :emit_checkins, false) do
      Keyword.put(opts, :checkin_opts, sentinel_checkin_opts())
    else
      opts
    end
  end

  defp sentinel_checkin_opts do
    case UnitaresSentinel.SessionAnchor.load() do
      {:ok, anchor} ->
        [anchor: anchor]

      {:error, reason} ->
        Logger.warning(
          "Sentinel governance check-ins enabled but session anchor could not be loaded: #{inspect(reason)}"
        )

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
