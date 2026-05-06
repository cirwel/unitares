import Config

first_boot_lookback_seconds =
  case System.get_env("UNITARES_SENTINEL_FIRST_BOOT_LOOKBACK_SECONDS") do
    nil -> 7 * 24 * 60 * 60
    raw -> String.to_integer(raw)
  end

config :unitares_sentinel,
  database_url:
    System.get_env("UNITARES_SENTINEL_DATABASE_URL") ||
      System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance",
  pool_size: 2,
  session_file_path: System.get_env("UNITARES_SENTINEL_SESSION_FILE"),
  legacy_session_file_path: System.get_env("UNITARES_SENTINEL_LEGACY_SESSION_FILE"),
  poller_interval_ms: 30_000,
  poller_initial_delay_ms: 1_000,
  poller_tick_timeout_ms: 30_000,
  start_fleet_state: true,
  start_websocket: false,
  start_fleet_finding_emitter: false,
  analysis_interval_ms: 300_000,
  analysis_initial_delay_ms: 5_000,
  analysis_jitter_ms: 5_000,
  analysis_tick_timeout_ms: 45_000,
  websocket_url: System.get_env("GOV_WS_URL") || "ws://localhost:8767/ws/eisv",
  websocket_reconnect_ms: 10_000,
  first_boot_lookback_seconds: first_boot_lookback_seconds,
  lease_advisory_enabled: true,
  lease_plane_base_url: System.get_env("LEASE_PLANE_BASE_URL") || "http://127.0.0.1:8788",
  lease_plane_timeout_ms: 2_000,
  findings_url: System.get_env("UNITARES_FINDINGS_URL") || "http://localhost:8767/api/findings",
  findings_timeout_ms: 3_000,
  findings_agent_id: System.get_env("UNITARES_SENTINEL_AGENT_ID") || "sentinel",
  findings_agent_name: "Sentinel",
  emit_findings: true,
  emit_checkins: false,
  governance_tools_url:
    System.get_env("UNITARES_GOVERNANCE_TOOLS_URL") || "http://localhost:8767/v1/tools/call",
  governance_checkin_timeout_ms: 45_000

if File.exists?("config/#{config_env()}.exs") do
  import_config "#{config_env()}.exs"
end
