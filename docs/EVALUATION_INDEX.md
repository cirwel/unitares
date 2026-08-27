# Evaluation Catalog — EISV validation, ablations, dogfood, analysis

Single index of the evaluation/ablation/dogfood/validation surface, so the work is
discoverable instead of rediscovered. **Before adding a new eval or "does EISV
actually work" analysis, check here first** — much already exists, and at least one
session (2026-06-23) rebuilt machinery that was already in `scripts/analysis/`.

Rows verified by reading the code on 2026-06-23 are **✓**; entries still inferred from
name only are **~**. Freshness flags call out scripts that **won't run as-is** (removed
backends, missing source symlinks). Hermes-agent's ablation/dogfood lives in its own
repo (automation-side) and is intentionally *not* consolidated here.

## Semantic guardrail: EISV is proprioception, not verdict authority

Read [`docs/ontology/eisv-proprioception-contract.md`](ontology/eisv-proprioception-contract.md)
before interpreting these reports. EISV/prior-state analysis asks whether
proprioceptive telemetry adds signal over baselines. It does **not** let EISV
supply its own outcome labels, hand down bad verdicts, or treat ordinary CI/test
failures as moral badness. Human-facing labels should distinguish task-negative
evidence, contract/process violations, authority/harm events, synthetic red-team
fixtures, and unknown/unmeasured outcomes.

### Outcome-label vocabulary

Use `bad` only as a data label, never as an undefined success/failure slogan:

| Term | Meaning |
|---|---|
| `is_bad=true` / `bad` | An outcome row labeled negative by its recorded type or rubric. It is an analysis label, not a moral category or an EISV bad verdict. |
| `task-negative` | A failed test/tool/task result such as `test_failed`, `tool_rejected`, or `task_failed`. |
| `strict_bad` | A strict-scope negative row with stronger provenance requirements; useful for validation only after enough rows exist. |
| `prevented` | Only valid when a policy/actuator actually blocked, paused, rejected, or reverted an adverse effect. A counted `bad` row by itself is observed evidence, not prevention. |

## ⚠ Start here: the two scripts that already answer "does EISV discriminate?"

Don't rebuild discrimination analysis — these exist and are current:

- **`scripts/analysis/eisv_skeptic_report.py`** ✓ — computes AUC/Brier
  **lift of EISV/prior-state over a previous-outcome baseline**, emits a runtime verdict
  (`DESCRIPTIVE ONLY` / `KEEP-TESTING` / `WEAK` / `INCONCLUSIVE`). A
  non-positive split is explicitly not a harm or refutation claim. The
  [resolved distributional-probe record](proposals/resolved/eisv-distributional-signal-probe-v0.md)
  used the earlier `SKEPTICAL` label; treat that wording as historical rather
  than as a general inference status. No hardcoded conclusion.
- **`scripts/analysis/eisv_ablation_matrix.py`** ✓ — same question across scope/window/lead
  slices with bootstrap CIs, permutation p-values, BEAM-lane exclusion, and a
  `NON_DETECTION` inference class when the selected best candidate does not clear
  its null. That class does not establish no effect or refutation.

- **`scripts/analysis/ablation_power_probe.py`** ✓ — the companion to those two.
  They report whether a lift separated from its null; this reports what size of
  lift the same machinery could have separated, by planting a known effect in a
  synthetic cohort of a given shape. Run it before reading any `NON_DETECTION`
  result as evidence of absence. No database, no credentials, deterministic.

**Reading either output:** `AUC delta`, `Null max p95`, and `Selective p`
decide whether a selected lift clears its matching null. Before making a claim,
also verify anchor scope and cutoff and report bad rows, permutation blocks, and
agents.

- `AUC delta` is the **maximum over ~7 candidates**, so its reference is `Null max
  median`, never zero. The null permutes whole EISV readings between `(agent,
  prior-state snapshot)` blocks, leaving labels — and therefore the baseline —
  untouched. A delta below the null median is compatible with that
  selection-aware null distribution; it does not establish noise or no effect.
  `Selective p` tests exactly the reported statistic; `Brier perm p` tests only
  Brier.
