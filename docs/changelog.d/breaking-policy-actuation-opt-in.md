- **Policy actuation is explicit opt-in.** EISV, risk, basin, void, coherence,
  and CIRS remain recorded as proprioceptive policy inputs, but the default
  `UNITARES_POLICY_ACTUATION=observe` posture no longer turns their inferred
  `pause` recommendation into a lifecycle hold. The raw recommendation remains
  under `policy_evaluation`; the effective `decision` is `proceed` / `guide`,
  and `enforcement` reports `posture=observe`, the recommended and effective
  actions, and `applied=false`. No pause token is minted and no later write is
  refused. Operators that intentionally rely on the historical circuit breaker
  must set `UNITARES_POLICY_ACTUATION=enforce`. Existing durable pauses,
  authentication and authorization failures, archive state, and bounded loop
  cooldowns remain enforced. Dialectic remains available as optional review and
  as recovery for a pause that was actually applied. This is a behavioral
  change only: the capability catalog and input-schema digests do not move
  (#3131).
