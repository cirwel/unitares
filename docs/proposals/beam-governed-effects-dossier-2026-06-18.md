# BEAM Governed-Effects Dossier and Migration Plan

> **For Hermes:** Use `elixir-beam-governance` and `subagent-driven-development` before implementing tasks from this plan. Execute task-by-task with TDD, ExUnit where BEAM code is touched, and Python regression tests where UNITARES analysis/governance code is touched.

**Created:** June 18, 2026  
**Last Updated:** June 18, 2026  
**Status:** Draft dossier + implementation plan — **amended post-council and operator dual-mode decision 2026-06-18 (see Council Amendment below)**

---

## Council Amendment (2026-06-18)

A three-member council (dialectic + code-review + live-verifier) reviewed this dossier against the running system. The boundary reasoning holds; the **mechanism does not**. Verdict: **accept the question, reframe the plane, ship only the telemetry phase.** Corrections, in priority order:

1. **The "new plane" already exists as built/live BEAM code — reframe as a lease-plane *effect-envelope extension*, not a new OTP app.** Verified against the running system:
   - **Leases + single-winner conflicts:** live. `surface_registry.ex:54-76` returns `{:error, :held_by_other}` to the loser; lease plane is the running `beam.smp` on `:8788`.
   - **Revocation:** live **and enforced** — `POST /v1/lease/force-release` (`http_router.ex:205-227`) gated by a separate `LEASE_FORCE_RELEASE_TOKEN`; the regular bearer is barred. Evidence §1 below mis-frames revocation as a gap; it is already solved. *(Strike that framing.)*
   - **Supervision / DynamicSupervisor / lease client:** already built in `elixir/agent_orchestrator/` (`agent_supervisor.ex`, `agent_runner.ex`, `lease_plane_client.ex`) — but **inert** (`:8789` not listening, no plist, nothing spawns through it). The "new governed-effect plane" is this app needing a plist + a caller, not a greenfield build. Do **not** create `elixir/governed_effect_plane/`.

2. **Operator resolution of the execute/record fork: BEAM can do both, but the modes must be explicit.** This is no longer a binary choice. The protocol must carry a per-effect `custody_mode`:
   - `record_only` / shadow mode: BEAM receives the proposal, checks identity/provenance shape, may acquire/observe leases, emits typed telemetry/audit, and returns a durable `effect_id`; the original caller or some external actor still executes. This is useful for migration, dry-run, replay, and learning, but it must not borrow "BEAM committed" language.
   - `execute` / enforced mode: BEAM becomes the executor/committer. It owns the bounded payload or command contract, holds required leases, can veto on `governance_blocked`, and emits the final committed/rejected/revoked fact. The new safety property ("agents propose, BEAM commits") exists in this mode.
   - Phase 2 therefore decides **mode per effect class**, not a single permanent answer. `repo://unitares/doc_update` can start record-only, then promote to execute once payload/idempotency/rollback are specified.

3. **Phase 1 is already fully shipped by PR #846 — demote it to verification.** `harness_lane_from_detail()`, `DEFAULT_EXCLUDED_HARNESS_LANES=("beam",)`, both negative-control tests, and BEAM emitter-disable in test configs all exist and pass. Do not re-implement.

4. **Repo-qualify all Phase 1 paths.** `scripts/analysis/*` and `tests/test_*` live in **`unitares-deploy`**, not the `unitares` server repo the preamble points Hermes at. The verification command needs `cd ~/projects/unitares-deploy &&`.

5. **Phase 4 surface collisions.** `resident:/sentinel_cycle` is already **live + enforced** (`LEASE_PLANE_ENFORCED_SURFACE_KINDS=resident`); `agent:/<id>` is already a `remote_heartbeat` TTL row. Both would double-book. **`repo://unitares/doc_update` is the only clean greenfield first surface.**

6. **Protocol holes (Phase 2):** no idempotency key (retry-after-timeout mints a second `effect_id` and races the surface); proposer-crash-after-202 / custody-TTL unspecified; `payload_ref` has no type/size contract (collides with invariant 7).

