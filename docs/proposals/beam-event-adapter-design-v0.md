# BEAM Event-Adapter Design — v0

- **Created:** 2026-06-20
- **Last Updated:** 2026-06-20
- **Status:** Design note — DEFERRED to the 2026-06-24 Wave-3 gate read; no runtime code
- **Scope:** How BEAM residents/supervisors would populate the harness-event-safety envelope. Design only.
- **Parent policy:** `harness-event-safety-policy-v0.md` (PR #957, cross-harness contract; not yet on master)

---

> **Gate banner.** This is a *design note*, not an implementation commitment. The
> BEAM adapter is item 4 of the parent policy's §15 follow-up list, which states
> the policy PR should land or be revised before implementation PRs. BEAM
> substrate-identity work is additionally governed by **Wave 3, deferred to the
> 2026-06-24 gate read**. Nothing here should be built into the lease plane or the
> resident check-in path before that read. The purpose of writing it now is to
> pressure-test the parent envelope's BEAM-facing fields while #957 is still draft,
> so any needed contract change is found before external review closes.

## 0. What problem the BEAM adapter solves

BEAM residents (Sentinel, future supervised agents) are the harness the parent
policy is *weakest* at expressing today, for three reasons that map to three
named-but-unsourced fields in the envelope:

1. **Restart storms read as governance noise.** A supervised resident that
   crash-loops produces a burst of restarts. The policy's `restart_count_window`
   → `pause_harness` row (§7) is explicitly **non-normative until a trusted source
   is named** — and no `restart_count_window` substrate exists anywhere in the
   codebase. BEAM is the natural trusted source: the OTP supervisor already counts
   restarts.
2. **Process restart is mistaken for identity churn.** A supervisor restart mints
   a new OS/BEAM process for the *same* logical agent. Without separating the two,
   restart bursts look like per-turn identity minting (the exact failure noted in
   the parent §11 BEAM row).
3. **Residents bypass the onboard path.** BEAM residents check in via
   `process_agent_update(agent_id=<uuid>)` and never call `onboard`, so any
   identity/liveness invariant must live on the **check-in path**, not onboard.

This note specifies how the three v0 envelope fields
(`process_instance_id`, `logical_principal_id`, `restart_count_window`) would be
sourced from BEAM's own trusted state, plus which §7 rows apply to residents.

## 1. The trusted source for restart intensity

The parent §10 requires every pause-triggering counter to name its trusted
source. For BEAM, that source is the **OTP supervisor's own restart accounting**
(`max_restarts` / `max_seconds` intensity window), not any adapter-supplied field
and not the resident's self-report.

Design intent:

- The supervisor (or a thin observer above it) is the *only* writer of
  `attempts.restart_count_window` for a resident's events. It reports the count
  of child restarts for that child within the configured intensity window.
- The resident process itself MUST NOT populate this field — a crash-looping
  child cannot be trusted to count its own crashes. This keeps the field on the
  trusted side of the parent §4 boundary.
- `restart_count_window >= restart_storm` then maps to `pause_harness` scoped to
  **that supervised child / resident**, never the global fleet (parent §10
  minimal-scope rule). A supervisor cooldown is the enforcement actuator.

> **To verify at impl:** the exact OTP surface (e.g. `supervisor:count_children`,
> a `:supervisor`-level restart-intensity hook, or a custom counting supervisor)
> and whether the lease plane already exposes a restart counter. The lease plane
> is Elixir/OTP; this counter likely belongs beside the existing supervision tree,
> emitted onto the event envelope rather than computed in Python.

## 2. process_instance_id vs logical_principal_id

The envelope carries both (parent §5). The mapping for BEAM:

| Field | BEAM source | Lifetime |
|---|---|---|
| `process_instance_id` | The concrete BEAM process / restart incarnation (e.g. the pid-epoch or a supervisor-stamped incarnation id) | Per process; changes on every supervisor restart |
| `logical_principal_id` | The stable resident principal — the agent UUID the resident checks in under | Survives restarts; the thing governance attributes work to |

Consequences:

- A supervisor restart mints a **new `process_instance_id` under the same
  `logical_principal_id`**. Restart bursts therefore do not read as identity
  churn — they read as one principal with N incarnations, which is exactly the
  parent §11 BEAM guard ("distinguish process restart from principal identity").
- Liveness for the principal is the lease-plane **presence lease**
  (`agent:/<uuid>`, refreshed on the check-in path, `expires_at > now()`) — the
  same source the lineage false-archival fix already uses. A dead
  `process_instance_id` whose `logical_principal_id` still holds a live presence
  lease is a restart-in-progress, not a vanished agent.
- `harness_id` stays a human-facing convenience label; it is NOT the identity
  axis. The two ids above are load-bearing.

> **Open design question (gate-relevant):** does `process_instance_id` need to be
> a governance record, or is it purely envelope-local telemetry? Leaning
> telemetry-only — it should not mint a governance identity (that is precisely the
> per-turn-minting failure). The principal's identity is the existing UUID; the
> incarnation is just measurement. This intersects Wave-3 substrate-identity
> decisions, hence the gate.

## 3. harness_local_phase for BEAM

Most BEAM residents have no protected "phase" analogous to Lumen's drawing loop.
The honest answer for v0 is: **BEAM rarely needs `harness_local_phase`.** The one
plausible use is a *supervisor cooldown / draining* phase — while a child is in
restart cooldown, the adapter may defer non-critical effects for that child.
Even then, the cooldown is better modeled as enforcement state (the
`pause_harness` from §1) than as a phase token. Recommendation: leave
`harness_local_phase` unset for BEAM residents in v0; revisit only if a concrete
protected-phase need appears.

## 4. Which §7 decision rows apply to BEAM residents

| Parent §7 condition | BEAM applicability |
|---|---|
| `restart_count_window >= restart_storm` → `pause_harness` | **Primary BEAM row.** Sourced per §1 above; scoped to the supervised child. |
| `auto_resume=true` and `resume_attempt > 1` → `quarantine_session` | Applies if a resident auto-resumes after crash; quarantine target must be the resident's substrate-resolved principal, not an envelope claim. |
| Unrecognized safety-relevant value → `reject_or_require_review` | Applies; residents must fail closed on unknown enums like any harness. |
| `diagnostic_probe=true` (evaluator-derived) | Applies to health-probe traffic against residents; must be derived, not self-claimed. |
| Lumen body-phase row | Not applicable to BEAM. |

A resident's ordinary check-in (`process_agent_update`) is a `governance_write`
under the parent §6, so it requires identity assurance + fresh-parent linkage.
Residents already carry a stable UUID; the design must ensure the check-in path
supplies the `logical_principal_id` and a fresh causal anchor rather than
re-deriving identity per process.

## 5. What this note deliberately does NOT decide

- No Elixir/OTP code, no lease-plane schema, no resident check-in path changes.
- The exact OTP restart-intensity API and whether a custom counting supervisor is
  needed — impl-time, post-gate.
- Whether `process_instance_id` is recorded anywhere durable — gated on Wave-3
  substrate-identity resolution (β/γ).
- Any change to the parent envelope. If this exercise shows a missing field,
  that is a revision to PR #957, raised there — not a fork of the contract here.

## 6. Inputs to the 2026-06-24 gate read

1. Confirm the OTP supervisor can emit restart-intensity onto the envelope as the
   trusted source for `restart_count_window` (closes the parent §10 gap for the
   first real harness).
2. Confirm `process_instance_id` can stay telemetry-only (no minted identity),
   consistent with the no-per-turn-minting invariant.
3. Decide whether the presence-lease liveness mapping (`logical_principal_id` ←
   `agent:/<uuid>` lease) is sufficient, or whether restart-in-progress needs its
   own signal.

Until those are answered at the gate, the BEAM adapter remains design-only.
