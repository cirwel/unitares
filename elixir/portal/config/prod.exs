import Config

# No force_ssl: the hub is plain http on loopback, and the Host allowlist
# (PortalWeb.Plugs.HostAllowlist) refuses every other name.

# Do not print debug messages in production
config :logger, level: :info

# Runtime production configuration, including reading
# of environment variables, is done on config/runtime.exs.