7. **Substrate-tax framing is overstated.** PR #218's `ExecutorPool` already mitigated the asyncpg ~60× amplification; the honest residual is **Redis** (still unwrapped). Cite the mitigation, not just the wound — and note this is the dossier's *positive* case for BEAM-on-this-class, currently omitted.

8. **Sequence behind the 2026-06-24 Wave-3 gate.** The effect plane coordinates against the same `core` tables A′ deferred, riding an inert orchestrator. Phase 1 (telemetry hygiene, already shipped) is the only part that proceeds immediately; Phase 2 can proceed as a design-only dual-mode contract. Phases 3–5 gate behind the dual-mode contract, a named first execute-mode surface, and the 2026-06-24 gate read.

Rhetoric discipline after the operator decision: **"membrane"** and **"effect custody"** are valid only when the effect is in `execute` mode. In `record_only` mode, call it shadow custody, audit, or proposal logging. Do not let record-only telemetry borrow commit/execution language.

Decision-packet mapping: this is **Option B (amend)** with an operator clarification: BEAM supports both record-only and execute modes. Phase 1 remains shipped telemetry hygiene; Phase 2 becomes the dual-mode protocol contract; runtime Phases 3–5 remain gated until the first execute-mode effect class and rollback path are named. The sections below are preserved as originally written; read them through this amendment.

---

## Goal

Establish an evidence-grounded plan for moving **governed-effect custody and hot runtime coordination** to BEAM/OTP without rewriting UNITARES durable governance, EISV analysis, calibration, KG, or Lumen body loops.

## Executive Thesis

The June 18 dogfood + ablation run is **not** a blanket argument to migrate UNITARES to BEAM. It is a strong argument for a narrower runtime boundary:

> Agents may propose; only a BEAM-supervised governed-effect plane may commit.

BEAM should own process supervision, leases, revocation, effect custody, bounded queues, and telemetry emission. UNITARES should remain the durable truth substrate for identity, EISV trajectory, calibration, dialectic, outcome history, observability, and KG sediment. Python analysis should remain the primary ablation and reporting lane unless a specific runtime property forces a port.

## Current Evidence Packet

### 1. Identity bleed was a proof-origin bug, not a BEAM failure

- Finding: no-proof/pre-onboard read-only MCP paths could surface a resident identity (`Chronicler`) because server-inferred session/transport context was treated as caller-owned identity.
- Fix landed separately as PR #839: pre-onboard read tools stay identity-neutral unless caller proof is explicit.
- Interpretation: this is an identity/provenance boundary lesson. It does **not** imply identity should be reimplemented in BEAM.
- Boundary consequence: any BEAM governed-effect plane must consume explicit UNITARES identity/proof envelopes and must not mint or launder identity.

### 2. BEAM harness telemetry is operationally important but analytically contaminating unless partitioned

- Ablation watchdog after lane split:
  - `eprocess_eligible=1778`
  - `eprocess_eligible_beam=1493`
  - `eprocess_eligible_substrate=285`
- Fix landed as PR #846: outcome inventory exposes `harness_lane`, and EISV ablation matrix excludes `beam` harness rows by default.
- Interpretation: BEAM/runtime telemetry is a large and useful signal stream, but it must not masquerade as EISV/prior-state predictive validation.
- Boundary consequence: BEAM can emit telemetry, but UNITARES analysis must classify it by provenance before using it for EISV claims.

### 3. Existing roadmap already points to stateful coordination, not whole-system rewrite

Existing BEAM roadmap language already favors A′:

- stateful coordination to BEAM in waves;
- stateless computation stays Python via Ports / HTTP;
- MCP transport remains Python until SDK/fitness gates close;
- each wave needs its own RFC, council pass, state ownership, rollback, and test strategy.

This dossier narrows the current argument further: start with **governed effects** and **runtime custody**, not governance-brain migration.

## Decision Boundary

### Move to BEAM/OTP

BEAM should own surfaces with these properties:

