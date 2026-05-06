import Config

# Test mode: skip the application supervisor tree. Postgrex is started
# manually by `test/test_helper.exs` (skip-if-unavailable pattern) so
# integration tests run when governance_test is reachable and pure-logic
# tests run regardless.
config :unitares_sentinel,
  start_application: false,
  start_postgrex: false,
  start_poller: false,
  start_finch: false,
  start_fleet_state: false,
  start_websocket: false,
  lease_advisory_enabled: false,
  database_url:
    System.get_env("UNITARES_SENTINEL_DATABASE_URL") ||
      System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance_test"
