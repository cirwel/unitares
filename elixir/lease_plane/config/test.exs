import Config

# Default to governance_test, NOT the live governance DB. This suite performs
# real acquires, conflicts and FORCED releases; against the live DB, Sentinel
# polled them as operator force-releases and paged three HIGH alarms on
# 2026-08-26 (dialectic:/test_elixir_http_*). CI sets the env var explicitly.
config :lease_plane,
  database_url:
    System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance_test",
  # pool_size 10 (was 2) — concurrent-acquire tests
  # (lease_acquire_concurrency_test.exs) need genuine in-flight transactions
  # to surface the race window. With pool_size 2 the test could pass on
  # bug-present code under serialization pressure (council CONCERN 1).
  pool_size: 10,
  start_application: false,
  start_workers: false
