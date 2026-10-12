# Paper evidence audit — 2026-10-11

Status: completed source-and-evidence audit; correction text prepared below.
Paper sources, PDFs, releases, runtime formulas, and registered gates are unchanged.

## Decision

Keep the reproducible findings and the registered decisions. Make targeted
corrections at the next paper revision; a wholesale rewrite is not warranted by
this audit. A signal can carry information while contributing little to a
particular threshold rule. Conversely, changing labels does not establish that
the replacement labels improve decisions.

This audit checks selected operational and empirical claims against their
named artifacts. It is not a complete mathematical peer review, an external
replication, a PDF/source parity check, or a verification of the current Zenodo
deposits. Source heads were fetched before inspection; no live outcome query,
protected discrimination read, or new experiment was run.

## Pinned scope

| Repository | Reviewed commit | Surfaces |
|---|---|---|
| `unitares` | `5c871945f3596770722d4e144c193729527fd7de` | Evaluation catalog, evidence contract, registered gates, signal definitions |
| `unitares-paper-v6` | `58d77d915109f3a52ab2c4cc7156e947b5fe5251` | v6.9.3 abstract, operational coherence, migration and conclusion claims; README; RELEASE |
| `digital-proprioception-paper` | `a5dae00dec60c897cbe52573e4a77996679f24b8` | v2.0 instrument definitions, replay, adjacent questions, limitations, conclusion; README; reproduction scripts |
| `trajectory-identity-paper` | `e6bd3c4e166fd5b13d46d21be9fd71b603daa836` | v0.17 abstract, component definitions, similarity, pilot correction, conclusion; README; workshop status |
| `tonal-sonification-paper` | `3d5b3d3f316e0c7e67d233b04f78e24cc0356d4d` | README and teaching metaphor; an early research repository, not a completed validation paper |

The shared paper checkouts were behind their remote heads. All paper claims in
this audit refer to the pinned remote sources, not those older working copies.

## What is already supported or correctly bounded

**Frozen replay.** Ran the two standard-library scripts from the pinned digital
proprioception source against copies of its two frozen CSVs. Both scripts exited
0; `reproduce_basinflip.py` verified both pinned input hashes. Recomputed results:

- 3,834 / 13,292 labels change: **28.84%**.
- 26,574 / 26,584 individual basin labels match the export; ten discrepancies
  are within the documented rounding margin. Maximum coherence discrepancy:
  `6.68e-4`.
- Setting legacy coherence to 1 changes **6 / 13,292** basins (0.045%).
- The legacy score still carries descriptive class information: pairwise folded
  AUC reaches **0.877**, and correlation with behavioral V is **0.726**.
- The second frozen window gives **44.3%** flips; 12,177 stored-value tuples
  match between windows with multiplicity. These are overlapping windows, not
  independent replications.

These reproduce descriptive arithmetic. They do not validate the replacement
score, establish independent samples, or establish prevention. In particular,
the row-level Wilson intervals printed by the reproduction script were not
validated here as dependence-aware inferential intervals.

