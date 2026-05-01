import Config

config :lease_plane,
  database_url:
    System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance",
  pool_size: 2,
  start_application: false,
  start_workers: false
