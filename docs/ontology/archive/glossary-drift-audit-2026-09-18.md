# Glossary Drift Audit — 2026-09-18

**Method:** Re-read every factual claim in `docs/ontology/glossary.md` against
the code and docs it cites, at master `f0261eaf`. Then swept the runtime
glossary (`src/governance_glossary.py`), the coherence and state-storage
modules, and `docs/EISV_COMPUTATION.md` for load-bearing words that answer more
than one question but have no glossary entry. Same flagging rule as the
2026-06-20 audit: a term is **DRIFT** when a word answers different questions
in different places with no marker saying which sense is meant, or when the
glossary's statement of a source no longer matches that source.

**Mechanical gate:** `scripts/dev/check_glossary_drift.py` passed before and
after this sweep. Every finding below is semantic, which is the part the gate
cannot see.

**Outcome:** Findings 1–4 are fixed in `glossary.md` in the same change. This
file is the point-in-time evidence.

## Findings

### 1. Glossary quoted a sentence its source had retracted (DRIFT, high)

`glossary.md` quoted `EISV_COMPUTATION.md` as saying the ODE "runs in parallel
and does NOT drive verdicts", and derived its headline reading from it ("two
loops", "the physics is built but not wired"). `EISV_COMPUTATION.md` was
corrected on 2026-08-29 (#1992): the ODE is not the verdict *owner*, but the
behavioral sensor still blends the legacy `C(V)` term into 25–30% of `E` and
30–40% of `I`. The old line "describes authority, not causal independence."
The runtime glossary had already been updated to match
(`EISV_DIMENSIONS["E"]`: "still contains a legacy ODE control-feedback level
term"); the doc glossary was the stale copy.

The first draft of this fix overcorrected: it said the `C(V)` term was the
ODE's *only* verdict-path influence. Review found three more channels: the Φ
cold-start prior that owns check-ins 1–2 is evaluated on the ODE state
(`src/monitor_phi.py`); the ODE-derived regime inputs to the behavioral sensor;
and the confidence fallback, 55% of whose base is `C(V_ODE)`
(`src/confidence.py`).

**Fix:** the Rosetta correction section now carries a dated second correction:
the ODE owns the cold-start verdict, does not own the post-warmup verdict, and
reaches it after warmup through compatibility couplings. The `◐ research-lens`
definition and the `free energy (ODE V)` note were brought in line with it.

### 2. Physics-register word in a published file (DRIFT, medium)

The Rosetta correction called `governance_core/dynamics.py` a "thermodynamic
ODE" twice. `glossary.md` is rendered into the public-site glossary viewer, and
its own guardrail is "name nothing more rigorous than it is".

**Fix:** "dynamical-systems ODE"; the second occurrence was removed in the
rewritten passage.

### 3. Unmarked homonyms missing from the glossary (DRIFT, medium)

- **`V`** — three quantities: behavioral EMA of E−I (`metrics['V']` once
  warm), the ODE damped integral (which coherence always reads), and the
  embodied instantaneous imbalance. Plus two naming layers (Valence in reader
  docs, Void in code/DB/papers) that are not extra quantities.
- **`coherence`** — three producers (`legacy_tanh_v` ODE control feedback,
  which is deployed; `manifold`, canonical only under the off-by-default
  `UNITARES_GROUNDING_APPLY`; `behavioral_assessment`).
  `src/coherence_provenance.py` already labels them; the glossary had no entry.
- **`drift`** — server-computed deviation components, the caller-reported
  `ethical_drift` input slots, and the runtime description of `S`. The
  Rosetta table listed `drift` as "already standard", which is true for public
  naming and hides the self-attested/measured split in technical prose.
- **`entropy`** — the deployed S axis is a heuristic blend labelled "Entropy";
  the paper's target is response-distribution entropy H, which is not computed
  on the primary path. Same deployed-vs-target split as `free energy`.

**Fix:** four new entries under "High-risk homonyms". Persisted column names
(`core.agent_state.entropy` holds S, `volatility` holds V, E only in
`state_json.E`) are single-sense names, not homonyms, so they went into a new
*Persistence false friends* note rather than the homonym table.

### 4. Open gap overstated the lease schema (DRIFT, low)

The BEAM-resident-harness gap called `hermes / claude_code / codex / dispatch /
lumen` "the lease enum". The only place that list appears is
`beam-coordination-kernel.md`, as an open "etc." list; no closed enum
enforcing it was found (the lease-plane source itself was not located during
this sweep). The gap is still real: no value has been named for a BEAM-resident
body.

**Fix:** reworded as "documented values … an open list rather than a closed
enum".

### 5. Stale code comment on the Φ default (code drift, low)

`src/governance_monitor.py`, at the `resolve_verdict_risk` call site, said "Φ
floors verdict/risk by default". The flag `UNITARES_PHI_TELEMETRY_ONLY`
defaults to on (`config/governance_config.py::phi_telemetry_only`), so by
default Φ is telemetry. The glossary's Φ entry was already correct.

**Fix:** comment corrected.

### 6. Stale bounds in the ODE module docstring (code drift, low)

`governance_core/dynamics.py` gave V as `[-2,2]` and S as `[0,2]`.
`governance_core/parameters.py` `DynamicsParams` clamps V to `[-1, 1]` and S to
`[0.001, 1]`; the "~2.0" in `dynamics.py` is the width of the V range, not a
bound. Live rows agree: over 30 days no ODE V left `[-1, 1]`. The first draft of
this change copied `[-2, 2]` into the glossary.

**Fix:** docstring bounds corrected; the glossary V rows now say both V senses
share `[-1, 1]`, so range cannot tell them apart.

## Review round

Three independent reviews ran on the first draft: a conceptual review against
the glossary's own rules, a source-accuracy review of every checkable claim,
and a read-only check against the live database and configuration. The
source-accuracy review found no factual errors. The conceptual review produced
the finding-1 overcorrection, the reclassification of persisted column names,
the `entropy` homonym, and the removal of the `V (coherence input)` row (a
routing fact, not a separate question). The live check confirmed the storage
mapping (7,155 of 7,155 recent rows), that 99.76% of stored rows over 7 days
carry `coherence_form = legacy_tanh_v` (the rest are synthetic fixtures), and
the live flag state; it also found finding 6.

## Re-verified, no drift

- Φ is telemetry by default and in the live configuration; the cold-start prior
  (`phi_cold_start`) remains its verdict-shaped role.
- `src/grounding/free_energy.py` tier-1 still raises `NotImplementedError`.
- `BASIN_E_HEALTHY = 0.60 == BASIN_HIGH.E_min`; the runtime `BASINS` entry
  still describes the health-band sense, consistent with the `basin` entry.
- `governance_core/dynamics.py` still describes V as "like Helmholtz free
  energy".
- Every canonical-source document named in the glossary exists.

## Not swept

Registers other than `ops` and `fep` were not re-read against their sources;
the single-sense table was checked only for cited-file existence. Re-run and
re-date when the vocabulary moves.