1. **Runtime custody** — a process owns the effect lifecycle from proposal to commit/reject/revoke.
2. **Supervision** — failures are visible, isolated, and restartable under OTP.
3. **Leases and conflicts** — shared mutable surfaces have one owner or one clear loser.
4. **Revocation** — an operator or governance policy can stop a stale or unsafe holder.
5. **Bounded queues** — backpressure is explicit instead of being accidental event-loop pressure.
6. **Telemetry emission** — runtime facts are emitted in typed, provenance-tagged envelopes.

### Keep in UNITARES/Python/Postgres

Do not migrate these merely because they touch governance:

1. **Durable identity and continuity** — UNITARES remains source of truth.
2. **EISV computation and calibration** — analysis stays Python unless a specific measured bottleneck survives Python fixes.
3. **Knowledge graph and dialectic records** — durable semantic sediment remains in UNITARES/Postgres/AGE.
4. **Ablation and skeptical analysis** — Python tooling remains the measurement/reporting lane.
5. **Lumen/anima body loops** — Python remains the embodied creature/runtime unless separately decided.
6. **MCP tool surface** — stays Python until the Elixir MCP SDK gate is explicitly evaluated and passed.

## Proposed Architecture

```text
Hermes / agents / resident processes
        |
        | propose effect + explicit identity/provenance envelope
        v
BEAM Governed-Effect Plane
  - Supervisor tree
  - Registry by canonical surface/effect id
  - DynamicSupervisor per active custody process
  - Lease / revocation client
  - telemetry sink with harness_lane/effect_lane tags
        |
        | accepted/rejected/committed/revoked runtime event
        v
UNITARES Governance MCP / Postgres
  - durable identity
  - outcome_event / audit_event
  - EISV trajectory
  - calibration / ablation / KG / dialectic
```

The plane is not an alternate governance brain. It is an effect-custody membrane.

## Required Invariants

1. **Identity ownership:** BEAM never asserts a caller identity from transport/session inference alone.
2. **Effect custody:** no effect commits unless a custody process owns the effect id and required lease(s).
3. **Typed denial:** rejected effects return stable typed errors (`identity_required`, `lease_held`, `revoked`, `governance_blocked`, `schema_invalid`, etc.).
4. **Telemetry provenance:** every emitted event includes lane tags such as `harness_lane`, `effect_lane`, and `verification_source`.
5. **Analysis separation:** runtime telemetry is visible by default but excluded from EISV validation unless it has prior-state eligibility and non-fixture provenance.
6. **Rollback:** every wave has a named fallback path to the previous Python/direct-effect behavior.
7. **No secret leakage:** continuity tokens and bearer credentials never appear in telemetry, KG, docs, logs, or final summaries.

## Phased Plan

### Phase 0 — Dossier Acceptance and Council Gate

**Objective:** Decide whether this dossier is the right boundary document before writing code.

**Files:**
- Create: `docs/proposals/beam-governed-effects-dossier-2026-06-18.md`
- Modify: `docs/proposals/README.md`

**Steps:**
1. Review this dossier against `beam-footprint-roadmap-v0.md` and `agent-orchestrator-beam-v0.md`.
2. Confirm operator decision: governed-effect custody first; no broad rewrite.
3. Run a council/review pass on the boundary if this becomes implementation-guiding.
4. Record any amendments at the top of this file rather than creating duplicate roadmap docs.

**Verification:**
- `python3 scripts/diagnostics/check_doc_health.py`
- `./scripts/dev/check-shared-contract.sh` if AGENTS/CLAUDE shared-contract files are touched.

**Exit criteria:**
- Operator accepts the boundary or names amendments.
- No open PR collision on BEAM/proposal single-writer surfaces.

### Phase 1 — Telemetry Hygiene **Verification** (already shipped by PR #846)