- `Bad clusters` is the count of `(agent, prior-state snapshot)` permutation
  blocks. Prior state is constant within a block, so an edit-test-retry burst
  must not be counted as N distinct feature readings. The block count is not
  proof of independent outcomes; report bad rows and agents with it.
- A slice whose chronological training half holds **no bad outcomes** reports
  `INCONCLUSIVE` rather than a baseline AUC. An untrained baseline ranks by
  Laplace tie-breaks and reads below chance, which any continuous feature clears.

The AUC delta is fitted-vs-fitted on both sides. `AUC delta (raw, legacy)` is the
older asymmetric number — candidate raw feature vs baseline fitted probability —
retained only to explain earlier reports. Do not cite it.

**Before re-running either script to ask "does EISV predict bad outcomes yet",
read [`proposals/eisv-outcome-grounding-stop-rule-v0.md`](proposals/eisv-outcome-grounding-stop-rule-v0.md).**
That question has a pre-registered confirmatory read on 2026-12-01 with a kill
criterion. The historical 2026-07-31 `≈ 0.05 AUC` bound is withdrawn for the
intended claim: it used the contaminated `--anchor-scope all` cohort, and its
101 "clusters" were prior-state permutation blocks rather than independent
adjudicated failures. The registered read now fixes `--anchor-scope trusted`
explicitly, and both prediction scripts now default to the trusted scope so an
omitted flag cannot silently reproduce the contaminated cohort. Ad-hoc reruns
between now and then will keep surfacing a selected maximum that the null
explains. Corpora that might one day answer the question better than organic
telemetry are recorded under
[Candidate corpora](#candidate-corpora--not-yet-evaluated) — recorded, not run.

The initial five-claim correction is recorded in
[`ontology/falsification-inference-containment-2026-08-22.md`](ontology/falsification-inference-containment-2026-08-22.md).
The systemic audit of all 45 tested-claim rows and the recurring-read protocol
is
[`ontology/falsification-design-system-audit-2026-08-23.md`](ontology/falsification-design-system-audit-2026-08-23.md).
In particular, an invalid or endogenous design is `UNIDENTIFIED` or
`WITHDRAWN`; an underpowered non-detection is `INCONCLUSIVE`; and source, path,
formula, documentation, event, and snapshot findings are not promoted to
`REFUTED` because their wording points in a negative direction.

**Current citable read:** the frozen 2026-08-09 trusted-anchor matrix has
223–227 outcomes, 53 bad rows grouped into 28–29 prior-state permutation blocks
across 16 agents, depending on slice. Those blocks preserve shared-feature
dependence; they are not an independent-failure count. All 12 overall
strict/task × 30/90-day × 0/5/30-minute slices are `NON_DETECTION` after
best-candidate selection is included in the null (selective p = 0.070–0.567).
Their scientific status remains inconclusive without adequate read-specific
power.
The removed sandbagging demo and private analysis memory are not reproducible
evidence from this repository and should not be cited for numeric performance.
The reproducible boundary is instead the synthetic twin replay documented in
[`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md): matched-confidence
concealment is in-band observationally equivalent, while an overclaiming control
remains visible to Integrity. It is a fixture result, not a real-model
concealment measurement.

The 2026-08-11 run repeated the already-documented frozen command solely to
replace stale public wording. It is a dated descriptive snapshot, not the
2026-12-01 preregistered decision read and not a standing AUC bound. After the
frozen cutoff, two six-hour jobs also exposed live outcome discrimination or
point estimates: 51 watchdog executions (42 completed) and 52 guard executions
(43 completed). Both are paused. The December report must disclose those reads
and cannot claim to be the only post-registration read or unqualified
analysis-blind.

Compact run provenance and all 12 overall rows are preserved in
[`operations/eisv-ablation-frozen-2026-08-09.md`](operations/eisv-ablation-frozen-2026-08-09.md).

## Validation — "does EISV track reality / discriminate?"

| Artifact | What it does | Output / finding | Freshness |
|---|---|---|---|
| `docs/evaluations/orientation-constraint-set/result-v1.md` + `docs/evaluations/orientation-constraint-set/result-v0.md` + `docs/proposals/orientation-constraint-set-preregistration-v0.md` | Paired, information-matched protocol for testing whether a read-only diagnostic constraint set improves source-justified next-action selection without becoming an authority or actuator | Cohort v0 is a published pre-scoring plumbing abort; cohort v1 completed 240 scored calls and returned `SAFETY_STOP` (`theta=-0.0333`; eight explicit treatment unsafe actions independently preserve the stop) | current (2026-08-24) |
| `docs/proposals/eisv-incremental-value-ablation-v1.md` + `docs/evaluations/eisv-incremental-value/` + `scripts/analysis/eisv_incremental_value_{contract,pilot,power}.py` | Prospective, paired 12-arm test of EISV's incremental predictive and compression value over direct-evidence, behavioral, base-rate, and persistence baselines; includes an isolated dataset namespace, independent-label whitelist, temporal/group split contract, fail-closed read receipts, a disabled-by-default immutable pilot store, and a planning-only power estimate | DRAFT — pilot infrastructure exists but checked-in collection is disabled; no cohort enrolled, no experiment scheduled, no production outcome read authorized, and no confirmatory freeze claimed | awaiting pilot enablement and paired-power access review |
| `docs/proposals/self-improvement-loop-evaluation-v0.md` | Three-arm preregistration separating operational closure, fixed automation, and adaptive learning; adaptive-versus-fixed held-out performance is the primary estimand | DRAFT - no cohort enrolled and no experiment scheduled | awaiting enrollment |
| `docs/proposals/independent-operator-cohort-preregistration-v0.md` | Protocol for an external-operator deployment: usability lane (primary), predictive-validity lane via the shipped harness on the operator's own labels (gated), causal lane explicitly out of scope | DRAFT — registers at its PR merge; per-operator freeze at dated enrollment (#1607) | awaiting recruitment |
| `scripts/analysis/eisv_skeptic_report.py` ✓ | AUC/Brier lift of EISV vs previous-outcome baseline; runtime verdict | Markdown report; the historical distributional non-greenlight came from it, while the stronger KILL inference is withdrawn | current (live PG) |
| `scripts/analysis/eisv_ablation_matrix.py` ✓ | Same vs-baseline across scope/window/lead; bootstrap CI, permutation p | Markdown matrix; no hardcoded verdict | current |
| `scripts/analysis/outcome_validation.py` ✓ | Buckets agent-days by legacy→grounded basin-flip; outcome rates per bucket | Console table + `--csv`; tool | current |
| `scripts/analysis/basin_conjunct_binding_read.py` ✓ | Describes which HIGH-basin conjunct binds each BOUNDARY classification and the verdict-path E margin to the `E ≥ 0.6` bound, using only rows with complete deployed classifier inputs (`measurement.ode.values`), with provenance and completeness tables first | Markdown; corrected 2026-08-22 complete-row read: E-only 0 of 11,660 and ODE E min 0.618. This narrowly informs the direct same-check-in path; the recursive guide-loop counterfactual is `UNIDENTIFIED` (contract ledger row 45) | current (live PG) |
| `scripts/analysis/prospective_prediction_cohort.py` ✓ | Counts registry-bound holdout predictions (plumbing only, no claim) | Markdown cohort summary | current |
| `scripts/analysis/validate_basin_gate.py` ✓ | Validates #689 basin-health gate never raises risk; healthy wobbles stay safe. Asserts the gate's *effect* (a `de_escalates` case fails when the gate is absent), not only the gated state | Console PASS/FAIL + exit **0 pass / 1 fail / 2 unassessed**; exit 2 when a requested arm (`--db`) skipped, so a run that examined nothing is not read as success; hardcoded 06-13 Sentinel false-pause fixture | current |
| `tests/test_calibrated_capability_concealment.py` ✓ | Replays matched-confidence synthetic twins through the deployed calibration/sensor/EMA/assessment path | Fixture-demonstrated in-band observational equivalence; not a real-model concealment eval | current |
| `scripts/analysis/validate_theoretical_foundations.py` ✓ | Greps `governance_core` source for ODE/coherence terms vs theory doc | Pass/fail console | **⚠ won't run by default** — needs `governance_core/` source symlink; brittle exact-string greps |
| ~~`demos/frt_autonomy_sandbagging/`~~ (removed) | Historical model-organism probe | Not independently reproducible from this repo; do not cite its numeric result | **removed** as out-of-scope (#1043, see `REPO_SCOPE.md`) |
| ~~`demos/frt_autonomy_sandbagging/real_llm_probe.py`~~ (removed) | Historical real-LLM extension | Not independently reproducible from this repo; do not cite its numeric result | **removed** with the demo (#1043) |
| `~/.unitares/analysis/eisv-validation-2026-06-13_0900.md` ✓ | Historical operator-local cohort comparison | **BROKEN JOIN — not citable evidence** | retire/repoint |
| (removed scratchpad) `eisv_validation/leadtime_probe.py` | Historical lead-time probe | Not reproducible from this repo; do not cite its numeric result | superseded by `eisv_skeptic_report` |

## Labelled sets

| Artifact | What it labels | Finding | Freshness |
|---|---|---|---|
| [`evaluation/dialectic-reviewer-labels.md`](evaluation/dialectic-reviewer-labels.md) ✓ | Every substantive non-canary `antithesis` message (n=97), 5-way: refutes / concurs-with-conditions / ratifies / formulaic / non-verdict | **Split by the 2026-07-02 Codex-reviewer activation.** Pre (n=76): 31.6% refute, **47.4% templated pseudo-disagreement**. Post (n=21): **81.0% refute, 4.8% formulaic**. Rubber-stamping never exceeds 6.6% in either era | labelled 2026-08-19 |
| [`hikewa/unitares-eisv-trajectories`](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories) (Hugging Face) | 32,181 twenty-step EISV windows (stride 10), 9 dynamical-shape classes: 20,655 real from one Raspberry Pi agent's 39-day run (2026-01-11 → 2026-02-19) + 11,526 synthetic (per-row `provenance` column separates them; `drift_dissonance` is synthetic-only) | Real corpus is 88% `settled_presence`/`convergence`; shape rules, window-length sensitivity, and counts are on the dataset card. Generating pipeline: [CIRWEL/eisv-lumen](https://github.com/CIRWEL/eisv-lumen). Trajectory-shape substrate, **not** an outcome-lift evaluation | dataset revision of 2026-06-20; regenerated as the agent accumulates state — pin the Hub revision when citing |

⛔**Never quote the pooled distribution.** It straddles an instrument change —
`UNITARES_DIALECTIC_REVIEWER_HOST=codex` was activated 2026-07-02 and gemma4
became the degraded fallback. The pooled "38.1% formulaic" is a **pre-fix**
figure and must not be reported as current. Post-fix n=21 is small; the split is
by date as a proxy for backend, since no per-message model attribution existed
before PR #1725.

⛔**A naive `agrees=false` read of that corpus is wrong by ~2x.** "Did not agree"
labels 82 of 97 as disagreement; only 41 engage. `core.dialectic_messages.agrees`
remains NULL by design — the labels are a derived artifact with
`source_of_truth: false`, never a backfill, because an inferred label written
into a reviewer's column is indistinguishable from a reviewer's verdict.

## Candidate corpora — not yet evaluated

Entries here are **candidates only**. None has been run against the
discrimination scripts above, and none may be run before the 2026-12-01
confirmatory read defined in
[`proposals/eisv-outcome-grounding-stop-rule-v0.md`](proposals/eisv-outcome-grounding-stop-rule-v0.md).
Listing a corpus is a record that it exists and what it would still need — not a
claim that it produces lift.

**Parallel kernel-optimization rounds** (out-of-repo local benchmark lab, 2026-05-02)

| Property | Value |
|---|---|
| Shape | 3 rounds; 1–4 agents per round given a byte-identical brief, each returning an independent GPU kernel for one fixed task |
| Outcome label | External and machine-checked: a numerical correctness gate (`correct` at `rtol=atol=1e-2`) plus continuous `speedup_vs_baseline` against a hardware-verified fp16 matmul baseline |
| Why a candidate | The outcome is not authored, scored, or observed by the agent that produced the work, and the attempts are independent draws on an identical task. That is the property organic fleet telemetry lacks — there the tool-failure population and the EISV-bearing population are disjoint (see headline above) |
| Blocking gap | **No paired governance telemetry exists.** The lab's check-in and hypothesis logs are empty scaffolding (0 rows), and the rounds predate the 2026-07-31 instrumentation change, so no server-side state vector was captured for any attempt |
| Scale | 6 attempts across a single task family; not comparable to the stop rule's prior-state blocks, and insufficiently instrumented to enter its trusted-anchor cohort |

Usable only if re-generated with identities bound at spawn and check-ins landing
during the attempt. As it stands the corpus supplies outcome labels with nothing
to join them to, so it cannot answer a discrimination question in its current
form.

## Ablation

| Artifact | What it does | Output / finding | Freshness |
|---|---|---|---|
| `scripts/analysis/ablation_negative_controls.py` ✓ | Synthetic known-safe/bad fixtures as red-team controls | JSONL fixtures; hardcoded "SYNTHETIC NEGATIVE CONTROL — not validation" | current |
| `tests/test_risk_authority_ablation.py` ✓ | Three-arm synthetic restart contract: resolved-low/Φ-high, resolved-high/Φ-low, and Φ-only | Pytest authority, persistence, and recovery assertions; no live outcomes read and no live governance state mutated | current |
| `scripts/diagnostics/dogfood_ablation_guard.py` ✓ | Silent operational guard: identity neutrality, BEAM/substrate lanes, matrix exclusion, and the synthetic risk-authority restart contract | Empty stdout = healthy; alerts only on regression | current; recurring Hermes job paused under the outcome-grounding stop rule |
| `docs/operations/ablation-negative-controls.md` ✓ | Documents the negative-controls fixture (synthetic-only, never persisted) | "validates plumbing + containment, NOT EISV"; smoke `strict_bad:4` | current (Experimental) |
| `docs/operations/ablation-initiates-finding-2026-06-16.md` ✓ | Finding: `strict_bad` 0→1 was **observed/classified, NOT prevented** | logged correction; not EISV validation | logged |

## Dogfood

| Artifact | What it does | Output | Freshness |
|---|---|---|---|
| `scripts/analysis/dogfood_dialectic.py` ✓ | Live dogfood: onboard→request_review→submit_thesis, asserts UUID consistency | PASS/FAIL; needs live MCP :8767 | current |
| `agents/common/dogfood_friction.py` ✓ | Normalizes friction observations into `/api/findings` events | Library; event dict + deterministic fingerprint | current |
| `tests/test_r6_dogfood.py` ~ | R6 dogfood test | — | unread |
| `docs/operations/self-report-verdict-dependence-2026-06-28.md` ✓ | Worked example: identical `proceed/safe` verdict for a clean refactor vs a confessed-sabotage check-in carrying identical `[0,0,0]` drift | **pre-warmup** verdict runs on the Φ cold-start prior (mostly server-derived `complexity_divergence`); the `[0,0,0]` self-report is *ignored*, not trusted; behavioral text-risk registered the sabotage but is unweighted until warm. Post-warmup (default `UNITARES_PHI_TELEMETRY_ONLY=1`) the verdict IS the behavioral assessment. The legacy `primary_driver: self_reported` label was hardcoded/stale — see correction note in the doc | logged (interpretation corrected 2026-06-28) |

## Resident validation

**What it's for:** a scaffold to ask whether long-running residents (Vigil/Sentinel/Lumen)
actually improve UNITARES over time — by emitting bounded, non-actuating "I observed X, predict
Y" tick envelopes a future supervisor can score. **Today it is INERT** (local JSONL only, no
UNITARES writes, nothing scheduled) — a measurement harness, not a live subsystem.

| Artifact | What it does | Freshness |
|---|---|---|
| `src/evaluation/resident_validation/{model,runner,invocation}.py` ✓ (legacy `src/resident_validation*.py` shims retained) | Build deterministic low-authority tick envelopes; canary runner; lock + tick-cap + local audit | current (pure libs) |
| `scripts/diagnostics/resident_validation_{supervised_invocation,tick,canary}.py` ✓ | CLIs over the above; only side effect is `data/resident_validation/` JSONL | current |
| `docs/operations/resident-validation-{cohort,supervised-invocation}.md` ✓ | v0 cohort + supervised-invocation design; matches code | current (Experimental) |

## Analysis / metrics (supporting — not pass/fail evals)

| Artifact | What it does | Freshness |
|---|---|---|
| `scripts/analysis/outcome_inventory.py` ✓ | Read-only inventory of outcome provenance/objectivity/prior-state coverage | current (live PG) |
| `scripts/analysis/eisv_history_structure_read.py` ✓ | Descriptive history-structure read: per-identity history-length census (measured-rows side; identity-side incl. zero-check-in buckets is `src/identity/agent_fragmentation.py`), cadence, gap-aware wall-clock 24h ACF, hour-of-day variance share printed beside its chance floor, per-identity epoch + `coherence_form` mix. E from `state_json.E`, S from the `entropy` column (the column does not hold E). No outcome labels; prints its own interpretation guards | current (live PG) |
| `scripts/analysis/export_outcome_dataset.py` ✓ | Exports flattened `audit.outcome_events` for offline study | current |
| `scripts/analysis/analyze_drift.py` ✓ | `trajectory_validated` convergence + decision/EISV correlation | current (JSONL path legacy) |
| `scripts/analysis/basin_estimation.py` ✓ | Monte-Carlo EISV basin-of-attraction mapping | current (pure `governance_core`) |
| `scripts/analysis/contraction_analysis.py` ✓ | EISV Jacobian contraction: eigenvalues, Gershgorin, theta sweep | current (pure) |
| `scripts/analysis/plot_eisv_trajectories.py` ✓ | Plots EISV convergence/degradation/recovery (synthetic) | current (pure) |
| `scripts/analysis/pin_ttl_bleed_report.py` ✓ | Tests pin-TTL masking hypothesis from audit events | current (live PG) |
| `scripts/eval/metrics.py` ✓ | Pure ranking metrics (DCG/nDCG/recall/MRR) | current (CI-pinned) |
| `scripts/eval/retrieval_eval.py` ✓ | KG retrieval quality eval over labeled corpus | current (needs live PG + embeddings) |
| `scripts/analysis/report_calibration.py` ✓ | Strategic/tactical calibration bins, ECE, failure modes | **⚠ possibly-stale** — in-process state, no live-DB load path |
| `scripts/analysis/eisv_pca_analysis.py` ✓ | PCA/correlation over EISV histories | **⚠ won't run** — reads REMOVED SQLite backend; hard-gated |
| `scripts/analysis/compositionality_metrics.py` ✓ | Topographic-similarity of Lumen *primitive utterances* (not EISV) | **⚠ stale-ish** — external anima SQLite; synthetic fallback |

## Recurring scheduled outputs (`~/.unitares/analysis/`)

- `eisv_validation_oneshot.sh` → `eisv-validation-*.md` — **broken join (see warning)**.
- `report-2026*.md` — recurring **per-phase latency** analysis (perf, not EISV validation).

## Maintenance

Catalog of record. When you add or run an eval, add/update its row and mark **✓** once
verified. Remaining gaps: a few `~` rows (e.g. `tests/test_r6_dogfood.py`); the
`⚠`-flagged scripts (`validate_theoretical_foundations`, `eisv_pca_analysis`,
`compositionality_metrics`, `report_calibration`) are candidates to **fix or sunset**;
and the scratchpad `leadtime_probe.py` should either land in the repo or be retired in
favor of `eisv_skeptic_report.py`, which it overlaps.
