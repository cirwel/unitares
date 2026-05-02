import Config

config :lease_plane,
  database_url:
    System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance",
  # pool_size 10 (was 2) — concurrent-acquire tests
  # (lease_acquire_concurrency_test.exs) need genuine in-flight transactions
  # to surface the race window. With pool_size 2 the test could pass on
  # bug-present code under serialization pressure (council CONCERN 1).
  pool_size: 10,
  start_application: false,
  start_workers: false
