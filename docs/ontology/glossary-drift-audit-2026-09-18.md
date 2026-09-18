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

**Fix:** the Rosetta correction section now carries a dated second correction
that separates authority from coupling, and the `free energy (ODE V)` note no
longer says the ODE "drives nothing".

### 2. Physics-register word in a published file (DRIFT, medium)

The Rosetta correction called `governance_core/dynamics.py` a "thermodynamic
ODE" twice. `glossary.md` is rendered into the public-site glossary viewer, and
its own guardrail is "name nothing more rigorous than it is".

**Fix:** "dynamical-systems ODE"; the second occurrence was removed in the
rewritten passage.

### 3. Unmarked homonyms missing from the glossary (DRIFT, medium)

- **`V`** — four quantities: behavioral EMA of E−I `[-1, 1]` (`metrics['V']`),
  ODE damped integral `[-2, 2]`, the ODE V that coherence reads, and the
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
- **Persisted column names** — `core.agent_state.entropy` holds S,
  `volatility` holds V, and E lives only in `state_json.E`. Reading the
  `entropy` column as E has already produced a wrong analysis.

**Fix:** four new entries under "High-risk homonyms".

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