The same-row formula/calibration ablation (11.2%, 23.5%, 28.9%) uses the original
13,310-row pull. The paper already discloses that the public export lacks the
`regime` column needed to re-run two arms. Those figures remain provenance-backed,
not newly reproduced by this audit. See the pinned
[reproduction record](https://github.com/cirwel/digital-proprioception-paper/blob/a5dae00dec60c897cbe52573e4a77996679f24b8/analysis/phase-2-2026-04-18/REPRO.md).

**Forecasting.** The paper correctly calls the question unresolved, discloses
interim outcome access, and withdraws the old negative interpretation. The
[August frozen matrix](../operations/eisv-ablation-frozen-2026-08-09.md) records
non-detection, not proof of no information. Missing null geometry and withdrawn
power calculations cannot supply a scientific bound.

**Identity.** The v0.17 abstract and conclusion correctly retain within-agent
observations while treating the between-agent pilot as confounded. The
production-similarity audit and the pilot use different instruments; one does
not directly refute the other. Preserve that distinction.

## Gates and ablations already in place

| Instrument / decision | What the record authorizes | What it does not authorize |
|---|---|---|
| [Outcome-grounding stop rule](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md) | Fixed December 1 decision; all four PASS conditions required. PASS keeps outcome-grounding open and allows Stage B to be reconsidered. FAIL closes the track for this premise, with reopening restricted. | Calling insufficient support scientific disproof; moving thresholds; treating a contaminated read as clean confirmatory evidence; automatic intervention authority on PASS |
| [Individuality-v2 record](../ontology/eisv-proprioception-contract.md#tested-claims--ledger) | The July 30 FAIL and operational retirement are honored; no v3 on the same measurement premise. | General disproof of individuality: the contract documents identification failures in the test |
| [Legacy-coherence dependency ablation](../proposals/registered/legacy-coherence-dependency-ablation-v0.md) | Shadow the contribution of controller variation to behavioral E/I and confidence; require the specified safety and recursive-replay gates before promotion. Consult the catalog's v0/v0.1 distinction before any execution. | Interpreting a same-input shadow as the alternative agent history; making a live replacement from narrow coherence spread alone |
| [Legacy-coherence identity ablation](../proposals/active/legacy-coherence-identity-ablation-v0.md) | Measurement-only shadow; the September 23 status says the shadow is built and aggregation is not done. | Treating the status note as a new live census or a completed efficacy result |
| [Incremental-value protocol](../proposals/eisv-incremental-value-ablation-v1.md) | Draft comparisons for incremental prediction, compression and dimension contribution; explicitly no cohort enrolled or experiment scheduled in the reviewed text. | Calling draft thresholds registered/frozen, counting a design as a result, or inferring causal guidance benefit from Phase I |

The [evaluation catalog](../EVALUATION_INDEX.md) is the starting point for further
work. This audit creates no new evaluation program and changes no gate.

## Targeted correction packet

### 1. Digital proprioception: do not promote operational PASS automatically

**Source:** [paper.md §7](https://github.com/cirwel/digital-proprioception-paper/blob/a5dae00dec60c897cbe52573e4a77996679f24b8/paper.md#7-limitations-and-what-would-change-the-picture).
The sentence beginning “If the 2026-12-01 read passes” promises a positive
forecasting finding. The registered PASS consequence is narrower: the track
remains open and Stage B may be reconsidered. The disclosed interim accesses
also remain relevant after PASS.

**Replacement:**

> If the December 1 read passes all four registered conditions, it meets the
> operational continuation gate: outcome-grounding remains open and Stage B may
> be reconsidered. Report the observed discrimination with the specified cohort,
> dependence, power and interim-access disclosures. PASS does not itself
> establish decision benefit. If the read fails, report the failed conditions
> and honor closure for this measurement premise; insufficient support is not
> scientific disproof.

**Same paper, conclusion:** “before calling it a measurement of anything” is
broader than the paper's own reproduced class information and threshold effects.
Replace its final sentence with:

> The next step for a replacement is an outcome-graded comparison, a tested
> recalibration policy, and a label source suited to the intended claim before
> describing it as a validated health indicator or using claimed benefit to
> justify a policy change.

### 2. UNITARES v6: separate the transform's range from the observed center

**Source:** [unitares-v6.tex, Operating Range remark](https://github.com/cirwel/unitares-paper-v6/blob/58d77d915109f3a52ab2c4cc7156e947b5fe5251/unitares-v6.tex#L569).
The remark moves from `V ≈ [-0.1, 0.1]` to `C(V) ≈ 0.49` and attributes the
observed narrow range to damping. With the specified unit gain and ceiling,
that interval maps to approximately `[0.450166, 0.549834]`; it does not identify
the distribution's center. Digital proprioception §3.3 explicitly leaves the
cause of the observed void-coordinate location unisolated.

**Replacement:**

> For unit gain and ceiling, V in [-0.1, 0.1] maps to coherence between about
> 0.450 and 0.550. The deployment's observed concentration near 0.49 is an
> empirical property of its input trajectory; the transform alone does not
> determine that center. Damping is part of the dynamics, but the evidence
> presented here does not isolate its causal contribution to the observed
> distribution. Assess this score's decision contribution against the deployed
> thresholds, separately from information retained in its variation.

This is a local inference correction, not a finding that the ODE is invalid.
The contraction certificate's complete assumptions and proof need a separate
mathematical review if that claim is to be revisited.

### 3. UNITARES v6 release instructions and README: prevent old claims returning

**Source:** [RELEASE.md](https://github.com/cirwel/unitares-paper-v6/blob/58d77d915109f3a52ab2c4cc7156e947b5fe5251/RELEASE.md)
still offers an old abstract for copying. It says stability is preserved under
the reformulation, whereas v6.9.3 requires re-certification for grounded
coherence; it also says per-class calibration is deferred although the paper
reports it. The instruction to paste this obsolete block is the defect.

**Correction:** replace the embedded abstract with a pointer to the abstract in
the tagged `unitares-v6.tex`, and require release metadata to match that version.
Do not silently change an already archived version or claim this audit checked
the live deposit.

**README insertion:**

> This paper describes the dated deployment and migration studied in its
> February–April 2026 evidence. Its information-theoretic definitions are
> research targets. For the current implementation and decision path, consult
> UNITARES's EISV computation contract; the paper is not the live specification.

### 4. Trajectory identity reader summary: preserve the derived-component boundary

**Source:** [README.md](https://github.com/cirwel/trajectory-identity-paper/blob/e6bd3c4e166fd5b13d46d21be9fd71b603daa836/README.md)
describes six quasi-invariant components without explaining there that Eta is
a derived summary excluded from the weighted similarity. Its plain-language
summary also says the framework detects impersonation, while the main paper
leaves discrimination open and distinguishes behavioral continuity from the
credential-based write gate.

**Replacements:**

> The signature contains five scored components plus a derived homeostatic
> summary, Eta. Eta is not an additional weighted term in similarity (§3.6–§4.1).

> The framework proposes behavioral continuity and anomaly checks. The current
> evidence does not establish individual recognition across harness changes or
> validated impersonation detection; operational authorization remains separate.

The workshop variant is already labeled stale in the README. Backport the main
paper's corrections before submission; no immediate rewrite is needed to
preserve an explicitly historical draft.

### 5. Sonification: date the findings and distinguish the two V variables

**Sources:** [README](https://github.com/cirwel/tonal-sonification-paper/blob/3d5b3d3f316e0c7e67d233b04f78e24cc0356d4d/README.md)
and [metaphor](https://github.com/cirwel/tonal-sonification-paper/blob/3d5b3d3f316e0c7e67d233b04f78e24cc0356d4d/docs/tonality-metaphor.md).
The June 17 README records the old doubly smoothed V. The current
[`_raw_valence`](../../src/behavioral_state.py) documents the June 19 change to
raw `E_obs - I_obs`, followed by V's own EMA. The metaphor uses behavioral V
and the ODE void argument as though they were the same, and calls the ODE
parallel-only despite its remaining feedback and structural checks.

**Replacement framing:**

> These are June 17, 2026 observations under the then-current V formula. Current
> behavioral V smooths the raw E–I observation gap once. Legacy coherence reads
> a separate ODE void coordinate; it is not a direct transform of behavioral V.
> The historical three-episode result motivates an S-forward rendering for
> that sample, not a general conclusion about the best signal or audible lead.

Replace the parallel-only statement with the current
[computation contract](../EISV_COMPUTATION.md). Repair the extracted document's
local links to files absent from that repository. Retain the honest statement
that audible lead has not been demonstrated.

## Delivery and follow-through

This packet is sufficient to prepare small, reviewable paper-repository changes
without reopening experiments. Apply corrections 1–2 in a versioned paper
revision with matching PDF and metadata; apply 3–5 to reader and release
surfaces with their source links. Keep historical results, cohort limitations,
and stop-rule consequences explicit. Do not recast a pending test as missing
validation infrastructure, or a narrow result as either universal proof or
universal failure.
