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
  default_lease_ttl_s: 300
