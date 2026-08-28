defmodule UnitaresLeasePlane.PeriodicWorkerContractTest do
  @moduledoc """
  Locks the `PeriodicWorker` callback contract against silent drift.

  `PeriodicWorker.run_worker/2` calls `worker.perform(args)` and rescues, so a
  worker that does not export `perform/1` does not fail the build, fail a test,
  or stop the supervision tree — it logs `undefined or private` once per tick
  and does nothing, forever. `IdentityNonceReaper` and `TopicMessageReaper` both
  shipped exporting only `run_once/0` and were found that way in production:
  `lease_plane.consumed_identity_attestations` held 621 rows, every one already
  past `expires_at`, because migration 067's purge had never executed.

  This reads `application.ex` as source rather than asserting a hand-kept list,
  so registering a new worker with the wrong arity fails here instead of
  silently in a log nobody greps.
  """
  use ExUnit.Case, async: true

  @application_path "lib/unitares_lease_plane/application.ex"

  defp registered_workers do
    @application_path
    |> File.read!()
    |> then(&Regex.scan(~r/worker:\s*(UnitaresLeasePlane\.[A-Za-z0-9_.]+)/, &1))
    |> Enum.map(fn [_, mod] -> Module.concat([mod]) end)
    |> Enum.uniq()
  end

  test "application.ex registers at least one PeriodicWorker" do
    # Guards the regex itself: a refactor that changes the child-spec shape
    # must not turn this suite into a vacuous pass.
    assert registered_workers() != []
  end

  test "every registered PeriodicWorker exports perform/1" do
    for worker <- registered_workers() do
      assert Code.ensure_loaded?(worker), "#{inspect(worker)} is registered but does not load"

      assert function_exported?(worker, :perform, 1),
             "#{inspect(worker)} is registered as a PeriodicWorker but does not export " <>
               "perform/1. PeriodicWorker calls worker.perform(args) inside a rescue, so " <>
               "this fails silently at runtime — once per tick, forever."
    end
  end
end
