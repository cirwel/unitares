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

  # Sign-in borrows the dashboard's passkey (portal-hub-v0 H2c). All three are
  # needed; without them the hub refuses everyone (Portal.Auth.Unconfigured).
  #   PORTAL_GOV_ORIGIN  the dashboard's origin, where the passkeys live
  #   PORTAL_GOV_SOCKET  the server's owner-only socket (UNITARES_UDS_SOCKET)
  #   PORTAL_HUB_TOKEN   the hub credential (the server's UNITARES_HUB_TOKEN)
  gov_origin = System.get_env("PORTAL_GOV_ORIGIN", "")
  gov_socket = System.get_env("PORTAL_GOV_SOCKET", "")
  hub_token = System.get_env("PORTAL_HUB_TOKEN", "")

  if gov_origin != "" do
    uri = URI.parse(gov_origin)

    loopback_http? =
      uri.scheme == "http" and uri.host in ["127.0.0.1", "localhost", "::1"]

    unless (uri.scheme == "https" or loopback_http?) and uri.host not in [nil, ""] and
             uri.path in [nil, ""] and is_nil(uri.query) and is_nil(uri.fragment) and
             is_nil(uri.userinfo) do
      raise "PORTAL_GOV_ORIGIN must be an origin such as https://gov.example.org (http only on loopback)"
    end
  end

  config :portal, :gov, origin: gov_origin, socket: gov_socket, token: hub_token

  if gov_origin != "" and gov_socket != "" and hub_token != "" do
    config :portal, :auth, Portal.Auth.Hub
  end
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
