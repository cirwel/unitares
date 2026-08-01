import Config

bool_env = fn name, default ->
  case System.get_env(name) do
    nil ->
      default

    "" ->
      default

    raw ->
      # Total for exactly the reason `int_env` below is, and the argument is not
      # weaker here — it is stronger. `scripts/start.sh` sources
      # `~/.config/cirwel/secrets.env` and then exports all NINE of these knobs from
      # `${VAR:-default}`, so both the launchd plist and secrets.env can set them;
      # config.exs is evaluated on every `mix run --no-halt`, so a raise here
      # exits non-zero and hands launchd (`KeepAlive=true`) a crash-looping
      # resident. `UNITARES_SENTINEL_EMIT_FINDINGS=y` is a knob an operator
      # reaches for MID-INCIDENT, and "y" is the obvious wrong guess: it used to
      # kill the resident outright rather than leave findings on. Warn naming the
      # variable and the offending value, then keep the default — a mistuned knob
      # must degrade, never kill.
      case raw |> String.trim() |> String.downcase() do
        "1" ->
          true

        "true" ->
          true

        "yes" ->
          true

        "on" ->
          true

        "0" ->
          false

        "false" ->
          false

        "no" ->
          false

        "off" ->
          false

        _ ->
          IO.warn(
            "#{name} must be a boolean-like value, got: #{inspect(raw)} — " <>
              "falling back to #{inspect(default)}",
            []
          )

          default
      end
  end
end

int_env = fn name, default ->
  case System.get_env(name) do
    nil ->
      default

    "" ->
      default

    raw ->
      # Total on purpose. This used to end in `String.to_integer/1`, which
      # RAISES on non-numeric input — and `scripts/start.sh` runs
      # `mix run --no-halt`, so config.exs is evaluated on EVERY boot. A plist
      # value of "12m" or "720s" therefore aborted config load, exited non-zero,
      # and handed launchd (KeepAlive=true) a crash-looping resident. The knob
      # below documents itself as the incident-time tuning surface, so the
      # failure mode landed on a hurried operator at the worst possible moment,
      # and `resolve_alert_after_seconds/1`'s defensive fallback in
      # `UnitaresSentinel.LeaseStarvation` never got a chance to run. Warn
      # naming the variable and the offending value, then keep the default:
      # a mistuned knob must degrade to "the old cadence", never to "no
      # resident at all".
      case Integer.parse(String.trim(raw)) do
        {value, ""} ->
          value

        _ ->
          IO.warn(
            "#{name} must be an integer, got: #{inspect(raw)} — " <>
              "falling back to #{inspect(default)}",
            []
          )

          default
      end
  end
end

first_boot_lookback_seconds =
  int_env.("UNITARES_SENTINEL_FIRST_BOOT_LOOKBACK_SECONDS", 7 * 24 * 60 * 60)

config :unitares_sentinel,
  start_application: bool_env.("UNITARES_SENTINEL_START_APPLICATION", true),
  database_url:
    System.get_env("UNITARES_SENTINEL_DATABASE_URL") ||
      System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance",
  pool_size: 2,
  start_postgrex: bool_env.("UNITARES_SENTINEL_START_POSTGREX", true),
  start_finch: bool_env.("UNITARES_SENTINEL_START_FINCH", true),
  session_file_path: System.get_env("UNITARES_SENTINEL_SESSION_FILE"),
  legacy_session_file_path: System.get_env("UNITARES_SENTINEL_LEGACY_SESSION_FILE"),
  poller_interval_ms: 30_000,
  poller_initial_delay_ms: 1_000,
  poller_tick_timeout_ms: 30_000,
  start_fleet_state: bool_env.("UNITARES_SENTINEL_START_FLEET_STATE", true),
  start_websocket: bool_env.("UNITARES_SENTINEL_START_WEBSOCKET", false),
  start_fleet_finding_emitter: bool_env.("UNITARES_SENTINEL_START_FLEET_FINDING_EMITTER", false),
  start_poller: bool_env.("UNITARES_SENTINEL_START_POLLER", false),
  analysis_interval_ms: 300_000,
  analysis_initial_delay_ms: 5_000,
  analysis_jitter_ms: 5_000,
  analysis_tick_timeout_ms: 45_000,
  websocket_url: System.get_env("GOV_WS_URL") || "ws://localhost:8767/ws/eisv",
  websocket_reconnect_ms: 10_000,
  first_boot_lookback_seconds: first_boot_lookback_seconds,
  lease_advisory_enabled: true,
  # Seconds a resident may be refused its advisory lease before it reports its
  # OWN starvation to /api/findings (2026-07-31 immortal-lease incident: 5,703
  # consecutive "tick skipped by lease enforcement" warnings, zero alerts).
  # Seconds and not a tick count, because `poller_interval_ms` is itself tunable
  # and ticks are jittered — a count would silently mean a different wall clock
  # after any retune. One shared value covers both residents. Env-tunable
  # because retuning during an incident is the actual use case and the launchd
  # plist is the tuning surface.
  # `ForcedReleasePoller` is started by `Application.poller_children/0` as a bare
  # module atom, so application env is its ONLY channel for this: it has to live
  # here, not just as an inline `Application.get_env/3` default.
  lease_blocked_alert_after_seconds:
    int_env.("UNITARES_SENTINEL_LEASE_BLOCKED_ALERT_AFTER_SECONDS", 720),
  lease_audit_session: System.get_env("UNITARES_SENTINEL_AUDIT_SESSION"),
  lease_enforced_surface_kinds: System.get_env("LEASE_PLANE_ENFORCED_SURFACE_KINDS"),
  lease_plane_base_url: System.get_env("LEASE_PLANE_BASE_URL") || "http://127.0.0.1:8788",
  lease_plane_timeout_ms: 2_000,
  findings_url: System.get_env("UNITARES_FINDINGS_URL") || "http://localhost:8767/api/findings",
  findings_timeout_ms: 3_000,
  findings_agent_id: System.get_env("UNITARES_SENTINEL_AGENT_ID") || "sentinel",
  findings_agent_name: "Sentinel",
  emit_findings: bool_env.("UNITARES_SENTINEL_EMIT_FINDINGS", true),
  emit_checkins: bool_env.("UNITARES_SENTINEL_EMIT_CHECKINS", false),
  governance_tools_url:
    System.get_env("UNITARES_GOVERNANCE_TOOLS_URL") || "http://localhost:8767/v1/tools/call",
  governance_checkin_timeout_ms: 45_000

if File.exists?("config/#{config_env()}.exs") do
  import_config "#{config_env()}.exs"
end
