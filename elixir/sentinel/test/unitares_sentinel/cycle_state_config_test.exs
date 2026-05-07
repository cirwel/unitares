defmodule UnitaresSentinel.CycleStateConfigTest do
  @moduledoc """
  Path-resolution-from-config tests live in a non-async module because they
  mutate `Application.env`, which is global. If these ran with `async: true`
  alongside the main `cycle_state_test.exs`, any concurrent test that called
  `CycleState.save/1` or `CycleState.load/0` with no `:path` opt could pick
  up the polluted config key and resolve to this test's tmp path.

  Surface 1 council fold (reviewer Critical-2): the prior shape had this
  test in the async module; the race was latent because the only no-opts
  callers in the test file were here. But the contract surface is global —
  any future test that defaults the path would join the race. Isolating
  here removes the class.
  """

  use ExUnit.Case, async: false

  alias UnitaresSentinel.CycleState

  setup do
    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_cycle_state_config_test_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)
    on_exit(fn -> File.rm_rf!(tmpdir) end)

    canonical = Path.join(tmpdir, ".sentinel_state")

    on_exit(fn -> Application.delete_env(:unitares_sentinel, :state_file_path) end)

    {:ok, tmpdir: tmpdir, canonical: canonical}
  end

  test "default path resolves from :unitares_sentinel, :state_file_path config", ctx do
    Application.put_env(:unitares_sentinel, :state_file_path, ctx.canonical)

    state = %{"forced_release_alarm" => %{"last_event_ts" => "2026-05-05T03:14:15+00:00"}}

    # save with no opts uses config; load with no opts uses config.
    :ok = CycleState.save(state)
    assert CycleState.load() == state
  end

  test "resolve_canonical_path/0 prefers Application env over system env", ctx do
    Application.put_env(:unitares_sentinel, :state_file_path, ctx.canonical)
    System.put_env("UNITARES_SENTINEL_STATE_FILE", "/should/not/be/used")
    on_exit(fn -> System.delete_env("UNITARES_SENTINEL_STATE_FILE") end)

    assert CycleState.resolve_canonical_path() == ctx.canonical
  end

  test "resolve_canonical_path/0 falls back to env var when config absent", ctx do
    Application.delete_env(:unitares_sentinel, :state_file_path)
    System.put_env("UNITARES_SENTINEL_STATE_FILE", ctx.canonical)
    on_exit(fn -> System.delete_env("UNITARES_SENTINEL_STATE_FILE") end)

    assert CycleState.resolve_canonical_path() == ctx.canonical
  end

  test "resolve_canonical_path/0 raises when neither config nor env set", _ctx do
    Application.delete_env(:unitares_sentinel, :state_file_path)
    System.delete_env("UNITARES_SENTINEL_STATE_FILE")

    assert_raise RuntimeError, ~r/STATE_FILE path not configured/, fn ->
      CycleState.resolve_canonical_path()
    end
  end
end
