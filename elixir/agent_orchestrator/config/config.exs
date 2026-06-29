import Config

# Lease plane HTTP boundary. The lease plane binds IPv4 127.0.0.1:8788 only
# (Bandit does not bind ::1), so the dotted-quad literal is intentional — a
# `localhost` URL would resolve to ::1 first on macOS Sonoma+ and fail.
config :agent_orchestrator,
  lease_plane_base_url: System.get_env("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788"),
  # Bearer is read from env at boot. Absent → LeasePlaneClient returns
  # {:error, :no_bearer} and lease-required agents refuse to start (fail closed).
  lease_plane_bearer_token: System.get_env("LEASE_PLANE_BEARER_TOKEN"),
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