> **Council amendment:** This phase is **already implemented and tested** by PR #846 — `harness_lane_from_detail()` (`scripts/analysis/outcome_inventory.py`), `DEFAULT_EXCLUDED_HARNESS_LANES=("beam",)` and `filter_rows_for_validation()` (`scripts/analysis/eisv_ablation_matrix.py:43,78-94`), both negative-control tests, and BEAM emitter-disable in test configs (`elixir/sentinel/config/test.exs:15` `emit_checkins: false`; `agent_orchestrator` has zero governance emit calls). Do **not** re-implement. Phase 1 is reduced to a confirmation gate.

**Objective:** Confirm the shipped telemetry hygiene still holds before any new BEAM surface grows.

**Files (all in `unitares-deploy`, NOT the `unitares` server repo):**
- Confirm: `unitares-deploy/scripts/analysis/outcome_inventory.py`
- Confirm: `unitares-deploy/scripts/analysis/eisv_ablation_matrix.py`
- Confirm tests: `unitares-deploy/tests/test_outcome_inventory.py`, `unitares-deploy/tests/test_eisv_ablation_matrix.py`

**Tasks:**
1. Confirm every BEAM harness event carries `detail.harness="beam"` (Invariant 4 is the *emission* contract; Invariant 5 the *analysis* contract — Invariant 5 is unenforceable unless emit always tags the lane).
2. Confirm the negative-control test proving BEAM rows cannot enter the default EISV validation matrix still passes.
3. Confirm BEAM tests run with live governance emitters disabled.

**Verification:**
```bash
cd ~/projects/unitares-deploy && \
PATH=/usr/local/bin:$PATH UNITARES_PYTHON=/usr/local/bin/python3 /usr/local/bin/python3 -m pytest \
  tests/test_outcome_inventory.py \
  tests/test_eisv_ablation_matrix.py
```

**Exit criteria:**
- BEAM/runtime rows are visible in reports.
- Default EISV validation excludes BEAM/harness rows unless explicitly requested.

### Phase 2 — Governed-Effect Protocol Contract

**Objective:** Define the dual-mode governed-effect protocol before building runtime machinery.

> **Operator amendment — BEAM can do both:** Phase 2 no longer chooses "execute vs record" globally. It specifies both modes and requires every effect class to declare which mode it is in. `record_only` mode is advisory/shadow custody: proposal logging, idempotent effect ids, lease observation/acquire if useful, typed telemetry, and no claim that BEAM committed the side effect. `execute` mode is enforcement: BEAM owns the bounded payload or command contract, holds required leases, can veto on `governance_blocked`, and performs or delegates the commit under OTP supervision. Three protocol holes remain mandatory before any execute-mode code: **(a) idempotency key** — a proposer-supplied UUID/content-hash so retry after timeout returns the existing `effect_id`; **(b) proposer-crash-after-202** — custody TTL and heartbeat rules for `proposed`/`held` effects; **(c) payload contract** — mode-specific type, hash, size limit, and redaction rules reconciled with no-secret leakage.

