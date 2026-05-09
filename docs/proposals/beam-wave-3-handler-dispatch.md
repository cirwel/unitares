# Wave 3 RFC: handler dispatch + identity middleware + dialectic resolution → BEAM

**Status:** v0.1.1, 2026-05-08. Council pass complete (architect / reviewer / live-verifier in parallel). **Read v0.1.1 AMENDMENT below first**, then v0.1 body as historical record. No code lands until v0.1.1 closes (operator decision: proceed-to-implementation, second-council-on-v0.1.1, or v0.2-redraft).
**Parent:** `docs/proposals/beam-footprint-roadmap-v0.md` v0.3 / v0.3.1 (operator-decision migration commit + council fold).
**Sibling, completed:** `docs/proposals/beam-wave-1-sentinel.md` (Sentinel-on-BEAM Surface 1+2 shipped, Surface 3 in flight).
**Sibling, completed:** `docs/proposals/surface-lease-plane-v0.md` Phase A + Wave 2 hardening (#412/#414/#417/#418/#419) — boundary contract is firm.
**Wave 0 channel:** `coordination_failure.beam_python_boundary.*` constants exist (#408) but are typed-but-unused; Wave 3 wires them at call sites so exit criterion #3 ("no new substrate-tax pattern at the Python-handler-body boundary") is measurable.
**Operator-protective single-writer surfaces:** Identity / onboarding (per `CLAUDE.md` "Before Starting Work on a Single-Writer Surface") spans this entire RFC. Branch from this RFC's head before any parallel work.

---

## V0.1.2 AMENDMENT 2026-05-08 — second council fold (post-v0.1.1)

**Read with v0.1.1 below.** Second council pass (architect / reviewer / live-verifier in parallel) was run on v0.1.1 per operator-decision (α). Findings: architect 3 BLOCK / 14 CONCERN / NIT; reviewer 2 BLOCK / 5 CONCERN / 1 NIT; verifier 18 VERIFIED / 3 DRIFT / 3 negative-claim VERIFIED. Council unanimous verdict: v0.1.2 amendment (narrow), not v0.2 redraft. Architect explicit: "if v0.1.2 produces another structural delta, v0.2 redraft becomes mandatory."

**Bias-persistence acknowledgment.** v0.1.1's bias acknowledgment was earnest for the named cases but architect's lane 1 found three fresh bias signatures in v0.1.1 itself — B6 (cache-invalidation defense recreates substrate tax), B8(iii) (lunge-at-first-association on single-process), §8 criterion 6 (sunk-cost protection in new shape). Verifier added a fourth: C2's "anchored to lease-plane Phase A measured baseline" was claimed-anchored to a baseline that doesn't exist (zero rows in `audit.coordination_events` across all partitions). The pattern is not "this author won't migrate"; it's "this author defends conservative-substrate posture in surface-specific ways, and migrates the defense across folds." Operator should weight v0.1.2's structural completeness against this pattern; if v0.1.2 surfaces a fifth bias signature on third council pass, v0.2-redraft-from-scratch is the discipline to break the cycle.

**Where v0.1.2 conflicts with v0.1.1 below, v0.1.2 governs.**

### B1.2 (architect Lane-1 B1 + Lane-2 (G)) — Anchor "before implementation starts" to git-event; anchor (B1) 60% threshold

v0.1.1 §B1's "before implementation starts" was escape-hatchable. (G) and B1's 60% threshold lacked anchors.

**Binding spec:**
- **B1 git-event anchor:** ODE profile commit must land on master before any commit on `wave-3-rfc-draft` (or successor branch) introduces a file under `elixir/handler_dispatch/` or any new `elixir/` tree. CI lint check enforces (script: `scripts/dev/check-wave3-ode-prereq.sh`).
- **B1 60% threshold anchor:** chosen as the threshold at which "more than half the floor is in `governance_core/` math, so porting the layer above the math cannot reduce the floor." Defensible vs higher (50% would be ambiguous between port-helps and port-doesn't) or lower (75% would fail to halt cases where the math is still the dominant cost). Council should challenge this anchor sentence; current text is the author's judgment, not a measurement.
- **(G) thresholds re-anchored to historical variance:** resolution-rate regression triggers if mean over the 14-day canary window falls more than 1σ below the trailing-30-day mean (computed from `core.dialectic_sessions` rows pre-Wave-3-implementation). Reviewer-reassignment rate triggers at >2σ above trailing-30-day mean. Both baselines (mean + σ) pinned in §"Exit criteria" prior to implementation start; the pinning commit is itself a Wave 3 prereq.

### B2.2 (architect B2 + reviewer CONCERN-5) — Saga gains PG-persistent state and per-step idempotency

v0.1.1's saga was 2PC-shaped but lacked durability under PG-commit failure (architect: phantom reads + observers) AND under GenServer crash-restart (reviewer: agents in RESOLVED-state-not-PG-committed).

**Binding spec:** Wave 3 introduces a new PG table `coordination.session_resolution_sagas`:

```sql
CREATE TABLE coordination.session_resolution_sagas (
    saga_id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES core.dialectic_sessions(session_id),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'applied', 'committed', 'reverted')),
    paused_agent_id UUID NOT NULL,
    reviewer_agent_id UUID NOT NULL,
    resolution_payload_json JSONB NOT NULL,
    resolution_payload_hash TEXT NOT NULL,  -- for idempotency
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (session_id, resolution_payload_hash)
);
```

Saga sequence:
1. Session GenServer INSERTs saga row with `status='reserved'`, then issues `GenServer.call(:reserve_for_session_resolution, {session_id, saga_id})` to both agent GenServers. Idempotent: agent GenServer keys on `(session_id, saga_id)`; re-call with same key returns ACK if already reserved.
2. Both agents ACK reservation → session GenServer UPDATEs saga to `status='applied'` AND issues `GenServer.call(:apply_resolution, {session_id, saga_id, payload, hash})` to both. Idempotent on `(session_id, hash)`; re-call returns ACK if already applied.
3. Both agents ACK apply → session GenServer commits `pg_resolve_session` AND UPDATEs saga to `status='committed'` in a single PG transaction.

Crash-restart recovery: session GenServer init reads any pending saga rows for its session_id. If status='applied' and PG commit not done → re-issue step 3 (idempotent at PG layer via `ON CONFLICT (session_id) DO NOTHING` on resolution INSERT). If status='reserved' and either agent doesn't have the saga active → UPDATE saga to `status='reverted'`, issue compensating `GenServer.call(:revert_reservation, {session_id, saga_id})` to both agents (idempotent: revert-of-non-existent-reservation is a no-op ACK).

Phantom-read mitigation (architect Lane-1 B2 issue 1): observers reading agent state via `audit.coordination_events` consumers OR `load_session_as_dict` MUST treat agent state as "in-flight" if a non-committed saga exists for the agent's active session. The query for "is this agent's state stable for downstream consumption" becomes:

```sql
SELECT NOT EXISTS (
    SELECT 1 FROM coordination.session_resolution_sagas
    WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
      AND status IN ('reserved', 'applied')
) AS is_stable;
```

Observers that don't accept stale-with-rollback semantics call this gate; observers that do (dashboard read paths) may proceed and re-read on the next polling cycle.

### B3.2 (architect B3 + reviewer BLOCK-1) — Comparator query specified; event_type co-lands

v0.1.1's B3 comparator was unspecified; the new event_type wasn't registered in `WAVE_0_EVENT_TYPES`.

**Binding spec:**
- **B3 prereq PR (single PR, co-lands ALL of):**
  - `db/postgres/migrations/0NN_identities_shadow.sql` — DDL for `core.identities_shadow` (schema = `core.identities` + `shadow_write_at TIMESTAMPTZ`).
  - `src/coordination_events.py` — adds constant `COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE = "coordination_failure.beam_python_boundary.shadow_divergence"` and adds it to `WAVE_0_EVENT_TYPES`.
  - `tests/test_coordination_events.py::test_event_type_constants_match_documented_set` — updated expected set.
  - `scripts/ops/wave-3-shadow-divergence-check.sql` — comparator query (below).
  - `scripts/ops/com.unitares.wave3-shadow-divergence-check.plist` — launchctl hourly trigger.
- **Comparator query (binding):**

  ```sql
  -- core.identities_shadow row count check
  SELECT
      i.agent_uuid,
      i.api_key IS DISTINCT FROM s.api_key AS api_key_diff,
      i.label IS DISTINCT FROM s.label AS label_diff,
      i.public_agent_id IS DISTINCT FROM s.public_agent_id AS public_id_diff,
      i.status IS DISTINCT FROM s.status AS status_diff,
      i.parent_agent_id IS DISTINCT FROM s.parent_agent_id AS lineage_diff,
      i.provisional_lineage IS DISTINCT FROM s.provisional_lineage AS provisional_diff,
      i.confirmed_at IS DISTINCT FROM s.confirmed_at AS confirmed_diff
  FROM core.identities i
  JOIN core.identities_shadow s USING (agent_uuid)
  WHERE i.api_key IS DISTINCT FROM s.api_key
     OR i.label IS DISTINCT FROM s.label
     OR i.public_agent_id IS DISTINCT FROM s.public_agent_id
     OR i.status IS DISTINCT FROM s.status
     OR i.parent_agent_id IS DISTINCT FROM s.parent_agent_id
     OR i.provisional_lineage IS DISTINCT FROM s.provisional_lineage
     OR i.confirmed_at IS DISTINCT FROM s.confirmed_at;
  ```

  `IS DISTINCT FROM` handles NULL semantics correctly. `shadow_write_at` is intentionally excluded. Each non-empty row emits one `shadow_divergence` event with payload `{agent_uuid, divergent_columns: [list]}`.

- **Load-amplification step before 7-day clock starts** (architect Lane-1 B3 final paragraph): shadow window includes ≥1 cycle of replay against captured production traffic at 2× rate (synthetic load via `scripts/ops/wave3-shadow-replay.sh`). The 7-day-zero-divergence clock starts AFTER replay completes with zero events.

### B5.2 (reviewer CONCERN-3) — Lease-plane Phase B is operator criteria-eval, not a PR

v0.1.1 §B5 said "Phase B amendment ships as separate PR." Reviewer: `surface-lease-plane-v0.md` §6.1 already specifies Phase B promotion criteria for any surface_kind, §6.2 specifies the promotion mechanism as "single config flag flip; no code change."

**Binding spec:** B5 is reduced to: operator evaluates `resident:/` against §6.1 criteria. If satisfied → flip the flag (no PR, no RFC amendment). If unsatisfied (e.g., <14 days advisory telemetry on residents at evaluation time) → the unmet criterion becomes the timer, not a Wave 3 RFC blocker. This reduces the named prereq PR count from 6 to 5 (verifier-confirmed).

### B6.2 (architect B6 + reviewer BLOCK-2) — Cache invalidation via Redis pub-sub

v0.1.1 §B6 endorsed two independent caches with no invalidation. Reviewer traced the actual Python write path at `phases.py:1314-1325`; architect read this as the substrate-tax pattern recreated.

**Binding spec:**
- Python `phases.py` baseline write path (find write site near 1314-1325 in current master; line numbers will drift) gains a fire-and-forget call to `_publish_baseline_invalidation(agent_id)` after `db.save_agent_baseline(...)`. Helper publishes to Redis channel `governance:baseline:invalidate` with payload `{agent_id, written_at, source: "python"}`.
- BEAM session GenServer init subscribes to `governance:baseline:invalidate`. On message receive: invalidate the in-state cached baseline for the named agent_id. Next observation triggers PG re-fetch (per B6's per-first-observation pattern, re-applied post-invalidate).
- Redis pub-sub down: BEAM falls back to per-update PG re-fetch (substrate-tax shape acknowledged in this fallback path; the fallback is bounded by Redis-down event duration, not steady-state).
- Python's `_baseline_cache` continues with its existing LRU eviction; both caches now converge on PG via the invalidation channel.

This explicitly rejects v0.1.1's "two independent caches accepted" posture and chooses cache-coherence-via-pub-sub. Acknowledged trade-off: every baseline write adds a Redis publish (cheap, fire-and-forget); BEAM gains a Redis subscription (one per BEAM application, not per GenServer).

### B7.2 (architect B7 + reviewer CONCERN-1) — Redis feature-flag with cache + pub-sub invalidation; Elixir-side timeout fallback

v0.1.1 §B7 had Python `asyncio.wait_for` contamination in BEAM spec; per-request Redis read regression; no Redis-down posture.

**Binding spec:**
- Both BEAM and Python read flag value at startup, cache locally per-process. Subscribe to Redis channel `governance:feature_flag:invalidate` for invalidation messages (payload: `{key, new_value, written_at}`).
- Python: subscription via existing Redis client; invalidation handler updates in-process cached value.
- BEAM: subscription via Redix Pub-Sub with `Process.send/3` to the GenServer holding the cached value. BEAM's timeout posture: `GenServer.call(flag_server, :get_strict_mode, 100)` with try/catch; on `:exit, :timeout` fall back to last-known-good cached value.
- Redis-down: both runtimes use last-known-good cached value (from prior Redis read or env-var bootstrap default). Health check at `/health/deep` adds a `redis_pubsub_lag` row; lag >60s emits a `coordination_failure.redis_pubsub_lag` event (this event_type also lands in the B3 prereq PR's WAVE_0_EVENT_TYPES update).
- Bootstrap: env var as default before first Redis read succeeds. Once Redis reachable, env var becomes init-only (per v0.1.1).

### B8.2 (architect Lane-1 B8 + reviewer CONCERN-5 + reviewer CONCERN-2) — Flip recommendation to (ii); JSON snapshot single-writer

v0.1.1 §B8 recommended (iii) GenServer-process-registry on the assumption of permanent single-BEAM-node. Architect: parent roadmap line 554 names MCP transport as a Post-Wave-3 candidate "if MCP SDK gate closes" — multi-node BEAM is a real possibility within Wave 3's lifetime; (iii) requires a coordinated re-port if multi-node ships. Reviewer: (iii) lacks idempotency under crash-restart. Reviewer CONCERN-2: JSON snapshot write race.

**Binding spec:**
- **Recommendation flipped to (ii) SELECT FOR UPDATE** on `core.dialectic_sessions` row at the start of any phase-mutating message handler. Releases on transaction commit. Safer default: doesn't break under multi-node BEAM. (iii) becomes the optimization, taken later if profiling shows row-level lock contends.
- Verify (ii) safety against the `updated_at` trigger at `db/postgres/schema.sql:157` (architect noted FOR UPDATE + trigger + concurrent reads can deadlock under PG MVCC; council should confirm trigger doesn't acquire its own conflicting locks before B8.2 (ii) is final).
- **JSON snapshot single-writer:** during shadow window, BEAM does NOT write `data/dialectic_sessions/<session_id>.json`. Python continues. Post-flip: BEAM writes, Python stops. The shadow-window flag is a boot-time config on BEAM (`UNITARES_DIALECTIC_BEAM_WRITES_JSON=0` during shadow, default `1` post-flip). No `-beam.json` suffix; no merge step; single writer always. Operator tooling reading the file path is undisturbed.
- B8 saga idempotency under crash-restart: covered by B2.2's PG saga state (above). (iii) recommendation drop makes the crash-restart concern moot — SELECT FOR UPDATE releases on transaction abort, no in-flight state survives across the restart boundary.

### C2.2 (verifier DRIFT #3) — Disconfirmer (B) anchor explicitly TBD; Wave 0 step 5 prereq added

v0.1.1 §C2 anchored disconfirmer (B) thresholds to "lease-plane Phase A measured p50/p99." Verifier: `audit.coordination_events` has zero rows across all partitions; lease-plane has never written to it. The anchor was performative.

**Binding spec:**
- v0.1.2 explicitly acknowledges: lease-plane Phase A latency baseline does not exist as of 2026-05-08. v0.1.1's claim of anchoring was the third bias signature in this RFC.
- **New Wave 0 step 5 prereq PR:** lease-plane Phase A latency instrumentation. Adds `coordination_failure.beam_python_boundary.lease_plane_request` event_type (informational, not failure) emitted by Python lease-plane client on every request to `127.0.0.1:8788`, with payload `{endpoint, method, status_code, elapsed_ms}`. Runs for ≥14 days before disconfirmer (B) thresholds can be set.
- Disconfirmer (B) text amended: "p50 < lease-plane Phase A measured p50 × 2 over 14-day window AND p99 < lease-plane Phase A measured p99 × 3 over 14-day window. **Phase A baseline computed from `audit.coordination_events` rows produced by Wave 0 step 5 instrumentation; if step 5 has not produced ≥14 days of data at Wave 3 implementation start, Wave 3 halts on missing measurement.**"
- Note: this adds the 6th prereq PR back (the 5th if you count B5.2 reduction), bringing total to 6 prereq PRs. Operator runway implication captured by disconfirmer (F).

### C7.2 (architect Lane-3 §C7) — Reconcile prose vs diagram

v0.1.1 §C7 prose said "structurally 2 boundary crossings per process_agent_update call" but diagram shows 4.

**Binding spec:** §C7 prose corrected — "Per call: up to 4 boundary crossings worst-case (dialectic-touching + governance_core math), 2 best-case (no dialectic + no governance_core math). Disconfirmer (B) budget at 4× per-crossing cost is correctly worst-case-anchored." The diagram is correct; the prose count was wrong.

### C13.2 (reviewer CONCERN-4) — Timestamp masking spec

v0.1.1 §C13 had no masking for non-deterministic fields. 100% parity gate would fail on every timestamp.

**Binding spec:** golden-capture fixture-comparison test masks any JSON key matching the regex `(.*_at|.*_time.*|.*_ms|server_time|processing_time_ms|elapsed_ms|server_time|created)`. Masking applies before equivalence comparison; masked fields are excluded from byte-identity check. Capture script (`scripts/dev/wave3-capture-goldens.sh` — also new in C13 prereq PR) applies the same masking when saving goldens. Specific known fields documented inline so future field additions to handler responses are caught at capture-time; if a handler adds a new non-deterministic field that doesn't match the regex, the golden capture script fails noisily (lint-style assertion).

### C-F.2 (architect Lane-2 (F)) — "Sacrificed" defined

v0.1.1 (F) had honor-system gate; "sacrificed" undefined.

**Binding spec:** "Sacrificed" defined as: calendar-week slip on any of {paper deadline, fellowship application, HLH, R2 Phase 2 gate} exceeds 25% of the original deadline window OR the operator's written go-decision document explicitly notes the slip and accepts it. Artifact: `docs/proposals/wave-3-go-decision-2026-MM-DD.md` written by operator at gate; document includes a §"Calendar reasoning" section enumerating each of the four named items with current slip vs original target. Without the document, gate (F) is unsatisfied.

### CA-prime.2 (architect Lane-2 (A′)) — Partial-fix joint behavior worked example

v0.1.1 (A′) collapsed (A)+(C) but didn't work through partial-fix cases.

**Binding spec:** Worked example added as §0 footnote: "Example: PR #3 lands during Wave 3 implementation window, brings p99 to 3.0s (between current ~5.0s and threshold 2.0s). ODE share rises from ~50% to ~65% because the non-ODE part shrank. (A′.1) — ODE share above 60% — fires; (A′.2) — p99 below 2.0s — does not fire. **(A′.1) firing alone halts Wave 3 implementation;** the rising ODE share specifically indicates the remaining floor is not in the layer Wave 3 ports. Operator may override with written analysis but the default is halt." This establishes (A′.1) as the dominant gate when ODE share rises, regardless of (A′.2).

### §8.2 — Criterion 6 escape hatch removed

v0.1.1 §8 criterion 6 said "operator decides whether Wave 3 still closes or whether the next port should be reconsidered" post-shipment.

**Binding spec:** Criterion 6 rewritten — "**(A′.2 / in-place-fix gate)** if any Python in-place fix shipped during implementation window brings `process_agent_update` p99 below 2.0s before Wave 3 implementation reaches canary-100%, Wave 3 halts. The 'operator decides post-shipment' framing is removed; the gate fires pre-canary-100%, not post-shipment." Sunk-cost protection eliminated.

### Master HEAD update + housekeeping

- Verifier DRIFT #2: master moved during this session from `b34dd7ad` to `c575f30c`. The collision check earlier in session noted `a15aadc5` between (master continued advancing while council ran). RFC's stated base hash is informational only; no rebase required for a docs-only branch.
- Verifier DRIFT #1: branch age string in v0.1.1 §B8 ("17h old") is now ~24-27h. Annotated as point-in-time.
- B8 v0.3.2 retirement: parent roadmap will gain a one-line index entry "Wave-N RFC authors: enumerate session-keyed coordination surfaces explicitly per Wave 3 §B8 pattern" — preserves the cross-RFC visibility v0.3.2 provided. Lands in a separate small commit on `wave-3-rfc-draft` immediately following this fold.

### Updated prereq PR count

| # | PR | Source | Status |
|---|-----|--------|--------|
| 1 | `core.identities_shadow` migration + `shadow_divergence` event_type + `WAVE_0_EVENT_TYPES` update + comparator query + launchctl trigger | B3.2 | named |
| 2 | Redis feature-flag reader (BEAM + Python) + pub-sub invalidation | B7.2 | named |
| 3 | `governance_core/coordination_events_helpers.py::make_boundary_payload` + Elixir-side equivalent | C8 (v0.1.1, unchanged) | named |
| 4 | IPUA pin integration test (`tests/integration/test_identity_path2_ipua_pin_pipeline.py`) | C12 (v0.1.1, unchanged) | named |
| 5 | Golden-capture fixture + capture script + masking spec + parity test | C13.2 | named |
| 6 | Wave 0 step 5 lease-plane Phase A latency instrumentation (≥14d data before disconfirmer (B) anchors) | C2.2 (NEW) | named |
| 7 | `coordination.session_resolution_sagas` migration + saga state machine | B2.2 (NEW) | named |
| 8 | `phases.py` baseline-write `_publish_baseline_invalidation` helper + Redis publish | B6.2 (NEW) | named |

Eight prereq PRs (B5.2 confirmed not a PR). Operator runway implication: per disconfirmer (F), if these eight + their council passes consume more than (Wave 1 elapsed × 3) calendar-weeks, halt and re-evaluate.

### Council recommendation for v0.1.2

**Operator decision (next):**
- **(α′)** Schedule third council pass on v0.1.2; if it returns clean, proceed to implementation prereq PRs in dependency order.
- **(β′)** Treat v0.1.1+v0.1.2 cumulative weight as exceeding the threshold architect named; do v0.2 redraft now from a fresh-doc baseline that incorporates everything but doesn't carry v0.1 historical record.
- **(γ′)** Halt Wave 3 RFC at v0.1.2 pending external pre-condition (e.g., disconfirmer A′.1 fires when ODE profile lands, or runway-check fires F).

Author recommends (α′) — the v0.1.2 fold addresses every named convergent finding; bias-persistence is acknowledged but not load-bearing for the v0.1.2 spec itself; one more council pass confirms whether the cycle closes or v0.2 redraft becomes mandatory.

If third council finds a fifth bias signature (any new "claimed-anchor" or "defends-status-quo-on-substrate" finding in v0.1.2 specifically), the discipline is to escalate to (β′) v0.2-redraft-from-scratch.

---

## V0.1.1 AMENDMENT 2026-05-08 — council fold (architect / reviewer / live-verifier)

**Read this with v0.1 body below.** Three council lanes ran in parallel after v0.1-draft was committed. Findings: architect 3 BLOCK / 4 CONCERN / 2 NIT; reviewer 2 BLOCK / 5 CONCERN / 1 NIT; verifier 39 VERIFIED / 7 DRIFT / 4 REFUTED / 2 SOURCE_ONLY. Architect bottom-line was "v0.2 redraft, not v0.1.1 amendment-fold" — the fold below is structurally heavy enough to be a redraft-in-amendment-form for the four sections architect named (§0, §2 invariant 5, §5.3, §4) plus reviewer's two BLOCKs (§2 invariant 6, §3 Surface H), with the remaining CONCERNs added inline. **Where v0.1.1 conflicts with v0.1 body below, v0.1.1 governs.**

### Author bias acknowledgment (operator-relevant)

Architect's lanes 1 and 3 named the documented author bias (`feedback_substrate-migration-status-quo-bias.md`) as actively shaping v0.1's §0 and §5.3. That diagnosis is accepted: v0.1's disconfirmer (A) was structurally a post-hoc footnote (BLOCK §0-1), and v0.1's `_has_recently_reviewed` "KEEP TOGETHER" classification used "PG round-trip dominates" — the exact status-quo reasoning the bias predicts (BLOCK §5-1). The bias-positive diagnosis is more important than the specific findings; v0.1.1 corrects the named cases but operator should treat the whole RFC with the bias caveat in mind.

### B1 (architect §0-1) — Disconfirmer (A) ODE-floor MUST be a pre-implementation hard gate

V0.1's §8 criterion 4 read "if the floor IS the ODE, Wave 3 closes as a structural success but operator-acknowledged user-visible-metric miss." That is sunk-cost protection masquerading as objectivity. Wave 1 RFC's §C1 fold (parent roadmap line 132-137) requires Wave 1's exit-criteria authorship to gate on ODE profile result; Wave 3 must do the same — and stronger, because Wave 3 is the largest blast-radius port.

**Binding spec:** ODE profile against the still-Python `governance_core/phase_aware.py` and `governance_core/stability.py` math path lands BEFORE Wave 3 implementation starts. If the profile shows the per-turn `process_agent_update` floor (defined as the asymptotic minimum p99 over a 7-day production sample) is dominated by ODE compute (>60% of the floor attributable to `governance_core/` synchronous math), Wave 3 halts and roadmap re-opens. The current §8 criterion 4 is replaced: ODE profile is a Go-gate prerequisite, not a post-hoc check.

### B2 (architect §2-1) — Cross-agent commit protocol for invariant 5

V0.1's §2 invariant 5 said "internal-message at the *session* GenServer." That's the topology, not the contract. The session GenServer must coordinate two agent GenServers (paused + reviewer); `execute_resolution` (`resolution.py:74-75`) writes to BOTH agents' state (status→active, paused_at=None). Session-GenServer serialization doesn't atomically span both agents.

**Binding spec:** Wave 3 implements the session resolution as a **session-supervisor saga**: the session GenServer atomically owns both per-agent GenServers' write-side mailboxes during the SYNTHESIS→RESOLVED transition window. Implementation pattern:
1. Session GenServer issues `GenServer.call(:reserve_for_session_resolution, session_id)` to both per-agent GenServers; both must ACK before proceeding. Either NACK rolls back (compensating action: release reservation on the ACK-ed side, leave session in SYNTHESIS).
2. Session GenServer issues `GenServer.call(:apply_resolution, resolution_payload)` to both per-agent GenServers; both must complete the agent-state mutation in their mailbox handler.
3. Session GenServer commits PG row (`pg_resolve_session`) only after both agents ACK the apply step. PG commit failure triggers compensating reverts on both agent GenServers.

This is a 2PC variant on the session-supervisor pattern. The session-keyed GenServer becomes a transactional coordinator, not just a serializer. Test: two concurrent `submit_synthesis(agrees=True)` calls from paused + reviewer must produce exactly one `pg_resolve_session` write and exactly one set of agent-state mutations on each side.

### B3 (architect §3-1) — Surface D divergence-detection contract

V0.1's §3.1 row D prescribed shadow→flip→dual-write→BEAM-only without naming what divergence-detection runs during shadow. PG `ON CONFLICT` is data-survival not divergence-detection.

**Binding spec:** During the shadow window, BEAM writes to a parallel `core.identities_shadow` table (DDL: identical schema to `core.identities` plus a `shadow_write_at TIMESTAMPTZ` column). A comparator query runs hourly via launchctl (`com.unitares.wave3-shadow-divergence-check`) joining `core.identities` and `core.identities_shadow` on `agent_uuid`, emitting `coordination_failure.beam_python_boundary.shadow_divergence` events for every row where the two writers produced non-equivalent state (excluding `shadow_write_at` itself, which is expected to differ). Shadow window cannot close until 7 consecutive days of zero divergence events. The migration that adds `core.identities_shadow` ships as a separate PR before Wave 3 implementation begins; same pattern for `core.agents_shadow`.

### B4 (architect §5-1) — `_has_recently_reviewed` reclassified as BEAM coordination

V0.1's §5.3 KEPT `_has_recently_reviewed` (`reviewer.py:55-119`) Python-side because "PG round-trip dominates." That reasoning is exactly status-quo bias: it ignores the Python→BEAM→Python boundary cost that Wave 3 introduces.

**Binding spec:** `_has_recently_reviewed` ports to BEAM as part of the dialectic session-keyed GenServer's reviewer-selection coordination. The PG round-trip remains (BEAM does Postgrex query directly), the boundary crossing disappears. If a future profile shows the Postgrex query is dominantly slower than asyncpg under realistic concurrency (which is itself a substrate-disconfirmer signal — see §0 v0.1.1), reconsider; the default classification is port-to-BEAM.

### B5 (architect Open-1) — §4 recommendation flipped to option (α) lease-plane Phase B

V0.1's §4 recommended option (β) PG advisory lock over option (α) extending lease-plane Phase B. Architect: option (β) introduces a coordination mechanism residents have no precedent for (lock leakage on connection death, contention with PG vacuum, observability gap, 50ms hard latency floor under contention), where option (α) extends an existing pattern Sentinel/Vigil/Chronicler/Steward already learn for `dialectic:/`.

**Binding spec:** §4 recommendation flips to **option (α)**: amend `surface-lease-plane-v0.md` to open a `resident:/` Phase B promotion window aligned with Wave 3's canary advance schedule. The Phase B amendment ships as a separate PR (against the lease-plane RFC) before Wave 3 implementation begins. Residents (Sentinel, Vigil, Chronicler, in-process Steward) update fail-closed-on-deny semantics in a window that precedes Wave 3 cutover by ≥7 days, with their own per-resident PR + test pass. Option (β) is preserved in v0.1 §4 below as an explicit alternative the council passed on; if the lease-plane Phase B amendment hits substantive resistance (e.g., resident maintainers reject in review), option (β) is the documented fallback.

### B6 (reviewer BLOCK-1) — Invariant 6 baseline cache: BEAM owns own GenServer-state cache, PG-fetched per agent on first BEAM observation

V0.1's §2 invariant 6 left the decision open. Reviewer: `_baseline_cache` (`governance_core/ethical_drift.py:418`) is a Python module-level OrderedDict; BEAM as a separate OS process has its own memory space. There is no shared cache across the boundary.

**Binding spec:** Wave 3 BEAM handler dispatch maintains its own per-BEAM-GenServer baseline cache as part of agent GenServer state. On first observation of a new agent, BEAM fetches baseline from PG (`get_baseline_or_none` becomes a Postgrex query); subsequent observations within the same BEAM GenServer's lifetime hit the in-state cache. Python's `_baseline_cache` continues to exist for the still-Python compute paths (governance_core math callers); the two caches are independent and PG is the authoritative replica for both. Anomaly detection at `phases.py:856-899` is therefore not silently degraded — BEAM has its own baseline available the moment the GenServer is initialized for that agent. Cost: an extra PG query per agent per BEAM-process lifetime (bounded; not per-request). This is an explicit choice over options (a) "every preload crosses boundary" and (c) "anomaly detection excluded from BEAM port"; both rejected (a for boundary cost, c for surface scope creep).

### B7 (reviewer BLOCK-2) — Surface H reclassified as Redis feature-flag, not direct-flip

V0.1's §3.1 Surface H said "Direct flip — config-only. Env var change applies to both sides at restart." Reviewer: BEAM and Python read config at different times (BEAM at OTP start, Python per-request via `config.governance_config`); a 1–30 second window of disagreement during plist reloads is a real operational footgun on a security gate.

**Binding spec:** Identity honesty mode (`identity_strict_mode`, `ipua_pin_check_mode`) moves to a shared Redis feature-flag key (`governance:feature_flag:identity_strict_mode`, `governance:feature_flag:ipua_pin_check_mode`) read per-request by both BEAM and Python. Default value matches current env-var default; the env var becomes the bootstrap default, the Redis key is the runtime-mutable source. Single-key change applies atomically to both runtimes within Redis read latency (~ms, not seconds). Migration: ship the Redis-flag-reader before Wave 3 implementation; Python reads Redis-then-env-fallback during the migration window; once both sides read Redis, the env var becomes init-only.

### C1 (architect §0-2) — Disconfirmers (A) and (C) collapsed

V0.1 §0 listed (A) ODE-floor and (C) in-place Python remediation as orthogonal disconfirmers. Both probe "where does the per-turn time go?" — false plurality.

**Binding spec:** §0 v0.1.1 collapses (A) and (C) into a single **disconfirmer (A′) "user-visible-metric headroom"** with two measurement paths: (A′.1) ODE profile shows >60% of `process_agent_update` p99 floor in `governance_core/` math (per B1 above); (A′.2) any in-place Python fix shipped during Wave 3 implementation window brings `process_agent_update` p99 to <2.0s (threshold per C2 below) without porting. Either path firing halts Wave 3.

### C2 (architect §0-3) — Disconfirmer thresholds anchored

V0.1 §0 cited p50<50ms / p99<250ms boundary cost and <1.5s p99 process_agent_update without source-citation.

**Binding spec:** Updated thresholds with anchors:
- **Boundary cost** (disconfirmer B): p50 < lease-plane Phase A measured p50 × 2; p99 < lease-plane Phase A measured p99 × 3. Lease-plane Phase A actuals are read from the Wave 0 channel before Wave 3 implementation; the multipliers reflect Wave 3's heavier per-call payload (full request marshalling vs lease ack).
- **`process_agent_update` p99** (disconfirmer A′.2): < 2.0s (corrected from v0.1's invented 1.5s). Anchor: per `project_locked-update-overhead-fix.md`, current per-turn overhead is ~5.0s post-#372; PR #3 deferred-pending-benchmark targets sub-5s. 2.0s reflects "in-place fixes brought it to 40% of current" — a defensible bar; tighter thresholds need a documented Python-fix path that gets there.

Council should challenge any threshold without an anchor sentence. v0.1's invented numbers are now explicit.

### C3 (architect §0-4) — Disconfirmer (F) opportunity-cost

V0.1 §0 listed only technical disconfirmers. Wave 3 is "the largest single port the BEAM-footprint roadmap names" and Kenny's solo-founder calendar carries paper v6.9.1, HLH instrumentation, R2 Phase 2 telemetry-gated work, +#371 executor_loop_died, locked_update PR #3, and ongoing dispatch/Lumen work.

**Binding spec:** New **disconfirmer (F) "opportunity cost":** if Wave 3 implementation (after RFC closes) is projected to consume more than (Wave 1's elapsed time × 3) calendar-weeks before the 21-day production-traffic exit criterion can begin, and any of {paper deadline, fellowship application, HLH, R2 Phase 2 gate} would be sacrificed by that consumption, the operator's call is "right port, wrong time" → defer. This is a non-technical disconfirmer, intentionally; it does not have a runtime measurement, but the operator's go-decision must explicitly check it at the gate.

### C4 (architect §0-5) — Disconfirmer (G) dialectic-quality regression

V0.1 §0 missed dialectic-quality regression. §5's two HTTP boundary calls per session (synthesize + select_reviewer) could regress dialectic resolution rates without showing up in the Python suite.

**Binding spec:** New **disconfirmer (G) "dialectic-quality":** during canary, dialectic session-resolution rate (resolved / (resolved + failed + escalated) over a 14-day window) must not regress more than 5% against the pre-Wave-3 baseline. Reviewer-reassignment rate must not increase more than 20%. Either breach halts canary advance. Baseline is computed from the trailing 30 days of pre-Wave-3 production sessions and pinned in §"Exit criteria" before Wave 3 implementation begins.

### C5 (architect §3-3) — Surface A ContextVar marshalling cost

V0.1 §3.1 row A said ContextVars stay Python at the dispatch boundary without acknowledging that BEAM-side cache lookups need the context shipped per-request. **Binding spec:** Surface A description amended — "BEAM message handler receives a marshalled context-payload as part of the wire envelope on each request; payload size and serialization cost are part of disconfirmer (B)'s budget. Wave 3 implementation must measure the marshalled context-payload's bytes-per-request and add it to the boundary-cost dashboard."

### C6 (architect §5-2) — Endpoint shapes specified; collapse to single endpoint

V0.1 §5.6 named two endpoints (`/v1/dialectic/synthesize`, `/v1/dialectic/select_reviewer`) without schemas, idempotency, or timeout posture.

**Binding spec:** Collapsed to a single endpoint **`POST /v1/dialectic/compute`** with `mode: "synthesize" | "select_reviewer"` discriminator. Request schema:
```json
{
  "mode": "synthesize" | "select_reviewer",
  "session_id": "<UUID, idempotency key>",
  "round": <int, idempotency key for synthesize>,
  "input": {...mode-specific bounded compute input...}
}
```
Response schema:
```json
{
  "result": {...mode-specific output...},
  "elapsed_ms": <int>,
  "cache_hit": <bool>
}
```
Idempotency: `(session_id, round, mode)` tuple is the cache key; same input within a 60s window returns cached result. Timeout: BEAM applies `asyncio.wait_for(..., timeout=2.0s)` equivalent; on timeout, BEAM emits `coordination_failure.beam_python_boundary.beam_to_python_request_failed` with `error_class="timeout"` and fails the synthesis round (does not retry — retry policy lives in the session GenServer's saga, not at the boundary call).

### C7 (architect Open-2) — §10.1 post-Wave-3 boundary topology

V0.1's §10 listed what stays Python without diagramming the steady-state topology. The Wave 3 close-state has structurally 2 boundary crossings per `process_agent_update` call.

**Binding spec:** New §10.1 added (in this amendment block as the binding spec, since v0.1 §10 lacks it):

```
MCP request
    ↓
Python MCP transport (unmarshal request envelope)
    ↓ [boundary crossing 1: Python→BEAM via Ports/HTTP]
BEAM handler dispatch (route, identity middleware, dialectic coordination)
    ↓ [boundary crossing 2: BEAM→Python for governance_core math + LLM SDK calls]
Python governance_core compute (ODE, stability, phase_aware) + LLM SDK
    ↑ [boundary crossing 3: Python→BEAM with compute result]
BEAM continues handler dispatch (audit emit, response shape)
    ↑ [boundary crossing 4: BEAM→Python for response serialization]
Python MCP transport (marshal response envelope)
    ↓
MCP response
```

Per-call: 4 boundary crossings, of which 2 are request-shape-marshalling (1 + 4) and 2 are compute round-trips (2 + 3). Disconfirmer (B)'s budget is 4× per-crossing cost. If real-world topology compresses (e.g., handlers that don't touch governance_core math skip crossings 2+3), budget per-call accordingly. The disconfirmer (B) budget MUST be set against measured-not-estimated per-crossing cost from lease-plane Phase A baseline (per C2).

### C8 (reviewer CONCERN-4) — `make_boundary_payload` enforcement helper

V0.1 §6.2 said "lint failure" on null `error_class` without naming an enforcement mechanism.

**Binding spec:** Add `governance_core/coordination_events_helpers.py::make_boundary_payload(endpoint, method, error_class, status_code, elapsed_ms) -> dict` that raises `ValueError` on None/empty/missing `error_class`. All `coordination_failure.beam_python_boundary.*` emissions MUST go through this helper; direct dict construction is prohibited. PR-time check: grep for the event_type constants in non-helper code is a CI lint failure. Same pattern applies to BEAM emissions (Elixir-side helper module).

### C9 (architect Open-6) — Audit-coordination-events wiring is in-scope, named explicitly

V0.1's §5.4 said "Wave 3 wires dialectic state transitions to `audit.coordination_events`" but §10 didn't acknowledge this as in-scope.

**Binding spec:** §10 in-scope list updated (v0.1.1 governs over v0.1): dialectic state-transition wiring to `audit.coordination_events` is part of Wave 3 because BEAM is the new writer and the table was greenfield (per Wave 2 #4, PR #403 created the dual-write fix; dialectic was not previously wired). If wiring proves invasive, it ships as a separate post-Wave-3 PR — but Wave 3 RFC owns the design.

### C10 (reviewer CONCERN-1) — Invariant 2 names `build_fork_context`/onboard-payload as accepted-staleness consumer

V0.1's invariant 2 (thread_id/node_index relaxation) said "document tolerant consumers" without naming them. Reviewer: `db/mixins/thread.py:84-101` `get_agent_thread_info` → `src/thread_identity.py:129` `build_fork_context` → `src/services/identity_payloads.py:268,275` is a non-tolerant consumer (agent's own onboard response).

**Binding spec:** §2 invariant 2 amended: "ACCEPTED INCONSISTENCY WINDOW — agent's onboard response may report `node_index` from the previous session's PG-persist if the fire-and-forget `_persist_thread_identity_async` (`phases.py:670-693`) hasn't completed before the next onboard call. Operator-acknowledged risk; mitigation = Wave 3 BEAM session-keyed GenServer can synchronously persist within the session-resolution saga (per B2) since the saga already crosses the boundary. Decision deferred to implementation: re-tighten thread_id persist as part of B2's saga, OR document the staleness window in the onboard response itself."

### C11 (reviewer CONCERN-2) — §3.2 rollback gap closed via 503 circuit-breaker

V0.1 §3.2 had a zero-request gap between BEAM-stop and Python-load that produced 500s on the synchronous path.

**Binding spec:** Python MCP transport gains a circuit-breaker: when proxying to BEAM yields connection-refused or timeout, transport returns HTTP 503 with body `{"ok": false, "error": "governance_temporarily_unavailable", "reason": "handler_dispatch_unavailable"}` and a `Retry-After: 5` header. Clients (Watcher, Sentinel, SDK consumers) gain matching retry-on-503 logic before Wave 3 cutover. Rollback procedure step 3 amended: "stop BEAM writes first; transport returns 503 during the gap; restore Python writers; transport resumes 200." Gap is bounded by operator's plist-load latency; clients absorb via retry. Stop sign #7 v0.1.1: 503 rate during cutover/rollback exceeding 1% of requests for >60s halts the procedure.

### C12 (reviewer CONCERN-3) — IPUA pin integration test

V0.1 cited the contract test at `tests/test_identity_path2_ipua_pin.py` lines 219-250, but the test pins helper-level behavior, not the request-pipeline integration.

**Binding spec:** Add a new integration test `tests/integration/test_identity_path2_ipua_pin_pipeline.py` that drives the full `handle_onboard_v2` call path with `agent_id` in `arguments` and asserts the strict-mode passthrough invariant holds end-to-end. Test lands BEFORE Wave 3 implementation begins (prerequisite PR). Wave 3 BEAM identity middleware port reuses the same integration test against the BEAM-side dispatch entry.

### C13 (reviewer CONCERN-5) — §7.1(c) "byte-identical" defined; golden-capture fixture is a prereq PR

V0.1 §7.1(c) said "byte-identical" without testable definition. Reviewer: existing tests do not capture full serialized response bytes; serialization order, whitespace, float-vs-int precision categories not covered.

**Binding spec:** §7.1(c) amended:
- "Byte-identical" defined concretely as: same JSON field-set, same value types (int stays int, float stays float — no implicit coercion), same nested dict ordering (Python 3.7+ dict insertion-order preserved), same float precision (12 decimal digits). String-byte equality is NOT required.
- A golden-capture fixture lands as a prereq PR before Wave 3 implementation: `tests/fixtures/wave3_response_golden/` containing 50+ captured responses across the full handler surface (process_agent_update, identity, onboard, dialectic_*, knowledge_*, observe, etc.) under deterministic input fixtures. Fixture-comparison test `tests/integration/test_wave_3_response_parity.py` runs the same fixture inputs against BEAM-side dispatch and asserts the JSON-equivalence definition above against each golden response.
- Pre-cutover gate: 100% golden-response parity on the captured fixture set. Failure of any golden response halts cutover.

### N1 (architect Open-4) — §0 framing tone

V0.1's §0 final paragraph claimed "§Exit criteria makes Wave 3's go-decision conditional on disconfirmers." Architect: §8 didn't deliver this strongly until v0.1.1's B1/C1/C2/C3/C4 corrections.

**Binding spec:** §0 framing rewritten in v0.1.1's §0-replacement (next subsection). The new framing acknowledges v0.1's overclaim and structures the gate strength to match the rhetoric.

### N2 (architect §3-2 + reviewer NIT-1) — §3.2 duplicate

V0.1 has `### 3.2 Rollback procedure (named)` at line 109 and again at line 123. Mechanical error.

**Binding spec:** Below this amendment block, the second §3.2 (lines 123-131 in v0.1) is to be deleted. Recorded here for traceability; the mechanical edit lands as a separate small commit immediately following this fold.

### B8 (operator-surfaced post-council, 2026-05-08) — `src/mcp_handlers/dialectic/session.py` was missing from v0.1 §5 entirely

The Explore-agent dialectic survey covered `dialectic_protocol.py`, `dialectic/handlers.py`, `dialectic/resolution.py`, `dialectic/auto_resolve.py`, `dialectic/reviewer.py` — but not `session.py`. v0.1's §5 cited `get_session_lock` at the call-site (`handlers.py:1184`) without tracing to its implementation in `session.py:55`. Parallel local branch `docs/beam-footprint-v0.3.2-dialectic-session-inventory` (1 commit, 17h old, not pushed) had already enumerated session.py at parent-roadmap altitude as a "bookkeeping addition" to Wave 3's surface inventory.

**Resolution:** v0.3.2 retired as superseded by this RFC at the right altitude — Wave 3 RFC owns Wave-3-level surface inventory; parent roadmap doesn't need to enumerate session.py separately if the wave RFC does. This v0.1.1 amendment subsection adds the inventory at wave-RFC altitude.

**Surface inventory for `src/mcp_handlers/dialectic/session.py` (514 lines):**

| Surface | Line | Description | Wave 3 BEAM mapping |
|---------|------|-------------|----------------------|
| `_SESSION_LOCKS` per-session asyncio.Lock dict | 51 | In-process locks, dict-of-locks guarded by `_SESSION_LOCKS_DICT_LOCK` (52). Implements invariant 5's serialization (B2 above). | **REPLACED** by session-keyed GenServer mailbox (per B2 saga). The asyncio.Lock disappears; BEAM message-handler-per-session is the new serialization. |
| `get_session_lock` accessor | 55-68 | Lazy lock creation + dict-acquire | **DELETED.** Replaced by GenServer process registry. |
| `_SESSION_LOCKS_DICT_LOCK` | 52 | Dict-of-locks guard | **DELETED** with its dict. |
| `ACTIVE_SESSIONS: Dict[str, DialecticSession]` | 31 | Process-local in-memory live-session cache | **REPLACED** by per-session GenServer state. The dict-keyed-by-session-id pattern → registry of GenServer PIDs (Erlang `:via, Registry, ...`). |
| `_SESSION_METADATA_CACHE` (60s TTL) | 35-36 | Per-agent in-session lookup cache | **REPLACED** by per-agent GenServer state on the BEAM side. Same pattern as Surface G agent metadata in §3. |
| `save_session` PG writes | 179 (`pg_resolve_session`), 185 (`pg_update_phase`) | Phase + resolution PG persists | **PORT** to BEAM. Postgrex queries from session-keyed GenServer message handler. JSON snapshot path (env-gated) becomes BEAM-side file write or stays Python (compute-only) per implementation pass. |
| `load_all_sessions` startup load | 213-242, asyncpg awaits at 223 (`pg_get_active_sessions`) + 230 (`pg_get_session`) | Reconstructs ACTIVE_SESSIONS from PG on startup | **PORT** to BEAM startup phase. BEAM application boot iterates active sessions, spawns one GenServer per session under a DynamicSupervisor. |
| `load_session` lazy reload | 244-259, asyncpg await at 247 | Single-session reload from PG | **PORT** to BEAM. Becomes GenServer init message. |
| `load_session_as_dict` raw asyncpg | 261-342, raw `compatible_acquire(db._pool)` + `conn.fetchrow` (273) + `conn.fetch` (282) | Read-only fast path for dashboard | **PORT** to BEAM Postgrex with named query. Two queries become two Postgrex round-trips (parity preserved). |
| `list_all_sessions` raw asyncpg | 352+ (per v0.3.2: line 433) | Admin/dashboard listing | **PORT** to BEAM Postgrex. |
| `verify_data_consistency`, `run_startup_consolidation` | 344, 348 | Utility consistency checks | **PORT** as part of BEAM startup phase. |

**Critical Wave 3 precondition surfaced by session.py — multi-process lock topology.** The `get_session_lock` docstring explicitly names this (lines 47-50): "Single-process MCP deployment makes this sufficient. If/when multi-process lands, this gets replaced by a postgres advisory lock or SELECT FOR UPDATE pattern." Wave 3 IS the multi-process landing. The RFC must specify which:

- **(i)** PG advisory lock per session_id at the start of any phase-mutating message handler (`pg_try_advisory_lock(hashtext(session_id))` with timeout). Same pattern as v0.1 §4 option (β) but at session-id granularity instead of agent-id.
- **(ii)** PG `SELECT … FOR UPDATE` on `core.dialectic_sessions` row at the start of any phase-mutating message handler. Row-level lock; releases on transaction commit.
- **(iii)** GenServer-process-registry serialization is sufficient if BEAM is single-OS-process (one BEAM application owning all session GenServers). Multi-process BEAM (e.g., a second BEAM node for HA) needs (i) or (ii).

**Recommendation pending council:** **(iii) for single-BEAM-node, with (ii) as the documented escalation path.** Current `wave-3-rfc-draft` posture assumes single BEAM node; multi-node is post-Wave-3 candidate per parent roadmap §"Post-Wave-3 candidates." If multi-node BEAM ever ships, (ii) is preferred over (i) because PG advisory locks have observability gaps the row-level lock doesn't. Recorded here so the question isn't lost.

**Storage tier addition to §5.4 (v0.1.1 governs):** the storage table in v0.1's §5.4 enumerated `core.dialectic_sessions`, `core.dialectic_messages`, `audit.coordination_events`. Add: `data/dialectic_sessions/<session_id>.json` (env-gated by `UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT`, default ON per `session.py:71-75`) — JSON snapshot file, write-only audit trail. Wave 3 BEAM either continues writing it (Elixir File.write) or migrates this to PG-only (the env var becomes default-OFF). Decision deferred to implementation pass.

### Verifier corrections — REFUTED + DRIFT errata table

| # | v0.1 cite | Correct value | Verifier finding |
|---|------------|----------------|--------------------|
| 1 | `coordination_events.py:35` (§5.4) | Module docstring lines 1-22 (`audit.coordination_events` first named at line 1, "stop or proceed to BEAM port" at 6); first SQL usage at line 233 | REFUTED: line 35 is `from uuid import UUID, uuid4` |
| 2 | `session.py:440-530` Surface F read (§3) | `session.py:769-797` `lookup_onboard_pin` (with `_PIN_REDIS_TIMEOUT = 0.5s` at line 28) | REFUTED: line 440 is a return annotation, off ~330 lines |
| 3 | `dialectic_protocol.py:162` numpy import (§§2, 5 prose) | Line 163: `import numpy as np` | REFUTED: line 162 is blank |
| 4 | Surface E continuity-token HMAC payload field names (§3) | Actual fields: `v`, `opv`, `sid`, `aid` (not `agent_uuid`), `mf`, `ch` (not `chh`), `iat`, `exp` | REFUTED: two field names wrong, two fields missing in description |
| 5 | `handlers.py:335-412` `_apply_reviewer_reassignment` (§5.1) | `handlers.py:368-412`. Lines 335-366 are `_validate_explicit_reviewer_candidate` (different function) | DRIFT |
| 6 | `phases.py:670-707` persist helper (§2 invariant 2) | `phases.py:670-693` `_persist_thread_identity_async`; 696+ is `_persist_inferred_purpose_async` | DRIFT |
| 7 | `resolution.py:667-1088` Surface D PATH 3 write (§3) | `resolution.py:950-1116` (PATH 3 starts at 950) | DRIFT |
| 8 | `background_tasks.py` `load_agent_metadata` Surface G write (§3) | Function does not exist; correct name is `background_metadata_load` at `background_tasks.py:343` | DRIFT (REFUTED on name, real symbol) |
| 9 | `agent_auth.py:309-377` `require_registered_agent` Surface G read (§3) | `agent_auth.py:309-515` (function ends at 515) | DRIFT |
| 10 | `dialectic_protocol.py:464-524` `DialecticSession.__init__` (§5.1) | `__init__` body 464-512; `_generate_session_id` 513-524 | DRIFT |
| 11 | `persistence.py:175-200` Redis SETEX with NX (§3 Surface C write) | Range is correct for the Redis write block; `NX` flag (`mint_guard`) is in inner `_cache_session_redis_write` at 206+ | SOURCE_ONLY (range correct, NX in inner function) |
| 12 | `context.py:131-147` "(read)" classification (§3 Surface A) | 141-147 contains `update_context_agent_id` (a writer) | SOURCE_ONLY (description mismatch, not error) |

The corrections are errata — they don't change RFC structure; they correct cite values. They land as a separate small commit immediately following this fold.

### v0.1.1 §0 replacement — Falsifying-evidence question, corrected

> **What evidence would update us away from porting handler dispatch + identity middleware + dialectic resolution to BEAM?**

v0.1's §0 was author-bias-contaminated (per the acknowledgment above). v0.1.1's disconfirmer set:

- **(A′) User-visible-metric headroom** (collapsed from v0.1's A + C). Two paths: (A′.1) ODE profile lands pre-implementation per B1, shows >60% of `process_agent_update` p99 floor in `governance_core/` math → halt. (A′.2) Any in-place Python fix during implementation window brings `process_agent_update` p99 below 2.0s without porting → halt.
- **(B) Boundary cost ≥ substrate tax removed.** `coordination_failure.beam_python_boundary.*` channel (Wave 2 #3, wired in §6) shows sustained per-call boundary cost p50 ≥ lease-plane-Phase-A-p50 × 2 OR p99 ≥ lease-plane-Phase-A-p99 × 3 over 14-day Wave 1 window. Anchored to measured Phase A baseline per C2.
- **(D) MCP SDK gate reverses.** Hands-on spike on `mcp_elixir_sdk` 1.0.1 or `hermes_mcp` 0.14.1 shows production-disqualifying failure (broken-on-Anthropic-streaming, MCP-spec drift, no maintainer responsiveness). Doubles disconfirmer (B)'s budget per C7's 4-crossing topology.
- **(E) State-ownership cutover structurally unsafe.** Identity middleware port surfaces irreducible per-request semantics that can't be moved to GenServer state without replicating coordination at the boundary.
- **(F) Opportunity cost.** Per C3: Wave 3 implementation projected calendar-weeks > (Wave 1 elapsed × 3) AND any of {paper deadline, fellowship, HLH, R2 Phase 2} sacrificed → defer.
- **(G) Dialectic-quality regression.** Per C4: dialectic resolution rate regresses >5% OR reviewer-reassignment rate increases >20% over 14-day canary window → halt.

§8 Exit criteria are amended in v0.1.1's §8 amendment (next): each disconfirmer has a measurable check that gates the Go decision. The "structural success but user-visible miss" escape hatch is removed.

### v0.1.1 §8 replacement — Exit criteria, evidence-bearing

1. Wave 2 has closed (satisfied per Wave 2 handoff 2026-05-08).
2. Handler dispatch on BEAM has served production governance MCP traffic for ≥21 days continuous.
3. Wave 0 channel shows zero coordination-class incidents attributable to handler dispatch over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
4. **(B1 / A′.1 hard gate)** ODE profile lands BEFORE Wave 3 implementation starts; result shows <60% of `process_agent_update` p99 floor in `governance_core/` math. Failure → halt and roadmap re-opens.
5. **(B / boundary cost gate)** `coordination_failure.beam_python_boundary.*` p50 < lease-plane-Phase-A-p50 × 2 AND p99 < lease-plane-Phase-A-p99 × 3 over 21-day window. Sustained breach halts.
6. **(A′.2 / in-place-fix gate)** if any Python in-place fix shipped during implementation window brought `process_agent_update` p99 below 2.0s without porting, operator decides whether Wave 3 still closes or whether the next port should be reconsidered.
7. **(F / opportunity cost gate)** operator's go-decision explicitly checks F at the gate; written acknowledgment that none of {paper, fellowship, HLH, R2 Phase 2} was sacrificed for Wave 3 calendar.
8. **(G / dialectic quality gate)** session-resolution rate regression ≤5% AND reviewer-reassignment rate increase ≤20% vs pre-Wave-3 baseline (baseline pinned in §"Exit criteria" prior to implementation).
9. Operator-led behavioral parity: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved per C13's golden-capture definition).
10. ExUnit + Python + integration + golden-response-parity test classes all green at gate.

### Council recommendation

Architect's bottom line was "v0.2 redraft, not v0.1.1 amendment-fold." This amendment block is a redraft-in-amendment-form for the four sections architect named (§0, §2 invariant 5, §5.3, §4) plus reviewer's two BLOCKs (§2 invariant 6, §3 Surface H), with CONCERNs added inline. **Operator decision:**

(α) Proceed with v0.1.1 as binding, schedule second council pass on v0.1.1, then implementation. Recommended.
(β) Treat v0.1.1 as transitional and v0.2-redraft to a clean document before any further council work.
(γ) Halt Wave 3 RFC at v0.1.1 pending external pre-condition (e.g., ODE profile lands and disconfirmer A′.1 fires; or operator-runway-check fires F; or B5 lease-plane Phase B amendment hits resistance).

Author recommends (α): the four BLOCKs are corrected in this amendment, the document gains structural baggage but stays auditable, and operator can decide on implementation start once a second-council confirms the v0.1.1 shape.

---

## V0.1 (preserved as historical record below — superseded by V0.1.1 amendment above where they conflict)

Status from v0.1-draft, 2026-05-08. Pre-council. Author: claude-wave3-rfc (UUID `326aadf6-66d0-4a92-a6e1-255ca8db3cdc`). The v0.1 sections below are kept verbatim for traceability; the v0.1.1 amendment above is binding.

---

## §0 Falsifying-evidence question — and the honest answer

Per `feedback_substrate-migration-status-quo-bias` (the documented author bias — "I reliably resist substrate migrations across sessions") and the symmetric warning in `beam-footprint-roadmap-v0.md` §"Why Read A, not Read B" ("Substrate-migration enthusiasm is the *prompt* to write this roadmap, not *evidence* that justifies escalation"), the question this RFC opens with is:

> **What evidence would update us away from porting handler dispatch + identity middleware + dialectic resolution to BEAM?**

If the answer is "nothing" — if no observable Wave 1/Wave 2 outcome could change the migration call — then the RFC is a ratification, not a decision. The honest discriminators below are what the Wave 3 Go/No-Go gate (§Exit criteria) is built on.

### The five disconfirmers, in descending strength

**(A) The locked-phase floor IS the ODE.** Per auto-memory `project_locked-phase-floor-is-the-ode.md` (2026-05-04): "process_agent_update locked-phase floor is the ODE" — surrounding awaits already cheap; don't swing at them again. If profiling Wave 1's BEAM Sentinel post-cutover, plus an ODE-trace on the still-Python `governance_core/phase_aware.py` and `governance_core/stability.py` math path, shows the per-turn floor is dominated by ODE compute rather than handler-dispatch / identity / dialectic coordination, then porting the layer Wave 3 names doesn't shrink p99. The right answer in that world is "profile and optimize the ODE; leave the handler layer alone." This is the strongest single falsifier because Wave 3 explicitly leaves `governance_core/` Python (per roadmap §"Out of scope for this roadmap"), so a port that doesn't touch the floor cannot move the user-visible metric.

**(B) Wave 0's boundary channel shows BEAM↔Python round-trip cost ≥ substrate tax it removes.** Stop sign #4 (V0.3.1): "Wave 0 instrumentation post-Wave-1 shows Ports/HTTP boundary accruing >1 distinct workaround pattern → boundary design is wrong, halt before Wave 3." Sharper measurable form: if the `coordination_failure.beam_python_boundary.*` channel (Wave 2 #3, wired in this RFC) shows a sustained per-call boundary cost p50 ≥ 50ms or p99 ≥ 250ms over the 14-day Wave 1 window, the port is net-negative — Wave 3 trades a Python coordination tax for a cross-runtime boundary tax of comparable magnitude.

**(C) In-place Python remediation closes the user-visible gap.** Per `project_locked-update-overhead-fix.md`: PR #1 (#362) + PR #2 (#372) shipped 2026-05-05; per-turn overhead ~6.5s → ~5.0s; PR #3 deferred pending benchmark; if still >5s, substrate question strengthens. **Inverse:** if the deferred PR #3 (or any post-Wave-1 in-place Python fix) brings p99 of `process_agent_update` to under a stated threshold (proposed: <1.5s p99 across the Wave 1 window) without porting, the substrate-tax claim weakens to "real but not user-blocking" — which is not enough to motivate Wave 3's blast radius.

**(D) MCP SDK gate reverses on hands-on evaluation.** V0.3.1 §B5 dissolved the gate based on hex.pm presence (`mcp_elixir_sdk` 1.0.1, `hermes_mcp` 0.14.1, plus six others at non-trivial versions). If a hands-on spike on either of the named SDKs shows broken-on-Anthropic-streaming, MCP-spec drift, no maintainer responsiveness, or other production-disqualifying failure, the gate re-closes. In that world Wave 3 must keep the MCP transport Python — which means a Python→BEAM→Python sandwich for every request, with two boundary crossings per call instead of one. That doubles disconfirmer (B)'s threshold and likely flips it negative.

**(E) State-ownership cutover is structurally unsafe.** If the identity-middleware survey (§3 below) finds that any identity-binding state has irreducible per-request semantics that can't be moved to GenServer state without re-creating the same coordination problem at the boundary — e.g., a contextvar that holds a live `asyncio.Event` whose synchronization is needed across the boundary — then Wave 3 needs a more invasive port than estimated, and the boundary cost from disconfirmer (B) gets a structural component on top of the network component.

### What the answer is NOT

- Wave 1 simply "shipping without incident" is **not** confirmation. Wave 1's exit criterion is 14 days × zero coordination-class incidents AND ODE profile completed before exit-criteria authorship (V0.3.1 §C1). A clean Wave 1 with bad boundary numbers is disconfirmer (B).
- "BEAM is the right substrate philosophically" is **not** evidence. It's a prior. Per the symmetric bias warning, enthusiasm and resistance are both errors.
- Operator preference is **not** evidence. V0.3 closed the *destination* on operator decision; the *Wave 3 Go gate* is a separate, evidence-bearing call.

### How this RFC handles disconfirmation

§"Exit criteria" below makes Wave 3's go-decision conditional on the disconfirmers above being measured-and-not-triggered, not on the calendar. If disconfirmer (A), (B), (D), or (E) is present at the gate, Wave 3 halts and the roadmap re-opens.

---

## §1 Roadmap-level scope (inherited from beam-footprint-roadmap-v0.md Wave 3)

Verbatim from parent doc §"Wave 3":

- Handler dispatch (the `@mcp_tool` decorator's wrapper, per-tool routing, response shaping) ports to BEAM. The MCP transport layer itself stays Python (per §"MCP SDK gate") and proxies to BEAM after request unmarshalling.
- Identity middleware (`src/mcp_handlers/middleware/identity_step.py`, the session-context contextvar chain, agent_id resolution, label resolution) ports to BEAM. This is the largest single coordination surface in governance MCP today and the highest-leverage substrate-tax elimination.
- Dialectic resolution (`src/mcp_handlers/dialectic/`) ports to BEAM. The dialectic engine's *reasoning logic* — what makes a thesis converge, the dialectic-knowledge-architect's substantive work — stays Python (it's compute, not coordination) and is called from BEAM. The coordination layer (session lifecycle, quorum tracking, condition resolution, audit emission) ports.
- Out of scope: `governance_core/`, Watcher, the LLM SDK call paths inside handlers (those stay Python and are called from BEAM via Ports).

§5 below splits dialectic explicitly per V0.3.1 §C2.

---

## §2 Lock-invariant inventory (per V0.3.1 §B2)

The lock surface is `StateLockManager.acquire_agent_lock_async` (`src/state_locking.py:286-423`), a per-agent file-based lock that brackets the `execute_locked_update` phase chain in `src/mcp_handlers/updates/phases.py`. Eleven invariants identified, three named in V0.3.1 §B2 (kept for traceability) plus eight folded from the survey.

For each: file:line / classification / Wave 3 GenServer mapping decision (**internal-message** = synchronous step inside the agent's GenServer mailbox; **explicit-relax** = inherit PR #362-style eventual consistency with named tolerant consumer; **PG-anchored** = explicit lock at DB layer, GenServer just serializes access).

| # | Invariant | File:line | Classification | Wave 3 mapping (proposed; council reviews) |
|---|-----------|-----------|----------------|---------------------------------------------|
| 1 | api_key PG/cache reconciliation: PG-create succeeds → cache.api_key syncs to PG; PG-create fails → cache is truth | `phases.py:723-798` (esp. 745, 778, 792, 798; comment naming at 773-776) | Critical read-then-write-then-validate under lock; three-way (UUID, api_key, cache) | **internal-message** — must stay atomic inside agent GenServer; api_key auth desync risk if relaxed |
| 2 | thread_id / node_index monotonic advancement on `active_session_key` change | `phases.py:822-851` (relaxation comment 834-837; persist helper 670-707) | Read-modify-write under lock; PG fire-and-forget post-PR #362 | **explicit-relax** — inherit PR #362 posture; in-memory `ctx.meta` is process-local truth, PG is cross-process replica. Document the tolerant consumers (cross-process thread-lineage observers). |
| 3 | previous_void_active snapshot: read-once inside lock before ODE, used post-lock for CIRS emission decision | `phases.py:800-807` capture; `phases.py:1125-1137` use | Atomic snapshot-capture under lock; out-of-lock guard | **internal-message** — must remain a single mailbox message; do NOT re-read post-ODE |
| 4 | Monitor lifecycle consistency: metadata fetched (line 743/768/789) and monitor lookup (line 803) must refer to the same agent under one lock acquire | `phases.py:743-798, 803-807, 880-923` | Cache-coherence assumption (in-memory dict lookups) | **internal-message** — corollary of (1); BEAM must keep meta+monitor reads in the same handler frame |
| 5 | Dialectic session lock exclusion: SYNTHESIS→RESOLVED phase transition must serialize across two `submit_synthesis(agrees=True)` calls or both finalize_resolution calls race; second `pg_resolve_session` overwrites the first | `dialectic/handlers.py:1179-1190` (named comment) | Lock-protected critical section; CROSS-AGENT (not per-agent-state) | **internal-message** at the *session* GenServer (not the agent GenServer); requires session-keyed routing in dispatch — see §5 |
| 6 | Baseline preload: `get_baseline_or_none(agent_id)` loads once per process (lines 812, 817); cached in-process; no cross-process refresh | `phases.py:809-820, 856-899` | In-process single-writer cache; no lock | **PG-anchored** — read on miss; **decision required:** if BEAM runs N agents per process, validate that baseline cache is per-agent-keyed (not per-process-keyed), or move to PG-on-every-read |
| 7 | Monitor state snapshot for enrichment vs Phase 5 anomaly drift: pre-ODE snapshot (596-602) used for ODE input; post-ODE re-read (1143-1147) used for CIRS emission; the two must NOT cross-contaminate | `phases.py:536-602, 1143-1147, 1156-1164, 1203-1223` | Read-snapshot-before-mutate; post-mutation re-read isolation | **internal-message** — single GenServer call carries both snapshots; BEAM must not split into two messages |
| 8 | Metadata cache-PG eventual consistency contract (corollary of 2 + thread_id persistence): in-memory writer within lock, PG replica written out-of-band; in-memory NEVER rolled back to match stale PG | `phases.py:823-851, 928-943, 670-707` (named in 834-837 + persist docstring) | Single-writer-then-broadcast | **explicit-relax** — formalize as cross-layer contract; document as Wave 3 design rule, not just a phases.py local choice |
| 9 | api_key mutable reference under lock (corollary of 1): `ctx.meta.api_key` mutations (745, 778, 792, 798) must complete before ODE call (905) which receives `api_key` param | `phases.py:745, 778, 792, 798, 905-911` | Mutable reference coherence during lock hold | **internal-message** — covered by (1)'s framing; flag here for completeness |
| 10 | CIRS void_active transition guard (corollary of 3): post-ODE void state vs pre-ODE captured snapshot determines emission; comparison MUST use captured value, not re-read | `phases.py:800-807, 1125-1137` | Captured-state guard for out-of-lock decision | **internal-message** — covered by (3); flag for completeness |
| 11 | Agent-state mutation ordering: agent_state immutable under lock (read-only for ODE input); result immutable post-ODE (outcome events read but don't mutate) | `phases.py:635-668, 709-920, 1010-1240` | Read-only vs write-once isolation across phases | **architectural-pattern** — Wave 3 GenServer message handler must enforce this by structure, not by lock |

**Decisions inheriting forward:** invariants 1, 3, 4, 5, 7, 9, 10 collapse into "must be a single GenServer mailbox message handler on the BEAM side." Invariants 2, 6, 8 are explicit-relax (with documented consumers). Invariant 11 is structural — the BEAM message handler's pure-functional shape preserves it for free if the dispatch is single-message-per-update.

**Open question for council:** invariant 5 (dialectic session lock) is cross-agent. Wave 3's GenServer topology must include a *session-keyed* GenServer (one per active dialectic session) above the per-agent GenServers. This is named in §5 below; the lock-invariant inventory surfaces it here.

---

## §3 State ownership and rollback during transition (per V0.3.1 §B3 + §B4)

> *Survey in progress (Explore agent). Filled below when complete.*

Per V0.3.1 §B3, every Wave RFC must cover, for each migrating surface:

- **Single source of truth per state surface** during the transition window (Python-side, BEAM-side, or shadow-mode dual-write with one canonical reader).
- **Cutover semantics** — direct flip / shadow-mode-then-flip / dual-write-then-converge. Default presumption: shadow mode for ≥1 cycle of meaningful traffic before flip.
- **State format compatibility** — any on-disk or shared-DB schema MUST be backwards-compatible with the Python reader OR a documented migration shim is provided. Default: BEAM does NOT modify the Python-readable format until Wave-N+1 explicitly changes the canonical reader.
- **Rollback procedure** — named launchctl/systemd command sequence, state-file restoration step, and explicit acknowledgement of which side keeps writing during the rollback window.

### 3.1 Surface inventory (state-ownership matrix)

Identity middleware decomposes into eight state surfaces. Source-cited columns from `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/identity/{resolution,persistence,session}.py`, `src/mcp_handlers/support/agent_auth.py`, `src/mcp_handlers/context.py`, and `src/background_tasks.py`.

| # | Surface | File:line (read) | File:line (write) | Single source of truth | Lock posture | BEAM port strategy | Cutover semantics |
|---|---------|-------------------|---------------------|------------------------|---------------|----------------------|---------------------|
| A | ContextVars (10 declarations; 4 identity-bearing — `_session_context`, `_mcp_session_id`, `_session_resolution_source`, `_pin_match_scope`) | `context.py:131-147` (`get_context_*`) | `context.py:86-114` (`set_session_context`, `update_context_agent_id`) | Process memory only (async-task-local) | None — request-scoped, never contended | **Stays Python (per-handler-task-local)** at the dispatch boundary. BEAM message handler threads request-context explicitly through GenServer state. ContextVars never cross the boundary. | **Direct flip** — ContextVars are ephemeral. BEAM owns identity-context at message-handler entry; Python's ContextVar layer sits above the BEAM call boundary in the still-Python MCP transport |
| B | Sticky transport binding cache (3-layer: in-memory dict / Redis / PG fallback) | `identity_step.py:289-298` (cache hit), `:292` (Redis recovery, 0.5s timeout) | `identity_step.py:98-157` (`update_transport_binding` + fire-and-forget Redis), `:230-248` (invalidate) | In-memory dict when populated; Redis when recovered; no PG anchor | Fire-and-forget to Redis; in-memory dict mutation under no lock | **BEAM owns** as per-process GenServer state (or stays Python — both work). Pure optimization layer. | **No shadow needed** — drop in-memory cache → next request falls through to Redis → falls through to session resolution. Zero data risk |
| C | Session→UUID Redis cache (`sticky:{ip_ua_fingerprint}:{mcp_session_id}` keys) | `resolution.py:430-470` (PATH 1 Redis lookup) | `persistence.py:175-200` (`_cache_session` Redis SETEX with `NX`) | PostgreSQL canonical; Redis is speed cache | NX flag on Redis writes (idempotent); 2h TTL | **Shadow-mode-then-flip.** Python writes both Redis + PG during warmup; BEAM reads. After ≥1 cycle of meaningful traffic, BEAM writes both, Python reads via context/HTTP fallback. | **Shadow ≥1 cycle then flip.** Rollback: re-enable Python writes, BEAM HTTP-read-only. ≤1-request consistency window at flip moment |
| D | Session→UUID PG canonical (`core.identities` + `core.agents` upsert on PATH 3 fresh mint) | `resolution.py:667-1088` (PATH 3) | `resolution.py` (`db.upsert_identity`, `db.upsert_agent`) | PostgreSQL — authoritative on fresh mint | `ON CONFLICT` clause (last-writer-wins at PG layer); in-memory `_session_identities` dict with S21-a `mint_guard=True` collision guard | **BEAM owns the upsert.** PG INSERT/UPDATE moves into GenServer message atomicity. Python REST-reads via boundary call on cache miss. | **Shadow ≥1 cycle then flip then dual-write window then BEAM-only.** Three-stage. Rollback: re-enable Python upsert, BEAM read-only. PG ON CONFLICT absorbs flip-moment race |
| E | Continuity token (cryptographic; HMAC over agent_uuid + chh + exp + iat + sid + opv) | `session.py:176-220` (extraction; no I/O) | `session.py` (`create_continuity_token` at onboard) | Cryptographic material — token string IS the source | None — stateless | **Stays Python OR moves to BEAM** — orthogonal substrates. Tokens issued by either are valid on both | **No rollback contract** — orthogonal credential layer |
| F | Onboard PIN (Redis-keyed `onboard_pin:{ip_ua_fingerprint}` with model scoping; IPUA pin-check enforces agent_id-as-proof per `project_ipua-pin-agent-id-proof.md`) | `session.py:440-530` (Redis lookup, 0.5s timeout) | `session.py` (`set_onboard_pin` SETEX, 30m TTL) | Redis (TTL 30m); IPUA pin treats `agent_id` claim as proof — invariant locked by contract test per memory | 0.5s read timeout (anyio mitigation) | **Shadow ≥1 cycle then flip.** Same pattern as (C). Validation logic mirrors per-runtime; the IPUA pin invariant CANNOT be relaxed without contract-test breakage. | **Shadow then flip.** PIN write moves to BEAM; PIN validation can stay either side |
| G | Agent metadata cache (`mcp_server.agent_metadata[uuid]` — label, public_agent_id, status, paused_at) | `agent_auth.py:59-134` (`compute_agent_signature`), `:151` (status check), `:309-377` (`require_registered_agent` label→UUID iteration) | `background_tasks.py` (`load_agent_metadata` broadcast → background load from PG) | PostgreSQL `core.agents` is canonical; in-memory dict is read-side cache | Fire-and-forget background loader; no explicit lock; status check accepts stale (advisory not fail-closed) | **Boundary service.** PG-anchored. OTP gen_server watches PG for changes and publishes; both BEAM + Python subscribe via the same broadcast channel | **No rollback contract** — read-mostly, stale reads degrade gracefully. Both sides can subscribe in parallel |
| H | Identity honesty gates (PATH 0 bare-UUID-passthrough strict-mode + FALLBACK 2 handler auto-generation) | `identity_step.py:365-474`, `agent_auth.py:271-293` | Config env var (`identity_strict_mode()`, `ipua_pin_check_mode()`); broadcast `identity_hijack_suspected` event | Config (env var) — no state surface | None — config-driven | **BEAM mirrors config check** at the same dispatch entry point. Broadcast event channel stays Python until OTP event-bus integration is decided (out of Wave 3 scope) | **Direct flip** — config-only. Env var change applies to both sides at restart |

### 3.2 Rollback procedure (named)

Following the pattern proven by Wave 1 (Sentinel had `.sentinel_state.pre-beam-*` snapshot files; runtime checkout per landmine #1 in the Wave 2 handoff):

1. **Snapshot before flip.** For every PG table touched by the Wave 3 BEAM service, `pg_dump` the table-set (at minimum `core.identities`, `core.agents`, `core.dialectic_sessions`, `core.dialectic_messages`) into `~/backups/governance/wave-3-pre-cutover-<ISO8601>/`.
2. **Plist swap.** New plist `com.unitares.handler-dispatch-beam.plist` lives in `scripts/ops/`. Cutover flips the BEAM service on; rollback unloads the BEAM plist and reloads the Python-only `com.unitares.governance-mcp.plist`.
3. **Single writer during rollback.** Per-surface protocol from §3.1 columns: stop BEAM writes first, then restore Python writers. No period of dual-write to the same canonical surface during rollback.
4. **Schema rollback.** Any new migration shipped with Wave 3 MUST have a paired DOWN migration that restores the prior shape; tested on a `governance_test` snapshot before the cutover migration runs in production.
5. **Per-surface rollback windows** (from matrix above):
    - Surfaces A, E, F (config + crypto): instantaneous; no data window
    - Surfaces B, C, G (caches): ≤2h staleness window (TTL); zero data risk
    - Surface D (PG canonical): ≤1-request inconsistency window at flip moment; ON CONFLICT absorbs
    - Surface H (config gates): instantaneous on env-var revert

> *§3.2 duplicate (formerly at lines ~373-380 of v0.1) deleted per V0.1.1 §N2; canonical §3.2 is the immediately-preceding subsection. v0.1.1 §C11 also amends step 3 to add a 503 circuit-breaker.*

---

## §4 `resident:/` Phase B enforcement gate (per V0.3.1 §C3)

V0.3.1 §C3 stated: lease plane Phase A is advisory-only; Wave 3 handler dispatch requires Python MCP to stop accepting writes for an agent while its BEAM GenServer is mid-update. That's Phase B enforcement. **Phase B eligibility for `dialectic:/` opens 2026-05-16** per lease plane RFC; **no Phase B window is named for `resident:/` surfaces.**

Wave 3 either:

- **(α)** Opens a `resident:/` Phase B window via amendment to `surface-lease-plane-v0.md`, or
- **(β)** Specifies a different enforcement-grade boundary mechanism (e.g., per-agent advisory lock at PG layer, taken with `pg_try_advisory_lock(hashtext(agent_uuid))` at the start of any writing handler — fails fast if BEAM holds the lock).

**Recommendation pending council:** option (β). The lease plane was greenfield; the `resident:/` surface has live Python writers across `agents/sentinel/`, `agents/vigil/`, `agents/chronicler/`, and the in-process Steward. Opening a Phase B window forces every Python resident to learn fail-closed-on-deny semantics in the same window the BEAM cutover happens, which couples two large changes. Option (β) keeps the lease plane unchanged and adds a per-agent PG advisory lock that BEAM acquires on enter and releases on exit; Python writers attempt the same lock with a 50ms timeout and fail-fast (returning a 503-equivalent that the MCP transport surfaces as `governance_temporarily_unavailable`).

Decision deferred to council pass.

---

## §5 Dialectic stateful/stateless split (per V0.3.1 §C2)

The architect council's finding: dialectic is plausibly BOTH stateful-coordinating (resolution timing, participant lifecycle) AND stateless-computing (numerical synthesis math — `src/dialectic_protocol.py:162` imports numpy). This RFC splits it explicitly.

### 5.1 Coordination surfaces → BEAM GenServer

Session lifecycle, participant binding (paused_agent_id ↔ reviewer_agent_id), phase FSM transitions, lock-protected critical sections, audit emission. All port to a *session-keyed* GenServer (one per active session) that supervises the per-agent message handlers for invariant 5 (§2 lock inventory).

| File:line | Function | Why coordination |
|-----------|----------|-------------------|
| `dialectic_protocol.py:464-524` | `DialecticSession.__init__` | Session lifecycle init; phase setup; timeout constants per session_type |
| `dialectic_protocol.py:526-552` | `submit_thesis` | THESIS→ANTITHESIS transition; auth check (only paused agent); state lock point |
| `dialectic_protocol.py:554-585` | `submit_antithesis` | Reviewer auto-assign if none set; ANTITHESIS→SYNTHESIS transition; reviewer role lock |
| `dialectic_protocol.py:587-638` | `submit_synthesis` | Convergence check (`agrees=True`→RESOLVED); synthesis_round counter mutation; multi-participant coordination |
| `dialectic_protocol.py:781-897` | `finalize_resolution` | Resolution lifecycle closure; dual-signature canonical-payload-v2 coordination |
| `mcp_handlers/dialectic/handlers.py:55-63` | `_resolve_dialectic_agent_id` | Session ownership verification; auth boundary |
| `mcp_handlers/dialectic/handlers.py:130-177` | `check_reviewer_stuck` | Circuit-breaker timeout (2h antithesis); session state validity; phase-gated |
| `mcp_handlers/dialectic/handlers.py:241-334` | `_build_dialectic_actionability` | Actionability state-machine assembly; next-valid moves per phase |
| `mcp_handlers/dialectic/handlers.py:335-412` | `_apply_reviewer_reassignment` | Session state mutation under stuck-session recovery |
| `mcp_handlers/dialectic/handlers.py:414-635` | `handle_request_dialectic_review` | Session creation; PostgreSQL write (`pg_create_session` line 478) |
| `mcp_handlers/dialectic/handlers.py:897-985` | `handle_submit_thesis` | PG write (`pg_add_message` 910); phase transition (922); session lock |
| `mcp_handlers/dialectic/handlers.py:986-1147` | `handle_submit_antithesis` | Reviewer assignment if missing (1040); phase transition (1056); session lock |
| `mcp_handlers/dialectic/handlers.py:1148-1388` | `handle_submit_synthesis` | Convergence check (1206-1228); synthesis_round multi-round (1181); session lock — **invariant 5 critical section** |
| `mcp_handlers/dialectic/handlers.py:1389-1506` | `handle_reassign_reviewer` | Session update (`pg_update_reviewer` 1460) |
| `mcp_handlers/dialectic/resolution.py:18-196` | `execute_resolution` | Agent state mutation (status→active, paused_at=None at 74-75); condition application sequencing |
| `mcp_handlers/dialectic/auto_resolve.py:54-220` | `auto_resolve_stuck_sessions` | Periodic stuck-session detection; reviewer reassignment; status mutation (`awaiting_facilitation`) |
| `mcp_handlers/dialectic/reviewer.py:121-200, 255+` | `is_agent_in_active_session`, `select_reviewer` | Quorum-prevention via participant tracking; collusion gate via state reads |

### 5.2 Computation surfaces → stays Python, called from BEAM via boundary

Pure functions: signature math, similarity scoring, safety regex, condition merging. No state mutation, no lock, no I/O.

| File:line | Function | Why computation |
|-----------|----------|------------------|
| `dialectic_protocol.py:1077-1162` | `calculate_authority_score` | numpy sigmoid health-score, Jaccard similarity, weighted authority aggregation; pure function |
| `dialectic_protocol.py:640-657` | `_normalize_condition_terms`, `_semantic_similarity_terms` | Term extraction + Jaccard; pure |
| `dialectic_protocol.py:659-743` | `_merge_proposals` | Condition semantic matching (0.6 threshold); intelligent merge via term overlap; pure |
| `dialectic_protocol.py:746-779` | `_conditions_conflict` | Contradiction detection via regex + term-overlap heuristics; pure predicate |
| `dialectic_protocol.py:250-265` | `DialecticMessage.sign` | HMAC-SHA256; deterministic |
| `dialectic_protocol.py:350-410` | `Resolution.compute_signature`, `verify_signatures` | HMAC-SHA256 keyed MAC + `hmac.compare_digest`; pure crypto |
| `dialectic_protocol.py:899-986` | `check_hard_limits` | Safety regex validation + threshold checks on risk/coherence; stateless predicate |
| `mcp_handlers/dialectic/handlers.py:180-200` | `_read_proposed_conditions` | Fallback alias handling; pure input normalization |
| `mcp_handlers/dialectic/calibration.py` (imported 99-102) | calibration updates from session outcomes | Statistical correlation without lock; numeric aggregation |
| `mcp_handlers/support/condition_parser.py` (imported in resolution.py:13) | condition parsing/application | Numeric/text transformation; stateless |

### 5.3 Mixed/boundary cases — RFC author judgments

| File:line | Function | Judgment | Reason |
|-----------|----------|----------|--------|
| `dialectic_protocol.py:995-1031` | `check_timeout` | **SPLIT**: coordination wrapper (reads phase + session.created_at; gates FSM) calls a stateless `_compare_against_timeout(now, created_at, phase, timeout_constants)` predicate. Wrapper ports to BEAM; predicate stays Python. | Time-comparison itself is pure; FSM-phase decision is coordination |
| `mcp_handlers/dialectic/reviewer.py:55-119` | `_has_recently_reviewed` | **KEEP TOGETHER as coordination**, called from BEAM. PG query is the load-bearing part; collusion-prevention is quorum coordination. | Splitting saves nothing; PG round-trip is the cost |
| `mcp_handlers/dialectic/auto_resolve.py:32-51` | `_parse_timestamp` | **Stays Python utility** (helper to coordination caller) | Pure helper; not worth boundary cost |
| `dialectic_protocol.py:318-329` | `Resolution.hash` | **Stays Python utility** (called from coordination) | Cryptographic hash; pure |
| `dialectic_protocol.py:331-347` | `Resolution.canonical_payload` | **Stays Python utility** (called from coordination — load-bearing for v2 signing per C2026-05-06 NEW-2) | Pure data serialization; substrate-agnostic |
| `calculate_authority_score` (reviewer selection math) | (per §5.2 already classified as computation) | **Stays Python, called from BEAM as `/v1/dialectic/select_reviewer`** during session creation | Pure compute; no shared state; same shape as `/v1/dialectic/synthesize` |

### 5.4 Storage surfaces (unchanged by Wave 3 — Wave 3 inherits)

- `core.dialectic_sessions` (sessions FSM table; `phase`, `status`, `paused_agent_id`, `reviewer_agent_id`, `quorum_*` reserved-but-unimplemented fields). Wave 3 BEAM session-keyed GenServer reads/writes via boundary; on-disk schema unchanged.
- `core.dialectic_messages` (append-only message history; `message_type` ∈ thesis/antithesis/synthesis/system/quorum_vote/failed). BEAM appends via boundary; schema unchanged.
- `audit.coordination_events` (referenced `src/coordination_events.py:35`; not yet wired for dialectic state transitions). **Wave 3 wires dialectic state transitions to this table** as part of §6 boundary-event instrumentation.

### 5.5 Lifecycle FSM (unchanged shape; preserved in BEAM port)

States from `DialecticPhase` enum (`dialectic_protocol.py:166-182`) and `dialectic_sessions` CHECK constraint:

```
THESIS → submit_thesis() → ANTITHESIS
ANTITHESIS → submit_antithesis() → SYNTHESIS (round 1)
SYNTHESIS → submit_synthesis():
    agrees=True → RESOLVED (terminal)
    agrees=False AND round < max → SYNTHESIS (round N+1)
    round ≥ max → FAILED (terminal)
ANTITHESIS (if check_reviewer_stuck) → auto_resolve → FAILED OR new ANTITHESIS (reassigned reviewer)
ESCALATED — reserved (quorum_voting); not implemented; out of Wave 3 scope
```

Phase-enforcement guards (lines 535-536, 569-570, 601-602) prevent out-of-order submissions. Wave 3 GenServer must reproduce these guards as message-handler preconditions, not as wrapping locks.

### 5.6 Boundary protocol for dialectic compute calls

For functions classified as **computation** (§5.2 + §5.3), the BEAM coordination layer calls them via the same Ports/HTTP boundary the lease plane established. Two new Python-side endpoints:

- `POST /v1/dialectic/synthesize` — input: bounded compute (proposals, conditions, threshold); output: merged result; no PG side-effect.
- `POST /v1/dialectic/select_reviewer` — input: candidate pool + paused-agent context; output: ranked candidates with authority scores; no PG side-effect.

Both endpoints are wrapped in the standard `coordination_failure.beam_python_boundary.beam_to_python_request_failed` instrumentation per §6.

---

## §6 `coordination_failure.beam_python_boundary.*` call-site wire-up (Wave 3 measurability)

Per Wave 2 #3 (PR #408): the typed event constants `python_to_beam_request_failed` and `beam_to_python_request_failed` exist with documented payload shape `{endpoint, method, error_class, status_code, elapsed_ms}` but are unused. Wave 3 wires them at every call site that crosses the boundary, so exit criterion #3 is measurable.

### 6.1 Call-site enumeration (Wave 3 introduces these)

- BEAM handler-dispatch service → Python MCP transport (response shaping post-handler-execution): `beam_to_python_request_failed` on any non-2xx return from the Python transport, `python_to_beam_request_failed` on any non-2xx return from BEAM.
- BEAM identity middleware → Python `governance_core/` math calls (when an identity decision needs ODE input): `beam_to_python_request_failed` on Port/HTTP failure.
- BEAM dialectic GenServer → Python `/v1/dialectic/synthesize` (per §5.1): `beam_to_python_request_failed` on synthesize failure.
- BEAM handler-dispatch service → Python LLM SDK call paths (per Wave 3 out-of-scope: LLM SDK stays Python, called from BEAM): both directions instrumented.

### 6.2 Emission contract

Every emission MUST populate all five payload fields. Empty/null `error_class` is itself a lint failure (this is what made the existing `coordination_failure.*` events useful to grep). Reviewer check during PRs: `python_to_beam_request_failed` and `beam_to_python_request_failed` must appear in audit-events tests with concrete payloads, not just `mock.call(event_type=...)`.

### 6.3 Wave 0 query

A new query lands in `scripts/ops/wave-0-channel-report.sh` (or wherever the Wave 0 dashboard sources from) returning, over a stated window: count, p50 elapsed_ms, p99 elapsed_ms, error_class breakdown, by endpoint. This query is what disconfirmer (B) (§0) reads against.

---

## §7 Test strategy under migration (per V0.3.1 §C4)

V0.3.1 §C4 said: 8329-test Python suite cannot cover BEAM-side code or the cross-runtime boundary. Wave 1 added an ExUnit suite for BEAM Sentinel; Wave 3 extends this.

### 7.1 Acceptance test classes

**(a) Existing Python suite.** All ~8400+ tests in `tests/` remain the Python-side acceptance gate. Pre-cutover gate: full green.

**(b) ExUnit suite for BEAM handler-dispatch.** New `elixir/handler_dispatch/test/` (or whatever the project layout settles on). Tests at minimum:
- Driven test: fixture MCP-style request → BEAM dispatch → assert correct Python handler is invoked with correctly-marshalled args.
- Identity middleware test: fixture process_agent_update with parent_agent_id → assert lineage declaration writes to PG with the correct shape (matches what `src/mcp_handlers/middleware/identity_step.py` produces today).
- Dialectic GenServer test: fixture session lifecycle (create → join → quorum → resolve) → assert the same audit.events row sequence Python produces today.

**(c) Cross-runtime integration test.** A new `tests/integration/test_wave_3_boundary.py` (Python side) that drives the full pipeline: MCP request → Python transport → BEAM dispatch → Python compute (governance_core) → BEAM coordination → Python audit emit. Asserts response shape byte-identical to pre-Wave-3 Python-only path. This is the Wave 3 byte-for-byte parity gate from the parent roadmap.

**(d) Behavioral parity gate.** Per parent roadmap exit criterion #4: "Operator-led behavioral parity test: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved byte-for-byte, response shapes identical, error codes identical)." Operator-led, not just CI; this is the cutover-day check.

### 7.2 What the Python suite stays the gate for

The Python suite stays canonical for: governance_core math, LLM SDK call paths, watcher pattern matching, all "compute" surfaces. The BEAM ExUnit suite is canonical for: handler dispatch routing, identity middleware coordination, dialectic GenServer state transitions. The integration suite is canonical for the boundary itself.

### 7.3 Migration-window test bar

During the cutover window (BEAM service running but pre-canary-100%), failure of any test class halts the canary advance. Specifically:
- (a) green AND (b) green AND (c) green → canary advances per schedule.
- Any single failure → canary stops, root cause identified, fix lands as a separate PR with its own council pass.

---

## §8 Exit criteria (Go/No-Go for Wave 3 close)

Inherited from parent roadmap §"Wave 3 — Exit criterion" + amended for measurability against §0 disconfirmers:

1. Wave 2 has closed (its exit criteria all hold; per Wave 2 handoff 2026-05-08, this is satisfied).
2. Handler dispatch on BEAM has served production governance MCP traffic for ≥ 21 days continuous (longer window than prior waves because this is the largest blast-radius port).
3. Wave 0 channel shows zero coordination-class incidents attributable to handler dispatch over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
4. **Disconfirmer (A) check:** ODE profile data, gathered before Wave 3 close, shows the per-turn floor is not dominated by `governance_core/` math alone. If it is, Wave 3 closes as a structural success but operator-acknowledged user-visible-metric miss; roadmap re-opens.
5. **Disconfirmer (B) check:** `coordination_failure.beam_python_boundary.*` channel shows p50 boundary cost < 50ms and p99 < 250ms over the 21-day window. Sustained breach halts.
6. **Disconfirmer (C) check:** if the deferred locked_update PR #3 (or any other in-place Python fix) shipped during the Wave 3 implementation window and brought p99 of `process_agent_update` to <1.5s without porting, the operator decides whether Wave 3 still closes (port already shipped) or whether the next port should be reconsidered.
7. Operator-led behavioral parity test: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved byte-for-byte, response shapes identical, error codes identical).
8. ExUnit + Python + integration test classes (§7.1) all green at gate.

---

## §9 Stop signs (additive to parent roadmap §Stop signs)

Inheriting parent roadmap stop signs #1–#4, plus Wave-3-specific:

**Wave 3 stop sign #5:** Identity-middleware port surfaces a coordination shape that Wave 1+2 didn't expose — e.g., the contextvar chain holding live object references that don't survive the Port boundary cleanly. Halt before canary advance; reopen architecture before continuing.

**Wave 3 stop sign #6:** Dialectic split per §5 turns out to be ungratified — a function classified as "computation" mutates state across calls (a hidden statefulness). Re-classify, possibly re-split, before canary advance.

**Wave 3 stop sign #7:** `resident:/` Phase B enforcement (option α or β per §4) blocks legitimate Python writers (Sentinel, Vigil, Chronicler, Steward) at non-trivial rate during the canary window. Halt; revisit the boundary mechanism.

---

## §10 What Wave 3 deliberately does NOT do

- Does not port `governance_core/`. Math stays Python.
- Does not port the MCP transport layer. Per §"MCP SDK gate" — even with V0.3.1 §B5's hex.pm reality, transport stays Python until disconfirmer (D) is run hands-on.
- Does not port the LLM SDK call paths. Anthropic/OpenAI/Ollama call paths inside handlers stay Python, called from BEAM via Ports.
- Does not port Watcher. Single-shot LLM pattern matcher; no coordination shape.
- Does not modify the existing `lease_plane` schema. Wave 3's new state lives in either GenServer memory or new tables (`coordination` schema is reserved for future use; Wave 3 default is GenServer memory + existing PG tables).

---

## §11 Council pass — pending

Three lanes scheduled in parallel (per `feedback_design-doc-council-review.md` and `feedback_council-adversarial-prompt.md`):

- **dialectic-knowledge-architect** — adversarial on the falsifying-evidence section's completeness, the dialectic split's structural rigor, and the Wave 3 framing as a whole. Does §0 actually enumerate the disconfirmers honestly, or is it ratification dressed as inquiry?
- **feature-dev:code-reviewer** — adversarial on the implementation patterns: lock-invariant inventory completeness, state-ownership matrix correctness, the option-α-vs-β recommendation in §4, the test strategy in §7.
- **live-verifier** — adversarial on every named file:line, endpoint, field, table, plist, lease-plane Phase B date, and runtime claim in this RFC. Cross-checks against running governance-mcp + lease-plane + the audit.events schema.

Each lane's findings will be folded inline as a §V0.1.1 amendment block.

---

## §12 Open follow-on (not Wave 3 scope, surfaced for completeness)

- The substrate-tax bug class is structural to anyio + asyncio + asyncpg on a shared event loop (per `CLAUDE.md` §"Substrate Tax: anyio-asyncio Coupling"). Wave 3 dissolves it in the Wave 3 surfaces; the remaining Python surfaces (governance_core compute, LLM SDK paths, Watcher, MCP transport) still live on the same substrate. If Wave 3 closes successfully and post-Wave-3 measurement shows the bug class persisting in those surfaces, the operator decides per §"Post-Wave-3 candidates" whether to continue porting or pause.
