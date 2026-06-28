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

    # RFC §7.10 — separate elevated bearer for force-release (operator-only).
    # Distinct config key + env var so the regular bearer can't authorize
    # force-release at the contract layer (HTTPAuth picks the right token by
    # path). Same fail-closed posture: if absent, force-release endpoint
    # returns 503.
    if token = System.get_env("LEASE_FORCE_RELEASE_TOKEN") do
      Application.put_env(:lease_plane, :force_release_token, token)
    end

    # Governed-effect `agent_spawn` execute (routes to the live orchestrator).
    # FAIL-CLOSED on every axis: the per-type flag defaults off, and the spawn
    # path additionally requires the orchestrator bearer to be present. With the
    # flag unset, `execute` stays `execute_not_implemented` exactly as before.
    Application.put_env(
      :lease_plane,
      :execute_agent_spawn_enabled,
      System.get_env("UNITARES_GOVERNED_EFFECT_EXECUTE_AGENT_SPAWN") == "1"
    )

    # file_write execute (first reversible surface). TWO flags, both fail-closed:
    # _ENABLED gates the dispatch; _COMMIT gates the real write (dry-run when off).
    Application.put_env(
      :lease_plane,
      :execute_file_write_enabled,
      System.get_env("UNITARES_GOVERNED_EFFECT_EXECUTE_FILE_WRITE") == "1"
    )

    Application.put_env(
      :lease_plane,
      :execute_file_write_commit_enabled,
      System.get_env("UNITARES_GOVERNED_EFFECT_EXECUTE_FILE_WRITE_COMMIT") == "1"
    )

    validate_execute_type_flags!()

    Application.put_env(
      :lease_plane,
      :agent_orchestrator_url,
      System.get_env("AGENT_ORCHESTRATOR_URL") || "http://127.0.0.1:8789"
    )

    if token = System.get_env("AGENT_ORCHESTRATOR_BEARER_TOKEN") do
      Application.put_env(:lease_plane, :agent_orchestrator_bearer_token, token)
    end

    # Governance MCP base URL for the §6 effect-veto (governed-effect execute).
    # Defaults to the local governance MCP REST surface; loopback bypasses its
    # auth, so no token is needed in the default single-host setup.
    Application.put_env(
      :lease_plane,
      :governance_url,
      System.get_env("UNITARES_GOVERNANCE_URL") ||
        System.get_env("GOVERNANCE_URL") || "http://127.0.0.1:8767"
    )

    if token = System.get_env("UNITARES_HTTP_API_TOKEN") do
      Application.put_env(:lease_plane, :governance_api_token, token)
    end

    # dialectic-on-BEAM Slice 2: per-session liveness timers. The flag gates only
    # whether a stuck-timeout *acts* (fails the session); the watcher processes
    # and presence run regardless and are always safe.
    Application.put_env(
      :lease_plane,
      :dialectic_beam_liveness,
      System.get_env("UNITARES_DIALECTIC_BEAM_LIVENESS") == "1"
    )

    children =
      [
        {Postgrex, postgrex_opts()},
        {Registry, keys: :unique, name: UnitaresLeasePlane.HolderRegistry},
        UnitaresLeasePlane.LeaseSupervisor,
        UnitaresLeasePlane.HandoffServer,
        UnitaresLeasePlane.SurfaceRegistry,
        {Registry, keys: :unique, name: UnitaresLeasePlane.DialecticLivenessRegistry},
        UnitaresLeasePlane.DialecticLivenessSupervisor,
        # Governed-effect EXECUTE crash recovery (§5b). Runs its orphan scan in
        # init/1 synchronously — placed AFTER Postgrex and BEFORE the HTTP
        # listener so no new effect is accepted while a prior crash's orphans are
        # unresolved. Fail-soft: a missing effects.* schema (pre-migration-052) or
        # a DB error is logged and skipped, never crashes boot.
        UnitaresLeasePlane.EffectRecovery
      ] ++ worker_children() ++ http_children()

    opts = [strategy: :one_for_one, name: UnitaresLeasePlane.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # Fail-closed boot guard for the file_write execute flags: turning ON the real
  # write (_COMMIT) without turning ON the dispatch (_ENABLED) is a misconfig —
  # a write capability with no governed path to reach it. Refuse to boot rather
  # than run in that ambiguous state.
  defp validate_execute_type_flags! do
    enabled = Application.get_env(:lease_plane, :execute_file_write_enabled, false)
    commit = Application.get_env(:lease_plane, :execute_file_write_commit_enabled, false)

    if commit and not enabled do
      raise "lease_plane boot refused: UNITARES_GOVERNED_EFFECT_EXECUTE_FILE_WRITE_COMMIT is on " <>
              "but UNITARES_GOVERNED_EFFECT_EXECUTE_FILE_WRITE (dispatch) is off — enable both or neither."
    end

    :ok
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

  defp worker_children do
    if Application.get_env(:lease_plane, :start_workers, true) do
      [
        {UnitaresLeasePlane.PeriodicWorker,
         id: UnitaresLeasePlane.Reaper,
         name: UnitaresLeasePlane.ReaperScheduler,
         worker: UnitaresLeasePlane.Reaper,
         interval_ms: Application.get_env(:lease_plane, :reaper_interval_ms, 30_000),
         initial_delay_ms: Application.get_env(:lease_plane, :reaper_initial_delay_ms, 1_000)},
        {UnitaresLeasePlane.PeriodicWorker,
         id: UnitaresLeasePlane.HandoffTimeout,
         name: UnitaresLeasePlane.HandoffTimeoutScheduler,
         worker: UnitaresLeasePlane.HandoffTimeout,
         interval_ms: Application.get_env(:lease_plane, :handoff_timeout_interval_ms, 5_000),
         initial_delay_ms:
           Application.get_env(:lease_plane, :handoff_timeout_initial_delay_ms, 1_000)},
        {UnitaresLeasePlane.PeriodicWorker,
         id: UnitaresLeasePlane.AuditOutboxForwarder,
         name: UnitaresLeasePlane.AuditOutboxForwarderScheduler,
         worker: UnitaresLeasePlane.AuditOutboxForwarder,
         interval_ms: Application.get_env(:lease_plane, :audit_outbox_forward_interval_ms, 30_000),
         initial_delay_ms:
           Application.get_env(:lease_plane, :audit_outbox_forward_initial_delay_ms, 2_000)},
        {UnitaresLeasePlane.PeriodicWorker,
         id: UnitaresLeasePlane.DialecticSagaReaper,
         name: UnitaresLeasePlane.DialecticSagaReaperScheduler,
         worker: UnitaresLeasePlane.DialecticSagaReaper,
         interval_ms:
           Application.get_env(:lease_plane, :dialectic_saga_reaper_interval_ms, 60_000),
         initial_delay_ms:
           Application.get_env(:lease_plane, :dialectic_saga_reaper_initial_delay_ms, 5_000)},
        {UnitaresLeasePlane.PeriodicWorker,
         id: UnitaresLeasePlane.DialecticLivenessReconciler,
         name: UnitaresLeasePlane.DialecticLivenessReconcilerScheduler,
         worker: UnitaresLeasePlane.DialecticLivenessReconciler,
         interval_ms:
           Application.get_env(:lease_plane, :dialectic_liveness_reconcile_interval_ms, 30_000),
         initial_delay_ms:
           Application.get_env(
             :lease_plane,
             :dialectic_liveness_reconcile_initial_delay_ms,
             7_000
           )}
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
