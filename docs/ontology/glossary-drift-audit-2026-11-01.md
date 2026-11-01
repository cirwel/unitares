# Glossary Drift Audit — 2026-11-01

**Scope:** Focused semantic refinement of `glossary.md` against master
`9983af89b`. This is not a new empirical study or an exhaustive re-verification
of every historical entry. The glossary's question-first structure and parser
sections remain intact.

**Outcome:** The corrections below are included in the same change. No runtime,
identity policy, enforcement setting, scientific threshold or review gate changes.

## Recommendation, action and hold had been collapsed

The pause entry said the policy decision caused the persisted pause. Under the
default `UNITARES_POLICY_ACTUATION=observe`,
`src/agent_loop_detection.py::apply_policy_actuation_posture` preserves the raw
recommendation in `policy_evaluation` and projects a pause into effective
`proceed` / `guide` with `enforcement.applied=false`. Even `enforce` permits an
attempt; the effective action alone does not prove a durable hold was applied.

The pause and verdict entries now separate recommendation, effective directive,
response wrapper and lifecycle state. Explicit operator/security, identity,
archive and coordination gates keep their own authority. Check-in margin and
recovery margin are also distinct: the latter's `recovery.margin.v2` excludes
legacy coherence (`src/mcp_handlers/lifecycle/recovery_policy.py`).

## Thread membership had been mistaken for UUID continuity

The sibling-locus row required the same UUID and no mint. The September
additions to `r6-episode-fork-response-shape.md` and
`src/thread_identity.py::fork_honest_message` explicitly cover fresh UUIDs on a
thread occupied by unrelated processes. The row now covers both cases without
asserting ancestry. The identity operational contract also distinguishes a
same-process compaction from an actual restart.

The token note called `continuity_token` deprecated as a whole. `identity.md`
still supports an explicit same-live-process proof rebind. The correction
distinguishes that from retired ordinary cross-process reuse. Lineage also
covers dispatched children of a live parent, not only succession from an exited
predecessor. Transport fingerprint inference does not become ownership proof.

## Metric variation had been overread as an empirical negative

The phi entry inferred "no outcome signal" from a lack of established
predictive lift. `docs/EVIDENCE_AND_LIMITS.md`, *Predictive lift*, calls the
frozen evaluation inconclusive, not a demonstrated negative. The entry now
states that boundary and the configured default of
`config/governance_config.py::phi_telemetry_only`, including the cold-start prior.

The behavioral V row now specifies the separately smoothed raw observation gap
`E_obs − I_obs`, rather than an ambiguous EMA of E−I. The drift note says S
contains drift; it does not equate S with the drift vector. Coherence rows name
defaults and flags rather than assert the state of every deployment.

## The shared record and report provenance needed definitions

`docs/PRODUCT_DEFINITION.md` and `docs/CAPABILITIES_AND_DEPLOYMENT.md` supply
the definitions of UNITARES, federation, findings, supersession, reconstruction
and check-ins. `docs/operations/github-workflow-conventions.md` records the
global contract's model / family / host distinction for review independence.
Outcome events and predictions now have separate entries. New confidence and review entries
separate forecasts, estimates, estimator maturity, advisory critique, governed
sessions and actual diff review. A self-report is evidence of what was reported,
not independent outcome verification; `epistemic_class` does not certify truth.

The FEP section's historical prescription for implementation order is replaced
with a pointer to the staged core-boundary proposal and claim ledger. Candidate
mathematical mappings remain visible as research; they confer no implementation
or enforcement authority. The register count is corrected from four to five.

## Verification

`python3 scripts/dev/check_glossary_drift.py` and
`python3 scripts/diagnostics/check_doc_drift.py` passed. The existing glossary
parser/runtime tests and glossary-related build/gateway/query checks passed
through `./scripts/dev/test-cache.sh --quick -- -k glossary` (60 tests).
These check structure, code references and vocabulary overlap; semantic
corrections above rely on the cited sources, not the parser.
