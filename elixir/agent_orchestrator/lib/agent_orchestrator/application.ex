defmodule AgentOrchestrator.Application do
  @moduledoc """
  OTP entry point for the ephemeral-agent orchestrator.

  This is the BEAM-native fleet runtime axis of the BEAM footprint: it is NOT
  the governance-server migration (Wave 1-3, `beam-footprint-roadmap-v0.md`) and
  it is NOT the lease plane itself. It supervises *ephemeral agents* — short-lived
  external runtimes (a Claude SDK process, `claude -p`, a tool worker) — as
  OTP children, one `AgentRunner` GenServer per agent, each wrapping a `Port`.

  Topology:

      AgentOrchestrator.Supervisor            (one_for_one)
      ├── Registry  (AgentOrchestrator.Registry)   execution_id -> runner pid
      ├── ResultStore  (GenServer + ETS)            retained final results
      ├── IdempotencyDB  (Postgrex)                 durable keyed-spawn ledger
      ├── AgentSupervisor  (DynamicSupervisor)
      │   └── AgentRunner  (GenServer + Port)       restart: :temporary
      └── SpawnGate  (GenServer)                    reserve -> spawn -> started

  `ResultStore` starts before `AgentSupervisor` so its ETS table exists before
  any runner can finalize and write its result — closing the await-vs-fast-exit
  race (#581) where a finished agent's result was lost to `:not_found`.

  Lease-binding to the plane is the architectural-coherence payoff: an agent
  registers an `agent:/<id>` presence row when it spawns and releases it on
  exit, so the fleet plane has a single source of truth for "which ephemeral
  agents are live." The presence row uses the `remote_heartbeat` holder kind,
  which migration 042 (PR #588) routes to the self-healing TTL-row path — so an
  orphan left by a crash that skips `terminate/2` reaps itself at TTL rather
  than leaking indefinitely. Explicit release on exit remains the fast path; the
  TTL is the backstop, not the normal path. (Earlier drafts claimed self-heal
  for every scheme — that was false before #588, when only `file://` got the
  TTL-row path and other schemes were coerced to an auto-renewing holder. See
  `AgentRunner`'s moduledoc for the full corrected story.)
  """

  use Application

  require Logger

  @impl true
  def start(_type, _args) do
    # First, so the cookie variable is gone before any child can be spawned.
    AgentOrchestrator.NodeDistribution.start_from_env()

    # Bearer for the control surface, sourced from env at boot. Absent → HTTPAuth
    # returns 503 on every request (fail closed, never silently open). Mirrors
    # the lease plane's posture; matters more here because POST /v1/agents spawns
    # an OS process — an unauthenticated reach is RCE.
    if token = System.get_env("AGENT_ORCHESTRATOR_BEARER_TOKEN") do
      Application.put_env(:agent_orchestrator, :bearer_token, token)
    end

    configure_lease_client_from_env()

    children =
      [
        {Registry, keys: :unique, name: AgentOrchestrator.Registry},
        AgentOrchestrator.ResultStore,
        AgentOrchestrator.Metrics,
        # Before the HTTP surface: it reads the profile set and raw-spawn mode.
        AgentOrchestrator.Profiles
      ] ++
        idempotency_children() ++
        [
          AgentOrchestrator.AgentSupervisor,
          AgentOrchestrator.SpawnGate
        ] ++ http_children()

    opts = [strategy: :one_for_one, name: AgentOrchestrator.Supervisor]
    Supervisor.start_link(children, opts)
  end

  defp idempotency_children do
    ledger =
      Application.get_env(
        :agent_orchestrator,
        :idempotency_ledger,
        AgentOrchestrator.PostgresIdempotencyLedger
      )

    case ledger do
      AgentOrchestrator.MemoryIdempotencyLedger ->
        [AgentOrchestrator.MemoryIdempotencyLedger]

      AgentOrchestrator.PostgresIdempotencyLedger ->
        postgrex_children()

      _custom_ledger ->
        []
    end
  end

  defp postgrex_children do
    case Application.get_env(:agent_orchestrator, :idempotency_database_url) do
      url when is_binary(url) and url != "" ->
        pool_size = Application.get_env(:agent_orchestrator, :idempotency_pool_size, 2)
        parsed = parse_database_url(url)

        [
          {Postgrex,
           hostname: parsed.host,
           port: parsed.port,
           username: parsed.username,
           password: parsed.password,
           database: parsed.database,
           pool_size: pool_size,
           name: AgentOrchestrator.IdempotencyDB}
        ]

      _missing ->
        Logger.warning(
          "agent orchestrator durable idempotency database is not configured; " <>
            "keyed spawns will fail closed"
        )

        []
    end
  end

  @doc false
  def parse_database_url("postgresql://" <> _ = url), do: parse_database_url(URI.parse(url))
  def parse_database_url("postgres://" <> _ = url), do: parse_database_url(URI.parse(url))

  def parse_database_url(%URI{scheme: scheme} = uri) when scheme in ["postgresql", "postgres"] do
    {username, password} =
      case uri.userinfo do
        nil ->
          raise ArgumentError, "idempotency database URL must include a username"

        userinfo ->
          case String.split(userinfo, ":", parts: 2) do
            [user, pass] -> {URI.decode(user), URI.decode(pass)}
            [user] -> {URI.decode(user), ""}
          end
      end

    host = uri.host || raise(ArgumentError, "idempotency database URL missing host")
    port = uri.port || 5432

    database =
      case uri.path do
        "/" <> name when name != "" -> name
        _ -> raise ArgumentError, "idempotency database URL missing database name"
      end

    %{username: username, password: password, host: host, port: port, database: database}
  end

  # Localhost-only Bandit listener for the control surface. Gated by
  # :start_http (OFF under :test so the unit suite binds no socket). IPv4 only —
  # Bandit does not bind ::1, and the localhost trust boundary is the security
  # model, so off-host exposure is intentionally not a built-in option.
  defp http_children do
    if Application.get_env(:agent_orchestrator, :start_http, true) do
      port = Application.get_env(:agent_orchestrator, :http_port, 8789)
      ip = Application.get_env(:agent_orchestrator, :http_ip, {127, 0, 0, 1})

      [{Bandit, plug: AgentOrchestrator.HTTPRouter, ip: ip, port: port}]
    else
      []
    end
  end

  @doc false
  def configure_lease_client_from_env do
    put_env_if_present(:lease_plane_base_url, ["LEASE_PLANE_BASE_URL"])

    put_env_if_present(:lease_plane_unix_socket_path, [
      "UNITARES_LEASE_PLANE_UNIX_SOCKET",
      "LEASE_PLANE_UNIX_SOCKET"
    ])

    put_env_if_present(:lease_plane_bearer_token, ["LEASE_PLANE_BEARER_TOKEN"])
    put_env_if_present(:governance_url, ["UNITARES_GOVERNANCE_URL", "GOVERNANCE_URL"])

    put_env_if_present(:governance_api_token, [
      "UNITARES_MCP_BEARER_TOKEN",
      "UNITARES_HTTP_API_TOKEN"
    ])

    put_env_if_present(:lease_identity_proof, [
      "AGENT_ORCHESTRATOR_LEASE_IDENTITY_PROOF",
      "UNITARES_LEASE_IDENTITY_PROOF"
    ])

    put_env_if_present(:lease_holder_agent_uuid, ["AGENT_ORCHESTRATOR_LEASE_HOLDER_AGENT_UUID"])

    if raw = System.get_env("AGENT_ORCHESTRATOR_REQUIRE_LEASE_IDENTITY_PROOF") do
      Application.put_env(
        :agent_orchestrator,
        :require_lease_identity_proof,
        parse_bool_env(raw)
      )
    end

    if raw = System.get_env("UNITARES_LEASE_INSECURE_GOVERNANCE_HOSTS") do
      hosts =
        raw
        |> String.split(",", trim: true)
        |> Enum.map(&(&1 |> String.trim() |> String.downcase()))
        |> Enum.reject(&(&1 == ""))
        |> MapSet.new()

      Application.put_env(:agent_orchestrator, :insecure_governance_hosts, hosts)
    end

    :ok
  end

  @doc false
  def parse_bool_env(nil), do: false

  def parse_bool_env(value) when is_binary(value) do
    String.downcase(String.trim(value)) in ["1", "true", "yes", "on"]
  end

  defp put_env_if_present(app_key, env_names) do
    case Enum.find_value(env_names, fn name ->
           case System.get_env(name) do
             value when is_binary(value) ->
               trimmed = String.trim(value)
               if trimmed != "", do: trimmed, else: nil

             _ ->
               nil
           end
         end) do
      nil -> :ok
      value -> Application.put_env(:agent_orchestrator, app_key, value)
    end
  end
end
