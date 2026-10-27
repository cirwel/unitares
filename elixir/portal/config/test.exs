import Config

# We don't run a server during test. If one is required,
# you can enable the server option below.
config :portal, PortalWeb.Endpoint,
  http: [ip: {127, 0, 0, 1}, port: 4002],
  url: [host: "127.0.0.1", port: 4002, scheme: "http"],
  check_origin: ["http://127.0.0.1:4002"],
  secret_key_base: "SOaKrlEweJ4cPnUy4retjNwROCVjp1eciPpOJyeuTKLVbAejK02pJT+NI/HILmx/",
  server: false

# Print only warnings and errors during test
config :logger, level: :warning

# Initialize plugs at runtime for faster test compilation
config :phoenix, :plug_init_mode, :runtime

# Enable helpful, but potentially expensive runtime checks
config :phoenix_live_view,
  enable_expensive_runtime_checks: true

# Sort query params output of verified routes for robust url comparisons
config :phoenix,
  sort_verified_routes_query_params: true

# Tests reach the endpoint as 127.0.0.1:4002.
config :portal, :boundary, host: "127.0.0.1", port: 4002

# No long-lived governance clients in tests: they start their own instances.
config :portal, :timeline, start_timeline_feed: false, start_firehose: false
