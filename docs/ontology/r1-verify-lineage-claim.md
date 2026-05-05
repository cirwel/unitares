# R1 — `score_trajectory_continuity` design spike

**Status:** Design doc, revision pass 3.
**Scope:** Plan row R1 (`docs/ontology/plan.md`). Produces: signature, plausibility model, implementation sketch including the series-reconstruction helper, and test-fixture plan.
**Author:** agent `8ae8cb4b-23d2-4b21-9906-b9993b4293d0` (claude_code), 2026-04-24.
**Revision history:**
- v1 (2026-04-24 morning) — one-shot draft; five-channel model, `verify_lineage_claim` naming. Dismissed as not implementable and carrying security-gate framing.
- v2 (2026-04-24 afternoon) — two-channel reduction (C1 trajectory DTW + C2 homeostatic composite); renamed `score_behavioral_continuity`. Reviewed by second council; council found the C1/C2 channels were themselves gated on agents explicitly uploading `TrajectorySignature.attractor` — both unavailable on the standard `process_agent_update` path. v2 also smuggled in a recursive-weight inconsistency (C2's internal 0.4/0.3/0.3 weights exempted from the "no weights until MI" rule applied at the outer level).
- v3 (2026-04-24 evening). Single-channel spec. Renamed `score_behavioral_continuity` → `score_trajectory_continuity` to match what the primitive actually measures. C1 retained via a new server-side helper that reconstructs per-dimension EISV series from `agent_states` rows — no agent-side cooperation required. C2/C3/C4/C5 all deferred with named prerequisites. No weighting question remains (one channel). Thresholds explicitly tagged "seeded, not earned; shadow-mode-calibrate before enforcement."
- **v3.1 (2026-04-24 late) — current.** Third council pass. Dialectic found no forcing issues (dynamics-confound noted but below primitive's resolution; `plausibility` at API boundary already scoped honestly — dialectic's own recommendation: stop iterating, let next signal come from shadow-mode data or downstream adoption). Code review found three factual errors in the implementation sketch — corrected below. No scope changes.

---

## Purpose

A fresh process-instance declaring `parent_agent_id=<uuid>` is making a *claim*, not a fact. `score_trajectory_continuity` scores how well the successor's observed EISV trajectory matches the parent's, giving a plausibility in `[0, 1]`.

This is a **single-channel primitive** that measures one thing: trajectory-shape similarity. It is not a behavioral fingerprint. It is not a five-dimensional agent identity gate. It is a narrow, implementable first step toward behavioral-continuity verification (ontology axiom, `identity.md`).

The broader "multi-channel agent fingerprint" that v1 reached for is not this row. It is a sequence of follow-up rows, each gated on the infrastructure it requires.

## Non-goals (explicit)

- **Not authentication.** Auth remains bearer-token + process-fingerprint.
- **Not a security primitive.** An adversary with KG read access can forge a passing trajectory. This primitive detects *honest over-claims*.
- **Not an identity issuer.** Output is a plausibility score; policy decides what to do with it.
- **Not a substitute for R4.** Substrate-earned agents use `verify_substrate_earned`.
- **Not an integration test.** Similarity ≠ integration. R5 discriminates integration from replay.
- **Not a behavioral fingerprint.** Trajectory shape is one facet of behavior. Calling this primitive "behavioral continuity" overclaims — v3 renames accordingly.

## Input signature

```python
def score_trajectory_continuity(
    claimed_parent_id: str,
    successor_id: str,
    *,
    min_observations: int = 5,
    window: timedelta = timedelta(days=30),
) -> TrajectoryContinuityScore:
    ...

@dataclass
class TrajectoryContinuityScore:
    plausibility: float                   # [0.0, 1.0], average of per-dimension DTW similarities
    verdict: Literal["plausible", "inconclusive", "unsupported"]
    observations: Dict[str, int]          # checkpoints used per dimension (parent, successor)
    components: Dict[str, float]          # {"E": 0.82, "I": 0.71, ...} — per-dimension similarity
    reasons: List[str]                    # human-readable drivers
    parent_mature: bool                   # parent had ≥ min_observations history in window
```

## The one channel

**C1 — Per-dimension EISV trajectory similarity.**

For each dimension `d ∈ {E, I, S, V}`:
1. Reconstruct parent's `d`-series and successor's `d`-series from `agent_states` rows over `window`.
2. `sim_d = _dtw_similarity(parent_series_d, successor_series_d)` — existing primitive at `src/trajectory_identity.py:198`.
3. If either side has < `min_observations` rows for dimension `d`, record `None` and carry the dimension in `reasons`.

`plausibility = mean(sim_d for d in dimensions if sim_d is not None)`.

If no dimensions are available, `verdict = "inconclusive"` with `plausibility = 0.0` (by convention, not score).

No weights. No composition. Four per-dimension DTW similarities, averaged.

## Deferred — what is *not* in R1 v3

Each item is deferred with a named prerequisite. When its prerequisite ships, it becomes its own plan row.

| Channel | Prerequisite to unlock |
|---|---|
| C2 Homeostatic set-point + recovery | Server-side fit machinery that produces mean/covariance/recovery-tau from `agent_states` scalars. (Current `homeostatic_similarity` expects these as agent-uploaded fields; `process_agent_update` does not populate them.) |
| C3 Calibration curve | Per-agent calibration storage (currently `calibration_checker` is a global aggregate, `src/calibration.py`). Also named in plan.md S12 as a FEP-unblock channel. |
| C4 Decision distribution | Persistent per-agent decision log (currently `AgentBaseline.recent_decisions` is a 20-entry LRU in-memory, `governance_core/ethical_drift.py`). |
| C5 Complexity distribution | Persistent complexity samples + Beta or covariance-fit machinery (currently `baseline_complexity` is a single EMA scalar). |

These four each look like 1-2 weeks of storage/fit work. None of them are blocked on R1; R1 can ship against the standard check-in path today.

## New helper: `reconstruct_eisv_series`

`src/mcp_handlers/identity/lineage_verification.py` (proposed module name at draft time was `<draft>/src/identity/lineage_continuity.py`; landed at the current path during implementation) exposes:

```python
def reconstruct_eisv_series(
    agent_id: str,
    window: timedelta,
    conn,   # asyncpg.Connection, invoked inside run_in_executor via the
            # sync-asyncpg-in-executor pattern at tests/test_db_utils.py:36-38
) -> Dict[str, List[float]]:
    """
    Return {'E': [...], 'I': [...], 'S': [...], 'V': [...]} from core.agent_state
    rows for the agent within window, ordered by timestamp ascending.
    Dimension keys map to SQL columns: E→entropy, I→integrity, S→stability_index,
    V→volatility. Empty lists for dimensions with no rows in window.
    """
```

**SQL shape.** `core.agent_state` stores `identity_id` (BIGINT FK to `core.identities`), not the text `agent_id`, so the helper joins `core.identities` to resolve the UUID string. This matches the existing pattern in `get_latest_agent_state` at `src/db/mixins/state.py` (see the same file for `_row_to_agent_state` mapping — Python field name `void` corresponds to SQL column `volatility`; use the SQL name `volatility` in the query).

```sql
SELECT s.entropy, s.integrity, s.stability_index, s.volatility, s.recorded_at
FROM core.agent_state s
JOIN core.identities i ON i.id = s.identity_id
WHERE i.agent_id = $1
  AND s.epoch = $2  -- GovernanceConfig.CURRENT_EPOCH; see v3.2 amendment
  AND s.recorded_at >= NOW() - $3::interval
ORDER BY s.recorded_at ASC;
```

Index coverage: `db/postgres/schema.sql:169` provides `idx_agent_state_identity_time ON core.agent_state(identity_id, recorded_at DESC)`. Planner resolves identity first, then range-scans the index. No new index needed.

**Epoch filter (v3.2 correction).** The 2026-04-25 council code-review pass found that every row written by `record_agent_state` (`src/db/mixins/state.py:33`) stamps an `epoch` column, and every existing read query filters on `s.epoch = $N`. Without the filter, the helper returns rows from all epochs (pre-grounding + grounded) on any deployed instance, conflating calibration data across the EISV grounding boundary. Use `GovernanceConfig.CURRENT_EPOCH` as the bound at call time; do not hardcode a literal.

**Schema specifics** (for implementor): columns are `REAL NOT NULL DEFAULT ...`; `recorded_at` is `TIMESTAMPTZ NOT NULL`. Group rows into per-dimension lists in Python by reading the four scalar columns in order.

## Plausibility → verdict thresholds

Initial values, synthetic-seeded:

| Condition | Verdict |
|---|---|
| `observations[successor] < min_observations` OR `parent_mature == False` | `inconclusive` |
| `plausibility >= 0.70` | `plausible` |
| `0.55 <= plausibility < 0.70` | `inconclusive` |
| `plausibility < 0.55` | `unsupported` |

**These thresholds are seeded, not earned.** They produce the right verdicts on the synthetic fixtures below. Before any caller treats a verdict as enforcement-worthy, shadow-mode production data must show the plausibility distribution separates genuine (`spawn_reason=new_session`) from non-genuine cases at the proposed cuts. If it does not, thresholds move; the primitive does not.

The synthetic fixture regression-tests the *cuts* given the generator, not the *calibration* of the cuts against reality. Honest framing matters here.

## Caller policy (the thing v2 punted on)

`inconclusive` has two reasonable caller postures. Every call-site picks one explicitly; no default.

- **Blocks (conservative):** `inconclusive` does not upgrade the lineage claim. Re-evaluate later when the successor has more observations.
- **Marks (permissive):** `inconclusive` proceeds but the lineage record is stamped `provisional=true`. Downstream consumers (trust-tier, KG provenance) can see the mark and decide independently.

Near-term call-sites and their postures:
- Onboard-time scoring of `parent_agent_id` → **Marks.** Fresh agents have few observations; blocking here is too strict.
- Promotion from `provisional` to `confirmed` after N check-ins → **Blocks.** Cannot confirm on inconclusive.
- Orphan archival (S8) re-classification with claimed lineage → **Blocks.** Archival is irreversible-ish.

## Implementation sketch

- **Location:** `<draft>/src/identity/lineage_continuity.py` (new — landed at `src/mcp_handlers/identity/lineage_verification.py` during implementation; this section captures the design intent at draft time). Consumers import `score_trajectory_continuity` directly.
- **DB pattern:** sync DB read inside `run_in_executor`. The project's `src/agent_loop_detection.py:374` template uses in-memory state and does *not* generalize to DB reads. The correct template already exists at `tests/test_db_utils.py:36-38` — it runs an asyncpg connection on a fresh event loop inside the executor thread via `asyncio.run(...)`. This adds no new dependency (project already has `asyncpg>=0.29.0`; `psycopg2` is not in `pyproject.toml`). Env var is `DB_POSTGRES_URL` (see `src/db/postgres_backend.py:72`), not `DATABASE_URL`. An implementation may promote the `test_db_utils` pattern into a reusable helper, but no new driver is required.
- **Exposure:** internal policy consumers only. No MCP tool wrapper in v3.
- **Observability:** emits a KG discovery of type `trajectory_continuity_score` with `plausibility`, `verdict`, `components` (per-dimension), and `observations`. Provides audit trail.

## Test fixture (synthetic)

`tests/conftest.py` is pure isolation infrastructure (session, DB-backend stubbing, ghost cleanup) — no data-generator fixtures live there today. Adding a trajectory generator there would be a stylistic mismatch. Place the generator in a dedicated helper module instead:

- `tests/helpers/trajectory_fixtures.py` (new) — `synthetic_trajectory_pair(seed, kind) → (parent_rows, successor_rows)`: returns two lists of dicts shaped like `core.agent_state` rows. `kind ∈ {"genuine", "divergent", "drifted", "early"}`.
- The `eisv_dtw_score_fixture` pytest fixture (for mocking the DB reader in unit tests) can live as a local fixture in `tests/test_lineage_continuity.py` itself, or be promoted to a helper module later if multiple tests use it.

Test cases in `tests/test_lineage_continuity.py` (new):

1. **Genuine.** Parent 30 rows high-basin, successor 10 rows continuing same generator. Expect `verdict=plausible`, `plausibility >= 0.70`.
2. **Divergent.** Parent as above; successor from independent generator. Expect `verdict=unsupported`.
3. **Early.** Parent as above; successor with 3 rows. Expect `verdict=inconclusive`, `parent_mature=True`.
4. **Drifted.** Parent stable; successor starts matched then drifts 10 rows. Expect `verdict=inconclusive` (the policy-decision zone).
5. **Immature parent.** Parent with 4 rows. Expect `verdict=inconclusive`, `parent_mature=False`.
6. **Dimensional degradation.** Parent has all four dimensions; successor has only E (others recorded as `None`). Expect plausibility averaged over E only, dimensions named in `reasons`.

Generators are deterministic (seeded). Thresholds are regression-tested against synthetic, *not* calibrated against it — see thresholds section.

## Shadow-mode calibration path

1. Ship primitive with synthetic-seeded thresholds.
2. Log every scoring call to KG discovery-type `trajectory_continuity_score`. Do not enforce.
3. After ≥ 2 weeks or ≥ 50 declared-lineage pairs (whichever later), inspect distributions by `spawn_reason`:
   - `new_session` — expect genuine, should cluster above 0.70.
   - `subagent` — hypothesis: bimodal.
   - `compaction` — unknown.
4. If distributions don't separate at the proposed cuts, move the cuts. If they do, the seeded thresholds stand.
5. Only after shadow calibration are the `blocks` policy variants considered for enforcement.

Shadow-mode is the calibration mechanism. The synthetic fixture is a regression mechanism. Confusing these is v2's "thresholds asserted, not earned" failure.

## Dependency map

```
R1 ── provides similarity gate for ── R2 (integration test; R5 discriminates integration from replay)
R1 ── does NOT unblock ───────────── Q1 (trajectory portability — similarity ≠ integration)
R1 ── does NOT unblock ───────────── Q2 (subagent ephemerality — shadow data may inform, does not resolve)
R1 ── does NOT unblock ───────────── S9 (PATH 1/2 — honesty primitive, not verification primitive)
```

R1's near-term value is **diagnostic**: shadow-mode scoring of real declared-lineage pairs produces the evidence needed to (a) calibrate thresholds, (b) inform Q2 subagent-ephemerality analysis, (c) motivate or refute the C2/C3/C4/C5 infrastructure investments. R1 does not become load-bearing until R2 is under work and uses R1 as its similarity gate.

v1/v2 framed R1 as "the earning mechanism." v3 is narrower: R1 is a single-channel telemetry primitive that *could become* part of an earning mechanism if R2 is built on top of it. Stated directly, not hidden in a Purpose-section flourish.

## Open questions for Kenny

1. **Caller-policy defaults.** v3 proposes three call-sites (onboard, promotion, orphan). Correct list, or are there others? Any that should flip their default?
2. **Shadow-mode cutoff.** "≥ 50 pairs" is a guess based on plan.md's 56-agent 3-week corpus statement. Right order of magnitude, or closer to 200?
3. **Blocking-issue for implementation.** The sync-DB-read helper is trivial but doesn't exist. Should R1's implementation row include that helper, or should it be a separate row (one small helper, used by many future primitives)?

## Appendix: what this does NOT solve

- **Adversarial forgery.** KG-readable parent state enables trajectory synthesis. R1 is an honesty primitive, not a security one.
- **Trajectory portability (Q1).** R5's job.
- **Substrate-earned identity (R4).** Separate three-condition test.
- **Multi-channel agent fingerprint.** Not this row. See deferred table.
- **Agents with < `min_observations` rows.** Intentionally outside scope; returns `inconclusive`.
- **Weight justification.** Moot — single channel, no weights.

## Appendix: review provenance

- v1 dialectic (`a98256ccd566598cd`) — independence-citation error, content-exclusion tension, Potemkin-verifier risk, R2/Q1 overclaim, inconclusive-band framing.
- v1 code review (`ae3eec7695eafae26`) — C3/C4/C5 not implementable from current storage.
- v2 dialectic (`a57d2b9f80ee33ce3`) — C2 hidden composite weighting, 0.5/0.5 as claim not refusal, "paces with v7" conflation, thresholds asserted not earned, earning-mechanism vs telemetry-only framing gap.
- v2 code review (`a5a418ffb32f569b8`) — C1/C2 both gated on agent-uploaded `TrajectorySignature.attractor`, `run_in_executor` template does not cover DB reads, `tests/fixtures/` convention does not exist.
- v3 dialectic (`acb058f6cd6f3f4f0`) — dynamics-confound observation (post-coupling EISV may measure dynamics more than agent), seeded-thresholds epistemic-debt tension, inconclusive-flood operational concern. All three judged below forcing threshold given v3's shadow-mode-only ship posture. Dialectic's explicit recommendation: stop iterating, next signal comes from data.
- v3 code review (`a68d31899e90dea48`) — `core.agent_state` indexes on `identity_id` not `agent_id` (JOIN required); env var is `DB_POSTGRES_URL` not `DATABASE_URL`; psycopg2 is not a project dependency (use sync-asyncpg-in-executor pattern already at `tests/test_db_utils.py:36-38`); `conftest.py` is pure isolation infrastructure, data generators belong in a helper module.

v3.1 applies the v3 code-review corrections in-place (SQL JOIN, correct env var, sync-asyncpg pattern, fixture module). No spec-level changes.

---

## Amendment v3.2 (2026-04-25) — post-acceptance council pass

After the operator accepted v3.1 on 2026-04-25, a fourth council pass (dialectic + code-review) ran specifically on the implementation surface. It found four issues v3.1 didn't address. None invalidate v3.1's single-channel design; all add specifications the implementation row must follow.

### v3.2-A. Telemetry-as-lineage-leak surface

**Issue.** v3.1 emits a KG discovery of type `trajectory_continuity_score` per scoring call with `verdict`, `plausibility`, `components` (per-dimension similarities), `observations`. The §"What this does NOT solve" appendix names "adversarial forgery" as out-of-scope. But emitting full per-dim `components` to the *readable* KG materially lowers the cost of fitting a forgery — an adversary who can read scoring history sees exactly which dimensions matched and can synthesize a trajectory that hits the cuts.

**Mitigation (required before implementation row opens).** Split the discovery write path:

- **Public KG discovery payload:** `verdict` + `plausibility` (scalar) + `parent_mature` + `observations` only. No per-dimension breakdown.
- **Audit-only persistence:** `components` dict (per-dim similarities) stored in `core.audit_events` or a new `r1_score_audit` table — readable by operator tooling, not by KG queries available to general agents.
- **Reasons array:** stays in audit-only path. Includes dimension-degradation messages that could leak which dimensions are missing.

This adjustment costs one extra write site and one schema decision (audit table vs. extending `core.audit_events`). It does not change the primitive's signature or the verdict logic. The `TrajectoryContinuityScore` dataclass returned to *internal callers* remains complete; only the KG-published shape narrows.

### v3.2-B. `provisional=true` read-side contract

**Issue.** v3.1 caller-policy lists `marks` for the onboard call-site (stamp `provisional=true` on the lineage record, proceed). It does not specify (a) where the flag lives, (b) how four downstream consumers interpret it.

**Specification.**

- **Storage:** `provisional_lineage` boolean column on the lineage edge or `provenance_chain` entry, defaulting to `false`. Set `true` when `score_trajectory_continuity` returns `inconclusive` and the call-site policy is `marks`.
- **Read semantics for downstream consumers:**

| Consumer | Provisional record handling |
|---|---|
| Trust-tier (S6) | Ignored. `provisional=true` does not contribute to tier upgrades. Substrate-earned routing unchanged. |
| KG provenance (S7) | Visible in `provenance_chain` but flagged. Aggregations of "lineage-attributed activity" exclude provisional records by default; explicit query opt-in shows them. |
| R3 role baselines | Excluded from baseline distribution computation. Provisional pairs are not yet load-bearing for fleet calibration. |
| Dashboard / external consumers | Shown with explicit "provisional" badge. Do not present as confirmed lineage. |

- **Promotion path:** `provisional → confirmed` via the `score_trajectory_continuity` re-evaluation policy at the promotion call-site (`blocks` posture per v3.1). When successor accumulates ≥ `min_observations` and re-scoring returns `plausible`, flag flips to `confirmed`. If re-scoring returns `unsupported` after maturation, lineage edge is removed (orphan-archival path).
- **Backstop:** any consumer that does not implement provisional-aware logic must be patched in the same PR. Default policy for unaware consumers is "treat as confirmed" — the unsafe default. v3.2 elevates this to a read-side specification, not a punt to consumers.

### v3.2-C. `calibration_status` field

**Issue.** v3.1's "shadow-mode-only is not load-bearing until R2" framing is true for *enforcement* and false for *interpretation*. A KG discovery type, a per-pair score, and any dashboard surface together create a public commitment. Operators and external consumers will read `plausibility=0.62` as meaningful before calibration earns it.

**Specification.** Every score record + every dashboard surface gated on a `calibration_status` field with two values:

- `seeded` (default at ship): synthetic-fixture-calibrated thresholds, no production validation. UI displays "uncalibrated" badge; verdict is shown but downstream treats it as advisory only.
- `earned`: shadow-mode pairs cleared the cutoff (≥100 pairs OR ≥4 weeks per operator decision); thresholds validated against production distribution. UI displays the verdict without caveat.

The transition from `seeded → earned` is a single explicit operator action, not an automatic flip. Even if shadow-mode telemetry passes the cutoff, an operator must run the calibration analysis and explicitly mark the field. This prevents the failure mode where a dashboard panel quietly graduates from "advisory" to "load-bearing" without anyone naming the moment.

### v3.2-D. KG discovery TTL/cap

**Issue.** v3.1 emits one KG discovery per scoring call. No retention policy. At `process_agent_update` rate × any agent with declared parent, this generates one node per check-in indefinitely. Watcher findings have an explicit `FINDINGS_TTL_DAYS = 14` cap; KG discovery types do not.

**Specification.**

- **Dedupe by `(parent_id, successor_id)` pair:** update the existing record rather than appending. The N-th score for a pair overwrites the (N-1)-th in the public KG; the audit-only table (per v3.2-A) retains history.
- **TTL = 30 days** on the public KG record. After 30 days without re-scoring, record is archived (audit table retains).
- **Audit-only table** retains history per its own retention policy (currently 90 days for `audit_events`; new `r1_score_audit` table inherits).

### v3.2-E. Inline corrections to v3.1 implementation sketch

The council code-review surfaced three additional implementation-row gotchas:

1. **`epoch` column filter** in the SQL (already applied above to `reconstruct_eisv_series`).
2. **conftest stub registration:** `tests/conftest.py:_isolate_db_backend` is autouse and replaces `_db_instance` with an `AsyncMock`. Any new method the helper adds must be registered as a method stub on `mock_backend` or new tests will get auto-generated `AsyncMock` children returning coroutines instead of lists. Implementation row must add `mock_backend.reconstruct_eisv_series` and `mock_backend.score_trajectory_continuity` to the conftest fixture.
3. **`asyncio.run()` vs `asyncio.new_event_loop()`:** v3.1's prose described the `tests/test_db_utils.py:36-38` template as using `asyncio.run(...)`. The actual pattern uses `asyncio.new_event_loop() + run_until_complete() + loop.close()`. Implementer should follow the actual code, not the prose description.

### v3.2-F. Known limitation — script-driven trajectory pairs

The trajectory council agent surfaced this: under S1-a (TTL shrink), Chronicler-style daily-cron processes get forced through `force_new` re-onboard on each wake. They will appear to R1 as declared-lineage pairs (the cron has a stable identity it can declare as parent), and their DTW similarity will be high — not because of behavioral lineage but because the script is deterministic.

**Captured as known limitation, not a v3.2 fix.** Mitigation lives in S8a Phase 2: when `session_like` (or a sibling `script_driven` class) is added, R1's calibration partition can filter these out. Until then, R1 implementation should:

- Document this expected high-plausibility cluster in the shadow-mode calibration appendix.
- Recommend that the calibration analysis, when run, explicitly inspects `class_tag=resident_persistent` separately from session-like pairs, since the deterministic-script behavior is concentrated in residents.

### v3.2 summary

Four normative additions (v3.2-A through D), three implementation-row corrections (v3.2-E), one captured limitation (v3.2-F). Single-channel design from v3.1 unchanged. No changes to `score_trajectory_continuity` signature; one new column on lineage records (`provisional_lineage`); one new field on score records (`calibration_status`); one new audit table (`r1_score_audit`).

**Implementation row sequencing reminder (per `plan.md` 2026-04-25 appendix):** R1 implementation row blocks on (1) S8c (`spawn_reason` write-path repair), (2) S8a Phase 2 (`session_like` class), (3) light council confirmation pass on this v3.2 amendment.

---

## Amendment v3.3 (2026-05-03) — council confirmation pass

The "light council confirmation pass" named at the end of v3.2 ran 2026-05-03 as three parallel agents (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`) against the v3.2 amendment. All three returned **WITHHOLD-PENDING-V3.3** with overlapping forcing items. v3.3 folds operator decisions on three taste-level choices and converts every council finding into either (a) a normative tightening, (b) an explicit implementation-row constraint, or (c) a doc-text correction.

Single-channel design from v3.1 is unchanged. v3.2's normative additions (provisional lifecycle, calibration_status, dedupe/TTL) stand; v3.3 corrects their text and tightens their semantics where the council found leakage or under-specification.

### v3.3-A. Public payload — strict redaction (supersedes v3.2-A)

**Council finding (architect, forcing).** v3.2-A's "audit-only `components`" left `verdict + plausibility (scalar) + parent_mature + observations (per-dim counts)` public. With dedupe-by-pair (v3.2-D) and 30-day TTL, an adversary with KG read still gets (a) the exact scalar that crossed the cut and (b) which dims were `None` via the per-dim `observations` map. The split moves the leak; it does not close it.

**Operator decision 2026-05-03.** Strict public redaction.

**Replaces v3.2-A's split-payload spec with:**

- **Public KG payload (label: "public redacted"):** `verdict` (enum) + `calibration_status` (enum, see v3.3-C) + `n_dims_used` (int 0–4) + `score_id` (UUID — the join key into the audit table). Nothing else. No `plausibility` scalar. No per-dimension `observations` map. No `parent_mature` boolean. No `reasons` array.
- **Audit-only persistence (`audit.r1_score_audit` table — see v3.3-E for table name correction):** full record — `score_id` (PK), `parent_id`, `successor_id`, `recorded_at`, `plausibility` (float scalar), `components` (jsonb, per-dim similarities), `observations` (jsonb, per-dim counts and which were None), `parent_mature`, `reasons` (text[]), `class_tag` (text, parent class at scoring time per v3.3-G), `calibration_status` (enum at time of write).
- **Naming.** Label this path "public redacted," not "audit-only." Calling it "audit-only" understates what was still public in v3.2-A.

**Join semantics.** The public KG node carries `score_id` (UUID). The audit row with that `score_id` is the canonical record; the public node is its redacted projection. When dedupe fires on `(parent_id, successor_id)` (v3.2-D), the public node's `score_id` updates to the new audit row's UUID. Audit table is append-only; previous `score_id` values remain queryable for the operator-only calibration analysis path.

This closes v3.2's residual leak and pre-resolves council finding A4 (audit↔public join key was implicit in v3.2; v3.3 makes `score_id` the explicit primitive).

**Internal-caller surface unchanged.** The `TrajectoryContinuityScore` dataclass returned to *internal callers* (the policy layer making `blocks` / `marks` decisions) remains complete per v3.1 §"Input signature." Only the KG-published shape narrows.

### v3.3-B. R2 row added to consumer table (closes v3.2-B gap)

**Council finding (architect, forcing).** v3.2-B's read-side contract listed four consumers (trust-tier S6, KG provenance S7, R3 baselines, dashboard) but omitted R2. R2 (honest memory integration, design v2 merged 2026-05-02) walks `provenance_chain` and credits along confirmed lineage chains. With `provisional_lineage` defaulting to "treat as confirmed" for unaware consumers (the unsafe-default backstop v3.2-B itself names), provisional edges would flow through honest-memory crediting via the unsafe default.

**Specification.** Extend the v3.2-B consumer table with an R2 row:

| Consumer | Provisional record handling |
|---|---|
| (existing rows from v3.2-B) | (unchanged) |
| **R2 honest memory integration** | **Excluded from chain crediting.** R2's forward-only chain counter (per `r2-honest-memory-integration.md` v2) does not advance through provisional edges; promotion to `confirmed` is the gate. R2's read path queries the explicit column added per v3.3-D (not the AGE edge property if and when it later mirrors). |

**Sequencing.** The R2 implementation row hasn't opened (it blocks on R1 implementation row per `plan.md`). v3.3-B specification binds R2's read path *when R2 impl opens*, not retroactively in R1's PR. R1's PR adds `core.identities.provisional_lineage` (per v3.3-D); R2's PR consumes it.

### v3.3-C. `calibration_status` lifecycle — three-state with timestamps (supersedes v3.2-C)

**Council finding (architect, forcing).** v3.2-C's two-state `seeded → earned` forecloses the third reality: shadow-mode ran and distributions did not separate at the proposed cuts. Operator's only choices become "leave seeded" (silent-indefinite-advisory) or "mark earned" (false). v3.2-C is *explicitly* written to prevent the silent-graduation failure; the missing state recreates the symmetric silent-stagnation failure.

**Operator decision 2026-05-03.** Add `calibration_failed` first-class. Add `seeded_since` timestamp.

**Replaces v3.2-C's two-state spec with three states + lifecycle timestamps:**

| Value | Meaning | UI / consumer behavior |
|---|---|---|
| `seeded` (default at ship) | Synthetic-fixture-calibrated thresholds, no production validation | "uncalibrated" badge; verdict treated as advisory only |
| `earned` | Operator ran calibration analysis; cuts validated against production distributions; seeded thresholds stand or new cuts adopted | Verdict shown without caveat |
| `calibration_failed` | Operator ran calibration analysis; distributions did not separate at any defensible cut | "calibration failed" badge; verdict suppressed in dashboard surfaces; downstream consumers (R2, trust-tier promotion gate) MUST treat as `inconclusive` regardless of what the primitive returned |

**Timestamps (added to score record + a single calibration_state singleton row, location TBD during impl):**

- `seeded_since` (TIMESTAMPTZ) — stamped at first ship of the primitive in production.
- `earned_at` (TIMESTAMPTZ NULL) — stamped on `seeded → earned` transition.
- `failed_at` (TIMESTAMPTZ NULL) — stamped on `seeded → calibration_failed` or `earned → calibration_failed` transition.

Transitions are explicit operator actions; no automatic flips. (Automatic transitions reintroduce the silent-graduation problem v3.2-C exists to prevent.)

**Stale-seeded surfacing.** Operator runbook + dashboard panel surface `seeded_since` age. ≥90 days seeded without an `earned_at` or `failed_at` decision = a flag in the operator's view. Not a hard cutoff; a visibility primitive. The forcing function is operator attention, not automatic transition.

**Consumer behavior under `calibration_failed`.** This is the load-bearing piece v3.2-C was missing: when status is `calibration_failed`, downstream consumers do not just "show the badge" — they degrade the verdict to `inconclusive` for decision purposes. R2 chain crediting, trust-tier promotion, orphan-archival re-classification all behave as if the score returned `inconclusive`. The score record itself retains the original verdict for forensic access, but the consumer-facing primitive returns `inconclusive`.

### v3.3-D. `provisional_lineage` storage — explicit SQL columns on `core.identities` (supersedes v3.2-B storage spec)

**Council finding (reviewer + verifier, forcing).** v3.2-B said "boolean column on the lineage edge or `provenance_chain` entry." Live-verifier confirmed neither exists: no `lineage_edges` table, no `provenance_chain` table, no `provisional_lineage` column anywhere in the live schema. Declared lineage lives in `core.identities.parent_agent_id` (TEXT FK) and the AGE graph. v3.2-B's "must be patched in the same PR" backstop was unenforceable until the storage target was named.

**Operator decision 2026-05-03.** SQL column on `core.identities`. AGE edge property may mirror later if graph traversal benefits surface; SQL is the source of truth for R2 / trust-tier / dashboard / audit consumers.

**Specification.** Add to `core.identities`:

| Column | Type | Default | Meaning |
|---|---|---|---|
| `provisional_lineage` | BOOLEAN NOT NULL | FALSE | True when most recent score returned `inconclusive` and call-site policy was `marks` |
| `provisional_score_id` | UUID NULL | NULL | References `audit.r1_score_audit.score_id` of the score that justified the current state; NULL when never scored or after `confirmed` transition |
| `provisional_recorded_at` | TIMESTAMPTZ NULL | NULL | When the current `provisional_lineage` value was last set |
| `confirmed_at` | TIMESTAMPTZ NULL | NULL | Stamped on `provisional → confirmed` transition |

Migration slot: take the next available slot in `db/postgres/migrations/`. No new indexes anticipated at v0; provisional rows are expected to be a tiny fraction of `core.identities`. Index decisions deferred until a query motivates them.

**Consumer patches required in the same PR (concrete file paths, not spec labels):**

| Consumer | File / module | Action |
|---|---|---|
| Trust-tier (S6) | `src/identity/trust_tier_routing.py` (`resolve_trust_tier`) | Read `provisional_lineage`; if true, do not contribute to tier upgrades |
| KG provenance (S7) | `src/storage/knowledge_graph_postgres.py` + `src/db/mixins/knowledge_graph.py` (provenance-chain query path; identify exact site during impl) | Aggregations of "lineage-attributed activity" exclude `provisional_lineage=true` by default; explicit query opt-in shows them |
| R3 role baselines | **Deferred — no in-process consumer exists today** (3-agent council 2026-05-04 confirmed: `audit.r1_score_audit` is write-only; no aggregator queries it; live runtime has 2 rows / 0 confirmed lineages). Provisional exclusion is a SQL query-time discipline for operator-side calibration analysis (the `seeded → earned` flip per v3.3-C is operator-driven, not automatic — building a runtime aggregator that fires on the hot path would violate that invariant). When the in-process or operator-tool reader lands, signature should be `get_r1_plausibility_distribution_by_class(exclude_provisional=True, *, window_days=180)` joining `audit.r1_score_audit` against `core.identities` and filtering on `provisional_lineage = FALSE`. | Bind when the reader lands; the spec contract is that `exclude_provisional` defaults to `True` and the reader's caller must opt in to seeing provisional rows. |
| R2 honest memory | (R2 impl row prereq, not in R1's PR — see v3.3-B sequencing) | Exclude provisional from forward-only chain crediting |
| Dashboard | `unitares-dashboard/` (specific file TBD during impl) | Show "provisional" badge with `provisional_recorded_at` |

Migration + first three consumers shipped in the R1 implementation row (PRs #306/#309/#314/#320/#321/#324). The R3 row was originally framed as a fourth in-PR consumer; council pass 2026-05-04 (architect + reviewer + live-verifier) found the named consumer site does not exist in code and that constructing one would violate v3.3-C's "calibration is operator-driven, not automatic" invariant. R3 row deferred per the table above; PR 4b folded into this doc-correction edit. Dashboard patch may ship in a follow-up if scoping demands it (note: per memory `unitares-dashboard.md`, dashboard panel changes follow file-allowlist conventions). R2 consumer patch lives in R2's own implementation PR.

**Why explicit columns, not JSON metadata.** R2 / trust-tier / R3 / dashboard all need to query on `provisional_lineage = false`. JSONB metadata makes the predicate awkward and untyped. Explicit columns + SQL constraints are the source-of-truth shape; AGE edge property can mirror later via a write-side trigger if graph traversal benefits surface.

### v3.3-E. Audit table — name and retention corrections (supersedes v3.2-A storage location and v3.2-D retention claim)

**Council finding (reviewer + verifier, forcing).** v3.2-A and v3.2-D both named `core.audit_events` as the audit persistence target and claimed 90-day retention. Live-verifier confirmed: actual table is `audit.events` (schema `audit`, not `core`); retention is **180 days** per `db/postgres/partitions.sql:4` and `drop_old_events_partitions(p_retention_days INTEGER DEFAULT 180)`. The 90-day figure applies to `audit.tool_usage`, not `audit.events`.

**Operator decision 2026-05-03.** `audit.r1_score_audit` is a new dedicated table (not folded into `audit.events`). Retention: **180 days**, inheriting `audit.events`. R1 shadow calibration needs long enough windows to diagnose separation failure; 90 days is too easy to starve, especially for low-volume class partitions (e.g., `subagent` declared-lineage pairs).

**Specification.**

- New table `audit.r1_score_audit` (schema `audit`).
- Columns per v3.3-A audit-only persistence list.
- Partitioning: RANGE on `recorded_at`, matching `audit.events` partition cadence (see `db/postgres/partitions.sql`).
- Retention: 180 days. Add to the existing partition-drop job — either by extending `drop_old_events_partitions` to cover the new table, or by a sibling function `drop_old_r1_score_audit_partitions` invoked from the same scheduled job. Implementation choice.

### v3.3-F. `epoch` column reference (supersedes v3.2-E item 1)

**Council finding (reviewer, forcing).** v3.2-E said "see `schema.sql:169` for the index" and named `epoch` as a column to filter on, but `db/postgres/schema.sql:146-167`'s `core.agent_state` DDL does not define `epoch`. The column is live (write path stamps it via `GovernanceConfig.CURRENT_EPOCH = 3`) — it was added in a migration that never folded back into the base `schema.sql`.

**Specification (impl row work).** Implementor identifies the migration that added `epoch` (search `db/postgres/migrations/` for `epoch` or `CURRENT_EPOCH`) and either (a) cites it directly in the v3.2-E correction, or (b) backports the column into `schema.sql` so the base DDL is honest. The latter is the migration-discipline-clean path; the former is the minimal-scope path. **Operator default:** backport into `schema.sql` if it's a one-line addition; cite-only if the migration carries other unrelated changes that complicate a partial backport.

This is migration-discipline housekeeping, not R1-specific work; it surfaces here because R1 is the first reader to depend on `epoch`'s presence and the gap was hidden until live-verifier ran.

### v3.3-G. `class_tag` on score record (closes flag A5)

**Council finding (architect, flag elevated to spec).** v3.2-F flagged the resident-class deterministic-script cluster as a known limitation. Without a `class_tag` on each score record, the calibration analysis must reconstruct partition membership *at analysis time* from `core.identities.metadata.class_tag` — which is what S8a Phase 1 stamps at onboard. That reconstruction is straightforward today but couples calibration analysis to S8a's tag-discipline state at *analysis time*, not *scoring time*.

**Specification.** Add `class_tag TEXT NULL` to `audit.r1_score_audit` (per v3.3-A audit columns). Stamped at scoring time by reading the parent's current `class_tag` from `core.identities.metadata`. NULL when the parent has no class tag (S8a Phase 2 backfill not yet complete for that agent).

This is the minimum honest move: future calibration analyses operate on the partition state *at scoring time*, not at analysis time. Cheap (one column read at score time), and avoids the failure mode where a Phase 2 backfill retroactively re-classifies an agent and silently changes the calibration partition for old score records.

### v3.3-H. Implementation-row constraints (captures flags A4, A6, C4, C5)

The following are not normative changes to the spec but explicit constraints the implementation row must follow. Naming them here so they are not left to implementor judgment.

1. **C4 — empty-dim handling.** `_dtw_similarity` (at `src/trajectory_identity.py:205`, see v3.3-I for line correction) returns 0.0 on empty input via `_dtw_distance` returning `float("inf")`. The implementation MUST short-circuit at the caller: per-dimension absence (empty list from `reconstruct_eisv_series`) excludes that dimension from the average rather than scoring 0.0. Test plan must include a synthetic case where parent has all four dims, successor has only E (S/I/V missing on successor side) — should average over E only, with S/I/V named in `reasons`.
2. **C5 — function placement.** `score_trajectory_continuity` is a module-level async function (not a backend method). It internally awaits `loop.run_in_executor(None, _score_sync, ...)` where `_score_sync` calls `reconstruct_eisv_series` (which IS the DB-touching helper, registered as a backend method per the v3.2-E correction). The conftest stub list narrows: only `mock_backend.reconstruct_eisv_series` needs registration in `tests/conftest.py:_isolate_db_backend`. `score_trajectory_continuity` itself is mocked at the module level when tests need to.
3. **A4 — join key.** Already specified in v3.3-A: `score_id` UUID is the explicit join key between public KG node and audit table row. No additional impl-row work; this constraint is informational.
4. **A6 — lifecycle scope expansion noted.** v3.2's normative additions (`provisional_lineage`, `calibration_status`) create a 2×2 lifecycle state (provisional × calibration_status) on what v3.1 framed as stateless-per-call. v3.3 acknowledges this explicitly. Test coverage in the impl row must include the four cells, not just happy-path single-call regression: `(provisional=false, status=seeded)`, `(provisional=true, status=seeded)`, `(provisional=false, status=earned)`, and crucially the failure-mode cells where `status=calibration_failed` degrades verdicts to `inconclusive` per v3.3-C.

### v3.3-I. Doc-text corrections (live-verifier ground truth)

These are mechanical fixes to v3.1/v3.2 prose — no spec impact. Future readers and implementors should trust v3.3 paths/lines over v3.1/v3.2 where they conflict.

| v3.1/v3.2 text | Correction | Source |
|---|---|---|
| `core.identities.id` (FK target) | `core.identities.identity_id` | `\d core.identities` (live schema) |
| `core.audit_events` | `audit.events` (schema `audit`, not `core`) | `db/postgres/partitions.sql:4` |
| 90-day retention for `audit_events` | 180-day retention for `audit.events`; 90-day applies to `audit.tool_usage` | `drop_old_events_partitions(p_retention_days INTEGER DEFAULT 180)` |
| `unitares/config/governance_config.py` | `config/governance_config.py` (live import: `from config.governance_config import GovernanceConfig`) | live import path verified |
| `governance_core/knowledge_graph.py` (does not exist) | `src/storage/knowledge_graph_postgres.py` + `src/db/mixins/knowledge_graph.py` | repo scan |
| `_dtw_similarity` at line 198 | line 205 | `src/trajectory_identity.py:205` |
| `record_agent_state` at line 33 | def at line 17; epoch arg at line 39 | `src/db/mixins/state.py:17,39` |
| `_write_entry` at lines 431-439 | def at line 515; fire-and-forget docstring at line 541 | `src/audit_log.py:515,541` |

**KG upsert primitive — flag refuted, not folded as forcing.** v3.2-D's dedupe-by-pair claim was challenged by the code-reviewer council pass as requiring a MERGE primitive that "doesn't exist." Live-verifier ground-truthed this: `src/storage/knowledge_graph_postgres.py:82` already has `ON CONFLICT (id) DO UPDATE SET`. The dedupe-by-pair pattern is feasible against the existing write path; v3.2-D stands as specified.

### v3.3 summary

**Three operator decisions on taste-level questions (2026-05-03):**

1. Strict public redaction (v3.3-A) — public payload narrows to `verdict + calibration_status + n_dims_used + score_id`; everything else moves to `audit.r1_score_audit`.
2. `calibration_failed` + `seeded_since` (v3.3-C) — three-state lifecycle with timestamps; failure state is first-class with explicit consumer-degradation semantics.
3. SQL column on `core.identities` for `provisional_lineage` (v3.3-D) — source of truth is SQL; AGE edge mirrors later if needed.

**Six forcing items folded** (A1, A2, A3 from architect; storage target, audit-table-name, epoch-column-reference from reviewer + verifier).

**Four flags converted to explicit impl-row constraints** (A4 join key, A6 lifecycle scope, C4 empty-dim skip, C5 function placement).

**One flag refuted by live-verifier** (KG MERGE primitive — exists at `src/storage/knowledge_graph_postgres.py:82`).

**Eight doc-text bugs corrected** (v3.3-I table).

Single-channel design (v3.1) unchanged. Normative shape (v3.2) preserved; tightenings only.

**Implementation row sequencing reminder (updated 2026-05-03):**

| Prereq | Status |
|---|---|
| S8c (`spawn_reason` write-path repair) | ✅ shipped 2026-04-25 (#155) |
| S8a Phase 2 (`session_like` class) | ✅ shipped 2026-05-01 (#252) |
| Light council confirmation pass on v3.2 | ✅ ran 2026-05-03; verdict WITHHOLD-PENDING-V3.3 |
| v3.3 amendment | ✅ this section |

**The R1 implementation row may open against v3.3.**

---

## Amendment v3.4 (2026-05-05) — orphan-candidate demotion primitive

The 2026-05-05 R1 maintenance follow-up shipped a promotion/TTL sweep that reported `unsupported` provisional scores as `orphan_candidate` but intentionally did not remove lineage edges. That was the right default while no explicit destructive primitive was named.

R2 Phase 1 has since supplied the storage primitive R1 needs: `demote_lineage(successor_id, reason=...)` on `src/db/mixins/identity.py`. It clears the declared parent edge, clears provisional/confirmed state, stamps `lineage_demoted_at`, resets `chain_obs_count`, and has terminal-state WHERE guards. R1 should use that primitive rather than invent a second edge-removal path.

### Decision

`sweep_provisional_lineage` remains report-only by default:

- `verdict="plausible"` + `apply=false` → `would_confirm`
- `verdict="plausible"` + `apply=true` → `confirm_lineage`
- `verdict="unsupported"` + `apply_orphans=false` → `orphan_candidate`
- `verdict="unsupported"` + `apply_orphans=true` → `demote_lineage(successor_id, reason="r1_unsupported")`
- `verdict="inconclusive"` → `blocked_inconclusive`

The destructive action is deliberately controlled by a separate flag, `apply_orphans`, not by `apply`. Confirming a plausible lineage and demoting an unsupported lineage are different operator choices; a caller may want one without the other.

### Audit semantics

If `demote_lineage` succeeds, the maintenance path emits the same lifecycle event class used by the R2 FSM:

```text
event_type = "lineage_demoted"
agent_id = successor_id
details = {
  parent_id,
  score_id,
  reason: "r1_unsupported",
  source: "r1_maintenance",
  plausibility?  # present when the score object exposes it
}
```

If the storage helper's WHERE guard returns false, the result is `orphan_demote_failed` and no audit event is emitted. This mirrors the R2 FSM convention: audit events record storage-confirmed transitions, not attempted transitions.

### CLI surface

`scripts/migration/r1_lineage_maintenance.py promote-provisional` gains:

```text
--apply-orphans
```

Without it, unsupported rows continue to be reported only. With it, unsupported rows are demoted through `demote_lineage`. The existing `--apply` flag continues to control only `confirm_lineage` for plausible rows.

### Non-goals

- No direct AGE edge deletion in R1 maintenance. SQL `core.identities` remains the source of truth; graph projection/cleanup belongs to S7/R2 downstream work.
- No automatic demotion from the normal hot path beyond the existing R2 FSM. This is an operator maintenance action.
- No demotion on `inconclusive`; the row remains provisional or proceeds to the grace-expiration archive path.
