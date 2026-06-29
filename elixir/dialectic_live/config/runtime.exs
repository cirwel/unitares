import Config

# config/runtime.exs is executed for all environments, including
# during releases. It is executed after compilation and before the
# system starts, so it is typically used to load production configuration
# and secrets from environment variables or elsewhere. Do not define
# any compile-time configuration in here, as it won't be applied.
# The block below contains prod specific runtime configuration.

# ## Using releases
#
# If you use `mix release`, you need to explicitly enable the server
# by passing the PHX_SERVER=true when you start it:
#
#     PHX_SERVER=true bin/dialectic_live start
#
# Alternatively, you can use `mix phx.gen.release` to generate a `bin/server`
# script that automatically sets the env var above.
if System.get_env("PHX_SERVER") do
  config :dialectic_live, DialecticLiveWeb.Endpoint, server: true
end

config :dialectic_live, DialecticLiveWeb.Endpoint,
  http: [port: String.to_integer(System.get_env("PORT", "8790"))]

# Upstream Python governance MCP (:8767). This app is a *consumer*: it subscribes
# to the broadcaster firehose over WebSocket and issues tool-call POSTs for
# dialectic list/get. The bearer token is held server-side here and never reaches
# the browser. Defaults target a local governance-mcp; override via env in prod.
config :dialectic_live, :governance,
  ws_url: System.get_env("GOVERNANCE_WS_URL", "ws://127.0.0.1:8767/ws/eisv"),
  tools_url: System.get_env("GOVERNANCE_TOOLS_URL", "http://127.0.0.1:8767/v1/tools/call"),
  api_token: System.get_env("UNITARES_HTTP_API_TOKEN"),
  # When false (default until the upstream firehose is reachable in this env),
  # the broadcaster WS client supervisor child is omitted so the app still boots.
  start_firehose: System.get_env("GOVERNANCE_START_FIREHOSE", "true") == "true"

if config_env() == :prod do
  # The secret key base is used to sign/encrypt cookies and other secrets.
  # A default value is used in config/dev.exs and config/test.exs but you
  # want to use a different value for prod and you most likely don't want
  # to check this value into version control, so we use an environment
  # variable instead.
  secret_key_base =
    System.get_env("SECRET_KEY_BASE") ||
      raise """
      environment variable SECRET_KEY_BASE is missing.
      You can generate one by calling: mix phx.gen.secret
      """

  host = System.get_env("PHX_HOST") || "localhost"

  config :dialectic_live, :dns_cluster_query, System.get_env("DNS_CLUSTER_QUERY")

  # Match the sibling BEAM apps' trust boundary: loopback-only by default. Exposure
  # to a tunnel/LAN is an explicit opt-in via DIALECTIC_LIVE_BIND_ALL=1, mirroring
  # the governance-mcp UNITARES_BIND_ALL_INTERFACES idiom.
  bind_ip =
    if System.get_env("DIALECTIC_LIVE_BIND_ALL") == "1" do
      {0, 0, 0, 0, 0, 0, 0, 0}
    else
      {127, 0, 0, 1}
    end

  port = String.to_integer(System.get_env("PORT", "8790"))

  config :dialectic_live, DialecticLiveWeb.Endpoint,
    url: [host: host, port: port, scheme: "http"],
    http: [ip: bind_ip, port: port],
    secret_key_base: secret_key_base

  # ## SSL Support
  #
  # To get SSL working, you will need to add the `https` key
  # to your endpoint configuration:
  #
  #     config :dialectic_live, DialecticLiveWeb.Endpoint,
  #       https: [
  #         ...,
  #         port: 443,
  #         cipher_suite: :strong,
  #         keyfile: System.get_env("SOME_APP_SSL_KEY_PATH"),
  #         certfile: System.get_env("SOME_APP_SSL_CERT_PATH")
  #       ]
  #
  # The `cipher_suite` is set to `:strong` to support only the
  # latest and more secure SSL ciphers. This means old browsers
  # and clients may not be supported. You can set it to
  # `:compatible` for wider support.
  #
  # `:keyfile` and `:certfile` expect an absolute path to the key
  # and cert in disk or a relative path inside priv, for example
  # "priv/ssl/server.key". For all supported SSL configuration
  # options, see https://plug.hexdocs.pm/Plug.SSL.html#configure/1
  #
  # We also recommend setting `force_ssl` in your config/prod.exs,
  # ensuring no data is ever sent via http, always redirecting to https:
  #
  #     config :dialectic_live, DialecticLiveWeb.Endpoint,
  #       force_ssl: [hsts: true]
  #
  # Check `Plug.SSL` for all available options in `force_ssl`.
end