> **Council note — the first effect class picks the *vehicle*, and they are different code:** "extend `agent_orchestrator`" is correct for *one* effect class, not all of them. The orchestrator's native effect is **spawn / await / kill an ephemeral agent** — that is the surface it already models (`AgentRunner` + `Port.open`, `agent:/<id>` presence lease). A *content* effect like `repo://unitares/doc_update` is **not** an orchestrator concern; it belongs on the **lease-plane effect-envelope extension** (a `payload`/`commit_status` envelope on a lease), and the orchestrator stays uninvolved. So the vehicle is determined by the chosen first effect class:
> - **First effect = agent-spawn** → vehicle is `agent_orchestrator`, which is **built but inert** (`:8789` not listening, no plist). De-inerting it is cheap and mechanical (launchd plist mirroring the lease-plane's + set `AGENT_ORCHESTRATOR_BEARER_TOKEN`; it already has `start.sh`, a fail-closed bearer-gated control surface, and lease integration). The real cost is that this stands up a localhost endpoint that **spawns OS processes** (RCE-class surface if the bearer leaks) — which is *why* it's fail-closed and why "turn it on" needs a real caller, not just a plist.
> - **First effect = a content surface (e.g. `doc_update`)** → vehicle is the **lease-plane envelope extension**; the orchestrator is not touched and stays inert until separately needed.
>
> Phase 2 must therefore name the first effect class *before* deciding which app to write, and the "extend agent_orchestrator" line below applies **only** if that class is agent-spawn.

**Files:**
- Create: `docs/proposals/governed-effect-plane-v0.md` or append an accepted protocol section here.
- Potential future code, **vehicle-dependent on the first effect class (see council note above):** for an **agent-spawn** effect, extend `elixir/agent_orchestrator/` (and stand up its launchd plist + bearer). For a **content** effect (`doc_update` etc.), extend the lease plane with an effect envelope rather than the orchestrator. Do **not** create `elixir/governed_effect_plane/` unless a later council explicitly rejects both extension paths.

**Protocol sketch:**

```text
POST /v1/effects
{
  "effect_type": "file_write" | "command" | "repo_commit" | "service_restart",
  "custody_mode": "record_only" | "execute",
  "executor": "caller" | "beam",
  "idempotency_key": "proposer-scoped UUID or content hash",
  "custody_ttl_ms": 120000,
  "surface": "surface://file//absolute/path" | "resident:/..." | "repo://...",
  "proposer": { "agent_uuid": "...", "identity_assurance": "..." },
  "provenance": { "harness": "hermes", "session_id": "...", "verification_source": "agent_reported_tool_result" },
  "payload": {
    "kind": "redacted_summary" | "artifact_ref" | "inline_text" | "command_argv" | "repo_patch_ref",
    "sha256": "...",
    "max_bytes": 65536,
    "ref": "optional ref for record_only or artifact-backed execute"
  },
  "required_leases": [...]
}
```

```text
202 accepted: {effect_id, custody_mode, executor, status: proposed|recorded|held|executing|committed|rejected|revoked}
4xx typed: {error: schema_invalid|identity_required|lease_held|revoked|governance_blocked|payload_too_large|idempotency_conflict}
```

**Tasks:**
1. Write the dual-mode schema and error vocabulary.
2. Decide the first `record_only` surface and the first `execute` surface. Recommended path: `repo://unitares/doc_update` starts `record_only`, then promotes to `execute` once payload and rollback semantics are specified.
3. Define what UNITARES records durably (`audit_event`, `outcome_event`, or both) for each mode.
4. Define what BEAM owns in memory versus what Postgres owns durably.
5. Define rollback mode by custody mode: bypass plane, shadow plane, or fail-closed.
6. Add an explicit rule: `record_only` status may become `recorded`, but never `committed`; `committed` is reserved for `execute` mode.

**Exit criteria:**
- One record-only effect class and one execute-mode candidate are specified end-to-end.
- Idempotency, custody TTL, and payload limits are specified.
- No implementation starts until protocol semantics are reviewable.

### Phase 3 — BEAM Thin Slice: Dual-Mode Shadow + Execute Dry Run

**Objective:** Prove OTP custody in both modes without blocking production effects.

**Files:**
- Existing candidate: extend `elixir/agent_orchestrator/`; it already owns supervised Ports, `DynamicSupervisor`, and the lease-plane client.
- Do **not** create a new OTP app unless the Phase 2 contract proves `agent_orchestrator` is the wrong host.

**TDD tasks:**
1. Write ExUnit test: `record_only` proposal returns stable `effect_id`, status `recorded`, and never reports `committed`.
2. Write ExUnit test: `execute` dry-run proposal acquires required lease, transitions `held -> executing -> committed|rejected`, and emits final telemetry.
3. Write ExUnit test: concurrent proposals for same exclusive surface produce one winner and one typed loser.
4. Write ExUnit test: revocation moves effect to `revoked` and prevents execute-mode commit.
5. Write ExUnit test: crash/restart preserves durable recorded/committed/rejected facts or rehydrates pending custody safely.
6. Implement minimal GenServer + Registry extension using existing orchestrator supervision.
7. Emit telemetry only to a test sink by default.

**Verification:**
```bash
cd elixir/<chosen_app>
mix compile --warnings-as-errors
mix test
mix format --check-formatted
```

**Exit criteria:**
- Shadow custody works under tests.
- No production effect is blocked yet.
- Telemetry contains no secrets and carries lane tags.

### Phase 4 — Enforced Execute Mode for One Low-Blast-Radius Surface

**Objective:** Promote one record-only surface into BEAM-executed enforcement.

**Candidate surfaces:**
1. ~~`resident:/sentinel_cycle` style resident cadence custody.~~ **COLLISION — already live + enforced** (`LEASE_PLANE_ENFORCED_SURFACE_KINDS=resident`, `local_beam` auto-renew). Stacking effect-custody here triples the coordination layers with no reconciliation.
2. ~~`agent:/<id>` ephemeral agent presence/effect lifecycle.~~ **COLLISION — already a `remote_heartbeat` TTL row** (`http_router.ex:453-458`); `AgentRunner` acquires/releases it in `init/1`/`terminate/2`. Double-gates presence.
3. **`repo://unitares/doc_update` — RECOMMENDED first surface.** The only candidate with no existing lease/coordination coverage; lowest blast radius and rollback cost. Define lease semantics explicitly.

**Tasks:**
1. Pick one low-rollback surface and run it in `record_only` first.
2. Add a fail-closed feature flag for `execute` promotion.
3. Add live-smoke script with explicit bearer config and no secret logging.
4. Prove acquire → propose → execute/commit-or-reject → release/revoke.
5. Document rollback command sequence.

**Verification:**
- ExUnit full suite passes.
- Python integration smoke passes.
- Watchdog/ablation reports show telemetry in its own lane.

**Exit criteria:**
- One governed-effect path is enforced.
- Rollback is tested.
- UNITARES health remains green.

### Phase 5 — Expand Only After Evidence

**Objective:** Expand surfaces only when the narrow lane proves useful.

**Possible next waves:**
1. agent orchestration effect custody;
2. resident-agent cadence control;
3. repo commit / PR merge custody;
4. service restart custody;
5. handler dispatch only if Wave 3 gates independently pass.

**Stop signs:**
- BEAM telemetry again contaminates EISV validation.
- Runtime plane requires identity issuance rather than consuming explicit UNITARES proof.
- A Python-side fix collapses the measured problem with lower blast radius.
- Rollback path is unclear.
- Cross-runtime boundary adds more substrate tax than it removes.

## Open Questions

1. Which effect class becomes the first `record_only` shadow: repo doc update, repo commit proposal, or agent spawn? *(This also picks the vehicle — see the Phase 2 council note: agent-spawn → de-inert `agent_orchestrator`; content effects → lease-plane envelope extension. They are different code.)*
2. Which effect class becomes the first `execute` promotion candidate after record-only evidence?
3. Should UNITARES record governed-effect lifecycle as `audit_event`, `outcome_event`, or a dedicated table/event stream?
4. What is the minimum identity assurance tier for enforced execute-mode effects?
5. Which surfaces should stay advisory/record-only forever?
6. What operator UI should show proposed/recorded/held/executing/committed/revoked effects?

## Acceptance Criteria for This Dossier

- It distinguishes measurement/instrumentation from diagnosis, governance policy, and enforcement.
- It uses BEAM for runtime properties, not as a universal rewrite target.
- It preserves UNITARES as durable truth.
- It gives the next implementer exact phases, files, tests, and stop signs.
- It can be amended in place as evidence changes.

## Next Clean Step

Operator decision packet:

```text
Decision already amended: BEAM can do both record and execute.
Next decision: which effect class gets the first dual-mode protocol?
Options:
A. repo://unitares/doc_update — record_only first, promote to execute after payload/rollback proof.
B. repo commit / PR merge proposal — higher value, higher blast radius; record_only only at first.
C. agent spawn/effect lifecycle — extend AgentOrchestrator presence into effect proposals.
D. Defer execute mode until after the 2026-06-24 Wave-3 gate; keep only record_only design.
```
