# This file is responsible for configuring your application
# and its dependencies with the aid of the Config module.
#
# This configuration file is loaded before any dependency and
# is restricted to this project.

# General application configuration
import Config

config :portal,
  generators: [timestamp_type: :utc_datetime]

# Configure the endpoint
# The one Host and port the hub answers for (Portal.Boundary). Numeric
# loopback: macOS resolves *.localhost to ::1 first, which a 127.0.0.1 bind
# would not answer. runtime.exs reads PORTAL_HOST and PORTAL_PORT.
config :portal, :boundary, host: "127.0.0.1", port: 8791

config :portal, PortalWeb.Endpoint,
  url: [host: "127.0.0.1", port: 8791, scheme: "http"],
  http: [ip: {127, 0, 0, 1}, port: 8791],
  check_origin: ["http://127.0.0.1:8791"],
  adapter: Bandit.PhoenixAdapter,
  render_errors: [
    formats: [html: PortalWeb.ErrorHTML, json: PortalWeb.ErrorJSON],
    layout: false
  ],
  pubsub_server: Portal.PubSub,
  live_view: [signing_salt: "gSjOQAMg"]

# Configure LiveView
config :phoenix_live_view,
  # the attribute set on all root tags. Used for Phoenix.LiveView.ColocatedCSS.
  root_tag_attribute: "phx-r"

# Configure Elixir's Logger
config :logger, :default_formatter,
  format: "$time $metadata[$level] $message\n",
  metadata: [:request_id]

# Use Jason for JSON parsing in Phoenix
config :phoenix, :json_library, Jason

# Import environment specific config. This must remain at the bottom
# of this file so it overrides the configuration defined above.
# The hub's stylesheet, built by `mix assets.deploy` (the version is required).
config :tailwind,
  version: "4.3.0",
  portal: [
    args: ~w(
      --input=assets/css/hub.css
      --output=priv/static/assets/css/hub.css
    ),
    cd: Path.expand("..", __DIR__)
  ]

import_config "#{config_env()}.exs"
