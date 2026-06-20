# BEAM Event-Adapter Design — v0

- **Created:** 2026-06-20
- **Last Updated:** 2026-06-20
- **Status:** Design note — DEFERRED to the 2026-06-24 Wave-3 gate read; no runtime code
- **Scope:** How BEAM residents/supervisors would populate the harness-event-safety envelope. Design only.
- **Parent policy:** [`harness-event-safety-policy-v0.md`](harness-event-safety-policy-v0.md) (cross-harness contract; merged via PR #957)

---

> **Gate banner.** This is a *design note*, not an implementation commitment. The
> BEAM adapter is item 4 of the parent policy's §15 follow-up list, which states
> the policy PR should land or be revised before implementation PRs. BEAM
> substrate-identity work is additionally governed by **Wave 3, deferred to the
> 2026-06-24 gate read**. Nothing here should be built into the lease plane or the
> resident check-in path before that read. The purpose of writing it now is to
> pressure-test the parent envelope's BEAM-facing fields now that #957 has merged,
> so any needed contract change is raised as a revision to it.

## 0. What problem the BEAM adapter solves

BEAM residents (Sentinel, future supervised agents) are the harness the parent
policy is *weakest* at expressing today, for three reasons that map to three
named-but-unsourced fields in the envelope:

1. **Restart storms read as governance noise.** A supervised resident that
   crash-loops produces a burst of restarts. The policy's `restart_count_window`
   → `pause_harness` row (§7) is explicitly **non-normative until a trusted source
   is named** — and no `restart_count_window` substrate exists anywhere in the
   codebase. BEAM is the natural trusted source — though, as §1 shows after
   checking the source, the OTP supervisor does *not* already expose a restart
   count, so this is net-new (but cheap) instrumentation.
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
source. For BEAM, that source is BEAM-internal supervision state, not any
adapter-supplied field and not the resident's self-report.

> **Verified against `unitares-deploy/elixir` source, 2026-06-20.** Findings that
> reshape the design:
>
> - **OTP restart-intensity is not queryable.** All supervisors are
>   `:one_for_one` with default, un-overridden intensity
>   (`agent_supervisor.ex`, `lease_supervisor.ex`, the app supervisors). OTP's
>   `max_restarts`/`max_seconds` is internal — it *shuts down the supervisor* when
>   exceeded but exposes no count. `DynamicSupervisor.count_children/1` returns
>   `.active` (live children), **not** a restart count. **No restart counter
>   exists in the code today**, and `:telemetry` is a dep but is **unwired** (no
>   `:telemetry.execute/attach` in app code). So this counter is net-new
>   instrumentation, however cheap.
> - **Ephemeral orchestrated agents are `restart: :temporary`** — the supervisor
>   never resurrects them (`agent_supervisor.ex:34`, `agent_runner.ex:20`:
>   "the supervisor does not resurrect it"). There is therefore **no OTP restart
>   storm for orchestrated agents at all.** This corrects the framing: the
>   restart-storm signal is two distinct things, neither of which is OTP intensity
>   over ephemeral agents.

`restart_count_window` should be split into two BEAM-internal sub-signals, each
with its own trusted writer:

1. **Resident/permanent-child crash-loop** — Sentinel's supervised GenServers and
   the lease plane's `:permanent` `periodic_worker` *are* restarted by their
   supervisors. The trusted source is a **supervisor-side restart counter** keyed
   by child id (an ETS/registry counter incremented on the child's `init`, or a
   `:telemetry` handler on restart), since OTP intensity itself is opaque. The
   crash-looping child MUST NOT write its own count (parent §4 trust boundary).
2. **Orchestrator re-spawn loop** — the orchestrator repeatedly `start_child`-ing
   the same logical task. This is an **orchestrator-level counter**, not OTP
   restart intensity, and belongs in the orchestrator, keyed by logical task.

`restart_count_window >= restart_storm` maps to `pause_harness` scoped to that
supervised child / resident / logical task, never the global fleet (parent §10
minimal-scope). A supervisor cooldown is the enforcement actuator.

> **Net for the parent contract:** `restart_count_window` is best documented in
> #957 as the max of these two sub-signals (or two named fields). Either way the
> source is BEAM-internal, satisfying the parent §10 "name the trusted source"
> requirement — and being net-new is exactly why this stays gate-deferred, not
> a thing to wire pre-gate.

## 2. process_instance_id vs logical_principal_id

The envelope carries both (parent §5). The mapping for BEAM:

| Field | BEAM source | Lifetime |
|---|---|---|
| `process_instance_id` | The concrete BEAM run handle — the orchestrator's `agent_id` (`AgentRunner.generate_agent_id`) for orchestrated agents, or a pid-epoch for residents | Per process/run; changes on every restart/re-spawn |
| `logical_principal_id` | The stable principal — `holder_agent_uuid`, the governance UUID the agent checks in under | Survives restarts; the thing governance attributes work to |

> **Verified, 2026-06-20.** This mapping is *already the architecture*, not a new
> idea: the orchestrator separates `agent_id` (BEAM-side run handle) from
> `holder_agent_uuid` (governance UUID) in `agent_runner.ex`/`agent_supervisor.ex`,
> and **the orchestrator never onboards on the child's behalf** — the child mints
> its own governance identity via a lineage-provisioned onboard
> (`agent_orchestrator.ex:15-24`). So the governance UUID is the principal, minted
> by the child, entirely decoupled from the BEAM incarnation. `process_instance_id`
> being telemetry-only that mints no identity is therefore *descriptive of the
> current system*, which puts low Wave-3 risk on this recommendation.

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

## 6. Findings and recommendation (was: open questions)

The three questions this note opened with were resolved against the
`unitares-deploy/elixir` source on 2026-06-20. None requires an operator answer;
they are engineering facts. Summary:

| Question | Finding | Status |
|---|---|---|
| Can the supervisor be the trusted source for `restart_count_window`? | Yes, but **net-new instrumentation** — OTP restart-intensity is opaque, no counter exists, `:telemetry` is unwired. And ephemeral agents are `:temporary` (never restarted), so the signal splits into resident-crash-loop (supervisor-side counter) + orchestrator-respawn (orchestrator-side counter). See §1. | **Resolved — feasible, deferred to impl** |
| Can `process_instance_id` stay telemetry-only (no minted identity)? | Yes — it *already is*. The orchestrator separates `agent_id` (run handle) from `holder_agent_uuid` (governance UUID), and identity is minted by the child's own onboard, not the BEAM incarnation. See §2. | **Resolved — descriptive of current system** |
| Is the presence-lease liveness mapping sufficient? | Yes — `agent:/<uuid>` is a canonical presence surface routed to `remote_heartbeat` (a self-reaping DB row, HTTP-heartbeated before `expires_at`); residents are remote_heartbeat holders. The lease survives a single BEAM incarnation, so a dead `process_instance_id` under a live lease reads as restart-in-progress, exactly as intended. (`canonicalize.ex:376-384`, `lease_plane.ex:29-49`) | **Resolved — mechanism already exists** |

### Live evidence (governance DB + BEAM logs, 2026-06-20)

Rather than wait on a calendar gate, the failure modes were checked against
running telemetry. Result: the demand the adapter addresses has **not** fired in
a way an envelope would have caught.

- **Identity churn was real and recent — but source-fixed, not envelope-fixed.**
  `dispatch_beam_harness` minted 155 identities on 2026-06-18 (150 instantly
  archived), 163 over 7 days, then dropped to 0 within 24h. The cohort is 162/163
  `spawn_reason=dispatch_beam_harness`, and the cutoff coincides with the
  dispatch-beam restart that deployed the `#17` test-leak fix (an env guard in
  `runtime.exs`). A narrow harness bugfix closed it — exactly the "narrow gateway
  bugfix, not a policy import" pattern the parent policy predicts.
- **Restart-storm is not evidenced on residents.** `sentinel-beam` has run as a
  single process since 2026-06-15 (5+ days, zero VM restarts; 4 lifetime
  terminations, all DB-pool shutdowns). The `restart_count_window` signal this
  adapter would source has no incident to act on.
- **Unrelated real bug found:** the lease-plane HTTP error path crashed 97× with
  `Plug.Exception.status/1` `UndefinedFunctionError` (the error handler errors).
  Dormant in the live instance but unconfirmed-fixed — a Plug/router fix, out of
  scope for this adapter, noted so it is not lost.

Net: the identity-churn demand fired once and a **source fix** met it; the
restart-storm demand has not fired. That is the evidence basis for the
demand-trigger below, and a live example of why building the envelope adapter
now would solve an already-closed problem with a heavier mechanism.

**Implementation is demand-triggered, not approval-gated.** The mapping above is
the design-of-record; what it is NOT is a reason to build now. The BEAM adapter
(the net-new restart counters of §1, the envelope emission) should be built only
when **telemetry actually shows a BEAM restart-storm or identity-churn event** —
a measured incident, not a calendar gate and not an operator/author sign-off.
Rationale, stated against my own grain: on BEAM the failure modes this adapter
addresses are mostly *unobserved* (ephemeral agents are `:temporary` so cannot
restart-storm; residents are n≈1 Sentinel; the counter is instrumentation for an
event nobody has logged). Building it pre-incident would be inventory ahead of
demand — and "it can ship inert" is precisely the tell for that.

So the trigger is concrete and evidence-based:

> Build the BEAM adapter when a real BEAM restart-storm (a `:permanent`/resident
> child exceeding a restart threshold) or identity-churn event (per-incarnation
> minting, principal/process confusion) appears in telemetry — and only after the
> policy (#957) has settled its envelope. Until then this note stays design-only.

The 2026-06-24 Wave-3 read does not need to approve a build; it only needs to not
*contradict* this mapping's identity stance (`process_instance_id` telemetry-only,
no new identity semantics), which §2 shows is already the deployed architecture.
