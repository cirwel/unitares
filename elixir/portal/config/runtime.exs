import Config

if System.get_env("PHX_SERVER") do
  config :portal, PortalWeb.Endpoint, server: true
end

# The hub answers on one Host and port, on loopback only (portal-hub-v0 §3.2).
# There is deliberately no setting that widens the bind: the application
# refuses to boot on a non-loopback address (Portal.Boundary.check_bind!/1).
if config_env() != :test do
  host = System.get_env("PORTAL_HOST", "127.0.0.1")

  port =
    case Integer.parse(System.get_env("PORTAL_PORT", "8791")) do
      {value, ""} when value in 1..65_535 -> value
      _ -> raise "PORTAL_PORT must be a port number"
    end

  config :portal, :boundary, host: host, port: port

  config :portal, PortalWeb.Endpoint,
    url: [host: host, port: port, scheme: "http"],
    http: [ip: {127, 0, 0, 1}, port: port],
    check_origin: ["http://#{host}:#{port}"]
end

if config_env() == :prod do
  secret_key_base =
    System.get_env("PORTAL_SECRET_KEY_BASE") ||
      raise """
      environment variable PORTAL_SECRET_KEY_BASE is missing.
      You can generate one by calling: mix phx.gen.secret
      """

  config :portal, PortalWeb.Endpoint, secret_key_base: secret_key_base
end
