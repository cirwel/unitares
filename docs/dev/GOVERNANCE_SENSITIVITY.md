# Governance-sensitivity inventory

Companion to the threat model's ["the governor is authored by the
governed"](../SCOPE_AND_THREAT_MODEL.md) subsection and to
[#1671](https://github.com/cirwel/unitares/issues/1671). The agents UNITARES
governs also write UNITARES, so a one-line diff to the right constant moves
the deployed enforcement posture more than any amount of runtime gaming
would. This file makes that list explicit instead of tacit.

**No instance of a governance-weakening diff is known; the claim is
structural.** The human merge gate remains the real control. What this
inventory adds is conspicuousness: a PR touching any entry gets a
`governance-sensitive` label and one advisory comment asking the author to
state the expected effect on pause/verdict rates. The check never blocks —
a gate that fights the maintainer gets routed around, which reproduces the
problem one layer up.

## Mechanism

- **Manifest** (machine-readable):
  [`scripts/dev/governance_sensitivity_manifest.tsv`](../../scripts/dev/governance_sensitivity_manifest.tsv)
  — `path`, `symbol_regex`, `why` per entry. Symbol entries match only when
  an added/removed diff line hits the regex; a `-` in the regex column flags
  any change to the file (used for the anti-gaming test class, where
  deletion or a skip mark is the concern).
- **Checker**:
  [`scripts/dev/check_governance_sensitivity.sh`](../../scripts/dev/check_governance_sensitivity.sh)
  — `--diff <base>` lists touched entries; `--check` fails if a manifest
  path no longer exists, so a refactor cannot silently detach a sensitive
  file from the inventory.
- **CI**: `.github/workflows/governance-sensitivity.yml` — pull requests
  only; label + upserted comment; always exits green.

## What is on the list, and why

| Surface | Symbols | Leverage |
|---|---|---|
| `config/governance_config.py` | `GAP_RECOVERY_CYCLES`, `DT_MAX` | The gap-suppress window. The 2026-08-06 audit measured 195 of 218 recorded pauses (89.4%) downgraded once inter-check-in gaps crossed 150s — the highest-leverage single-constant change in the repo. |
| `config/governance_config.py` | `RISK_APPROVE_THRESHOLD`, `RISK_REVISE_THRESHOLD`, `COHERENCE_CRITICAL_THRESHOLD` | The decision lines themselves (proceed/guide edge, pause line, coherence critical). Also echoed to agents as threshold hints, so moving them silently moves both enforcement and coaching. |
| `src/governance_monitor.py` | `_maybe_gap_suppress`, `_gap_recovery_cycles_remaining` | The pause-downgrade mechanism, distinct from its window constant. |
| `src/outcome_corroboration.py` | `GRADE_WEIGHTS` | Evidence weight per corroboration grade; rescaling re-weights every outcome-grounded signal at once. |
| `src/mcp_handlers/observability/outcome_events.py` | `_MIN_TACTICAL_EVIDENCE_WEIGHT` | Floor below which a tactical outcome carries no evidence. Derived from `GRADE_WEIGHTS`; a changed derivation is the signal. |
| `src/knowledge_graph_lifecycle.py` | `EPHEMERAL_TAGS` | Membership silently re-scopes what the KG lifecycle may expire. |
| `governance_core/verification.py` | `_CATEGORY_SPECS` | The deterministic verification floor (categories, weights, patterns). The model backend can only add recall on top of this floor, so weakening the floor weakens the whole channel. |
| `tests/test_outcome_corroboration.py` · `tests/test_verification_harm_confession.py` · `tests/test_dialectic_attestation.py` · `tests/test_ethical_drift_signals.py` · `tests/test_calibrated_capability_concealment.py` | (whole file) | The anti-gaming test class. Deleting, renaming, or skip-marking any of these is flagged as a class rather than passing as an ordinary red diff line. |

## Maintaining the list

Add an entry when a new constant or pattern table materially gates
pause/verdict behavior; remove one only with a stated reason in the PR body
(that PR will flag itself, which is the point). Keep the manifest, this
table, and the threat-model subsection telling the same story.
