import Config

# Lease plane HTTP boundary. Prefer a local Unix domain socket when configured
# (`UNITARES_LEASE_PLANE_UNIX_SOCKET` or `LEASE_PLANE_UNIX_SOCKET`, or a
# `unix:///` base URL); otherwise fall back to IPv4 127.0.0.1:8788 (Bandit does
# not bind ::1, so the dotted-quad literal is intentional — a `localhost` URL
# would resolve to ::1 first on macOS Sonoma+ and fail).
first_nonempty_env = fn names ->
  Enum.find_value(names, fn name ->
    case System.get_env(name) do
      value when is_binary(value) ->
        trimmed = String.trim(value)
        if trimmed != "", do: trimmed, else: nil

      _ ->
        nil
    end
  end)
end

config :agent_orchestrator,
  lease_plane_base_url: System.get_env("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788"),
  lease_plane_unix_socket_path:
    first_nonempty_env.(["UNITARES_LEASE_PLANE_UNIX_SOCKET", "LEASE_PLANE_UNIX_SOCKET"]),
  # Bearer is read from env at boot. Absent → LeasePlaneClient returns
  # {:error, :no_bearer} and lease-required agents refuse to start (fail closed).
  lease_plane_bearer_token: System.get_env("LEASE_PLANE_BEARER_TOKEN"),
  # Governance REST endpoint for request-bound `lat.v1` attestation exchange
  # (`POST /v1/lease-holder/attest`). Loopback HTTP or HTTPS only unless a host
  # is explicitly allowlisted via UNITARES_LEASE_INSECURE_GOVERNANCE_HOSTS.
  governance_url:
    first_nonempty_env.(["UNITARES_GOVERNANCE_URL", "GOVERNANCE_URL"]) ||
      "http://127.0.0.1:8767",
  governance_api_token:
    first_nonempty_env.(["UNITARES_MCP_BEARER_TOKEN", "UNITARES_HTTP_API_TOKEN"]),
  # Optional standing identity proof (continuity token) exchanged per lease
  # mutation for a short-lived request-bound `lat.v1` attestation, plus optional
  # matching default holder UUID for presence leases.
  lease_identity_proof:
    first_nonempty_env.([
      "AGENT_ORCHESTRATOR_LEASE_IDENTITY_PROOF",
      "UNITARES_LEASE_IDENTITY_PROOF"
    ]),
  lease_holder_agent_uuid: first_nonempty_env.(["AGENT_ORCHESTRATOR_LEASE_HOLDER_AGENT_UUID"]),
  # Fail-closed gate: when enabled (`1`/`true`/`yes`/`on`), LeasePlaneClient
  # refuses lease mutations unless a request-bound `lat.v1` attestation is
  # minted and attached in `x-unitares-identity-proof`.
  require_lease_identity_proof:
    System.get_env("AGENT_ORCHESTRATOR_REQUIRE_LEASE_IDENTITY_PROOF", "")
    |> String.trim()
    |> String.downcase()
    |> Kernel.in(["1", "true", "yes", "on"]),
  insecure_governance_hosts:
    System.get_env("UNITARES_LEASE_INSECURE_GOVERNANCE_HOSTS", "")
    |> String.split(",", trim: true)
    |> Enum.map(&(&1 |> String.trim() |> String.downcase()))
    |> Enum.reject(&(&1 == ""))
    |> MapSet.new(),
  # Default TTL for an ephemeral agent's remote_heartbeat lease. The lease is a
  # pure DB TTL row reaped by the lease plane's reaper, so a crashed orchestrator
  # self-heals within one TTL rather than leaking the surface forever.
  default_lease_ttl_s: 300,
  # Wall-clock ceiling on a single agent's lifetime (30 min). No caller is
  # obligated to DELETE a wedged agent, so this is the backstop that keeps a
  # never-exiting child (and its subprocess tree) from leaking until restart.
  # Generous — only fires on a genuinely stuck agent; per-spawn overridable via
  # the spec's max_runtime_ms (nil/<=0 disables).
  default_max_runtime_ms: 1_800_000,
  # AgentOrchestrator.ResultStore retention (closes the await-vs-fast-exit race,
  # #581). A finished runner retains its final result for this long so a late
  # await/snapshot survives process death; the sweep evicts expired rows and the
  # max caps the table under a churn burst within one TTL window.
  result_retention_ms: 300_000,
  result_sweep_interval_ms: 60_000,
  result_store_max: 10_000,
  # Keyed direct spawns use the shared Postgres database in runtime and an
  # isolated in-memory ledger under MIX_ENV=test. Production never silently
  # falls back to memory: if Postgres is absent, keyed spawns fail closed while
  # unkeyed spawns keep their existing behavior. Raw keys/specs/output are not
  # persisted — only hashes, execution ids, state, and timestamps.
  idempotency_database_url:
    System.get_env("AGENT_ORCHESTRATOR_DATABASE_URL") ||
      System.get_env("GOVERNANCE_DATABASE_URL") || System.get_env("DB_POSTGRES_URL"),
  idempotency_ledger:
    if(config_env() == :test,
      do: AgentOrchestrator.MemoryIdempotencyLedger,
      else: AgentOrchestrator.PostgresIdempotencyLedger
    ),
  idempotency_pool_size: 2,
  # Keep retry identity for at least 24 hours. SpawnGate extends an individual
  # row when a caller requests a longer runtime, covering that runtime plus the
  # five-minute result window so the protected execution cannot outlive its key.
  # Expired rows are swept by SpawnGate.
  spawn_idempotency_retention_ms: 86_400_000,
  spawn_idempotency_sweep_interval_ms: 60_000,
  # Test-memory backend only; Postgres is bounded by expiry + the indexed sweep.
  spawn_idempotency_max: 10_000,
  # Control surface (lib/agent_orchestrator/http_router.ex). Binds IPv4
  # 127.0.0.1 only — a single localhost trust boundary, matching the lease
  # plane (Bandit does not bind ::1, so the dotted-quad is intentional). The
  # bearer is read from AGENT_ORCHESTRATOR_BEARER_TOKEN at boot (see
  # application.ex); absent → HTTPAuth returns 503 (fail closed, never open).
  # start_http is OFF under :test so the unit suite never binds a socket; the
  # router is exercised in-process via Plug.Test.
  start_http: config_env() != :test,
  http_port: String.to_integer(System.get_env("AGENT_ORCHESTRATOR_HTTP_PORT", "8789")),
  http_ip: {127, 0, 0, 1},
  # Optional executable allowlist for POST /v1/agents (basenames). nil = parity
  # with the in-VM AgentOrchestrator.run/1 (any cmd). Set e.g. ["claude", "sh"]
  # to constrain what the authenticated control surface may spawn.
  cmd_allowlist: nil
