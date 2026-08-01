defmodule UnitaresSentinel.ConfigEnvTest do
  @moduledoc """
  `config/config.exs` is evaluated on EVERY boot — `scripts/start.sh` runs
  `mix run --no-halt`, so config load is part of starting the resident, not a
  one-off build step. That makes a malformed env var much more than a
  config-time nuisance: a raise during config load exits `mix run` non-zero, and
  launchd (`KeepAlive=true`) crash-loops the job.

  The knob most exposed to it is `lease_blocked_alert_after_seconds`, whose own
  comment says it is env-tunable "because retuning during an incident is the
  actual use case and the launchd plist is the tuning surface" — so the failure
  landed on a hurried operator typing "12m" or "720s" at the worst possible
  moment, and `LeaseStarvation.resolve_alert_after_seconds/1`'s defensive
  fallback never got a chance to run because the raise came first.
  `first_boot_lookback_seconds` shares the same helper and shared the hazard.

  The BOOLEAN helper next to it raised for the same reason and had a worse blast
  radius. `scripts/start.sh` sources `~/.config/cirwel/secrets.env` and then
  exports all NINE boolean knobs from `${VAR:-default}`, so both the launchd
  plist and secrets.env can set them — and `UNITARES_SENTINEL_EMIT_FINDINGS` is
  one an operator reaches for mid-incident, where `y` is the obvious wrong
  guess. It used to kill the resident outright instead of leaving findings on.

  These tests read the real config file rather than re-implementing the parsers.
  Both parsers have to be anonymous fns inside config.exs (config is evaluated
  before this project's modules are compiled), so a copy here would prove
  nothing about the file that actually boots.
  """

  use ExUnit.Case, async: false

  import ExUnit.CaptureIO

  @alert_var "UNITARES_SENTINEL_LEASE_BLOCKED_ALERT_AFTER_SECONDS"
  @lookback_var "UNITARES_SENTINEL_FIRST_BOOT_LOOKBACK_SECONDS"
  @emit_findings_var "UNITARES_SENTINEL_EMIT_FINDINGS"
  @start_poller_var "UNITARES_SENTINEL_START_POLLER"

  @default_alert_after_seconds 720
  @default_lookback_seconds 7 * 24 * 60 * 60

  setup do
    on_exit(fn ->
      System.delete_env(@alert_var)
      System.delete_env(@lookback_var)
      System.delete_env(@emit_findings_var)
      System.delete_env(@start_poller_var)
    end)

    :ok
  end

  defp read_config do
    Config.Reader.read!(Path.expand("config/config.exs", File.cwd!()), env: :test)
  end

  # `config/test.exs` pins seven of the nine boolean knobs to false so the
  # supervisor tree stays down under ExUnit, which would mask whatever the base
  # parser produced for them. Reading as `:prod` (no `config/prod.exs` exists, so
  # nothing is imported over the top) is the only way to observe config.exs's own
  # values — and it is the environment the launchd resident actually boots in,
  # which is where the crash-loop lived.
  defp read_base_config do
    Config.Reader.read!(Path.expand("config/config.exs", File.cwd!()), env: :prod)
  end

  test "a well-formed integer env var is still honoured" do
    System.put_env(@alert_var, " 300 ")

    assert read_config()[:unitares_sentinel][:lease_blocked_alert_after_seconds] == 300
  end

  test "an unset env var still yields the default" do
    System.delete_env(@alert_var)

    assert read_config()[:unitares_sentinel][:lease_blocked_alert_after_seconds] ==
             @default_alert_after_seconds
  end

  test "a malformed alert threshold warns and falls back instead of aborting the boot" do
    System.put_env(@alert_var, "12m")

    {config, warning} = with_io(:stderr, fn -> read_config() end)

    assert config[:unitares_sentinel][:lease_blocked_alert_after_seconds] ==
             @default_alert_after_seconds

    # The warning has to name both the variable and the value, because the
    # operator reading it is mid-incident and looking at a plist, not at code.
    assert warning =~ @alert_var
    assert warning =~ "12m"
  end

  test "the pre-existing first-boot lookback knob shares the guard" do
    System.put_env(@lookback_var, "7d")

    {config, warning} = with_io(:stderr, fn -> read_config() end)

    assert config[:unitares_sentinel][:first_boot_lookback_seconds] == @default_lookback_seconds
    assert warning =~ @lookback_var
    assert warning =~ "7d"
  end

  test "well-formed boolean env vars are still honoured in both directions" do
    System.put_env(@emit_findings_var, " OFF ")
    System.put_env(@start_poller_var, "Yes")

    config = read_base_config()

    assert config[:unitares_sentinel][:emit_findings] == false
    assert config[:unitares_sentinel][:start_poller] == true
  end

  test "a malformed boolean knob warns and falls back instead of killing the resident" do
    # `y` is the obvious wrong guess for a flag whose accepted set is
    # 1/true/yes/on/0/false/no/off, and `UNITARES_SENTINEL_EMIT_FINDINGS` is
    # reached for mid-incident. Raising here aborted config load, exited
    # `mix run --no-halt` non-zero, and handed launchd (KeepAlive=true) a
    # crash-looping resident: a mistuned knob took the whole resident down
    # rather than degrading to the default. Same argument as the integer knob
    # above, and a wider surface — start.sh exports all NINE booleans from
    # `${VAR:-default}` after sourcing secrets.env.
    System.put_env(@emit_findings_var, "y")

    {config, warning} = with_io(:stderr, fn -> read_config() end)

    assert config[:unitares_sentinel][:emit_findings] == true
    assert warning =~ @emit_findings_var
    assert warning =~ "y"
  end

  test "a malformed boolean knob whose default is false falls back to false" do
    # The fallback is the DEFAULT, not a blanket `true`: turning a resident on
    # because its flag was mistyped would be its own incident.
    System.put_env(@start_poller_var, "maybe")

    {config, warning} = with_io(:stderr, fn -> read_base_config() end)

    assert config[:unitares_sentinel][:start_poller] == false
    assert warning =~ @start_poller_var
    assert warning =~ "maybe"
  end
end
