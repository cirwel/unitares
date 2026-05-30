import Config

# Defaults for the Wave 3a BEAM handler app.
#
# Listening port discipline (RFC §5 PR #4): pick a Wave-3a-specific port
# distinct from neighbors —
#
#   8766 anima-mcp (Pi)
#   8767 governance-mcp (Mac, public)
#   8768 governance-gateway (Mac, weak-client surface)
#   8769 anima-mcp no-auth proxy (occupied — Anima Pi-bound proxy)
#   8770 wave3a-handlers (THIS APP)  ← chosen
#   8788 lease-plane (Elixir)
#
# 8770 leaves the heterogeneity rule from MEMORY.md "Ports & Endpoints —
# DO NOT NORMALIZE" intact while keeping numeric adjacency to the rest of
# the MCP family. Verified free of conflicting listeners on 2026-05-30 via
# `lsof -i :8770`.
config :wave3a_handlers,
  http_port: 8770,
  http_ip: {127, 0, 0, 1},
  start_application: true,
  start_http: true,
  # Python probe surface — host:port lifted from PR #1 of this wave.
  # `WAVE_3A_PROBE_BASE_URL` overrides at runtime; the literal default is the
  # same dotted-quad the proxy module hits.
  probe_base_url: System.get_env("WAVE_3A_PROBE_BASE_URL") || "http://127.0.0.1:8767",
  probe_timeout_ms: 500

if File.exists?("config/#{config_env()}.exs") do
  import_config "#{config_env()}.exs"
end
