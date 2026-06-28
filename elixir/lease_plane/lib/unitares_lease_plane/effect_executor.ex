defmodule UnitaresLeasePlane.EffectExecutor do
  @moduledoc """
  Behaviour for a per-`effect_type` executor on the governed-effect EXECUTE path
  (§5a). The `EffectCustodian` GenServer acquires the required leases and clears
  the governance veto, then hands off to `apply_effect/3`.

  Contract for an implementation (e.g. `FileWriteExecutor`):

    * `apply_effect/3` MUST capture the rollback pre-image (via
      `EffectRepo.record_pre_image/4`) BEFORE mutating the surface, then mutate,
      then `EffectRepo.mark_committed/1` AFTER the mutation is durable.
    * On a mutation failure it MUST run its own in-process compensation (restore
      the pre-image while it still holds the lease) and return `{:rejected, _}`;
      if compensation also fails it MUST `EffectRepo.quarantine/1` and return
      `{:rejected, :rollback_failed}`.
    * It MUST NOT restore bytes across a process crash — crash recovery is the
      custodian/`EffectRecovery`'s job and is by-construction non-mutating
      (commit-forward / tombstone / quarantine by DB mark only).

  `reversible?/0` declares whether the type supports rollback. An irreversible
  type (e.g. `agent_spawn`) returns `false` and is promotable only under an
  explicit no-rollback acknowledgment (§5b), never via this reversible path.
  """

  @callback apply_effect(effect_id :: String.t(), payload :: map(), leases :: [map()]) ::
              {:committed, map()} | {:rejected, term()}

  @callback reversible?() :: boolean()
end
