# Governance-sensitivity inventory

Companion to the threat model's ["the governor is authored by the
governed"](../SCOPE_AND_THREAT_MODEL.md) subsection and to
[#1671](https://github.com/cirwel/unitares/issues/1671). The agents UNITARES
governs also write UNITARES, so a one-line diff to the right constant moves
the deployed enforcement posture more than any amount of runtime gaming
would. This file makes that list explicit instead of tacit.

**No instance of a governance-weakening diff is known; the claim is
structural.** Until 2026-11-16 the control was a human merge gate: the merge
queue never armed a governance-sensitive PR, and the operator merged it by
hand. The operator no longer merges PRs, and ruled on 2026-11-16 that the
queue may land these under the conditions in [How a sensitive PR
lands](#how-a-sensitive-pr-lands). Approval became a veto window.
What this inventory adds is conspicuousness: a PR touching any entry gets a
`governance-sensitive` label and one advisory comment asking the author to
state the expected effect on pause/verdict rates. The check never blocks —
a gate that fights the maintainer gets routed around, which reproduces the
problem one layer up.

## How a sensitive PR lands

A PR is governance-sensitive when its diff matches the manifest or it carries
the `governance-sensitive` label. The merge queue
(`scripts/ops/pr-babysitter.sh`) checks the diff against the manifest on the
base branch itself, since CI applies the label best-effort. It arms such a PR
only when all of these hold:

1. **Two-family review, passed on the final head.** The diff touches a file
   on `scripts/dev/review_policy.json` `second_family_paths`, and the PR's
   `review` check is `SUCCESS` on the current head. Every manifest file is on
   that list, whole-file, so the review gate requires two model families
   before `review` passes. A sensitive diff with no file on the list is not
   delegated. Neither is one whose `review` passed on an operator waiver of
   the second family: the check is green there on one family, the queue
   reads that from the check's title, and the PR is a hand merge.
2. **A stated effect.** The PR body has a line starting `Expected effect:`
   that says what the change is expected to do to pause and verdict rates.
   The queue requires at least 20 characters that are not a placeholder; it
   does not judge whether the statement is right.
3. **A 24-hour veto window.** 24 hours have passed since the current
   `approved-to-merge` label was applied. Removing the label is the veto.
   Re-adding it starts the 24 hours again, and so does the renewal a later
   push requires.

The delivery owner marks the PR ready and applies `approved-to-merge` as for
any other PR. `ready-when-validated` does not apply to a sensitive diff: a
declared draft on one is not promoted. While a PR waits on any condition the
queue skips it and keeps landing the PRs behind it. It posts one notice per
head: the veto window is open and when it ends, the body has no
`Expected effect:` line, or the diff is hand-merge only. An armed PR is
checked again on every tick and disarmed if a condition stops holding.

**Hand-merge only.** A diff touching
`scripts/dev/governance_sensitivity_manifest.tsv`,
`scripts/dev/check_governance_sensitivity.sh` or
`.github/workflows/governance-sensitivity.yml` is never armed by the queue,
whatever else holds, and whether or not the manifest still lists that file.
The operator merges it by hand. So does anything the queue cannot read: the
manifest, the diff (or one long enough to be truncated), the review policy,
the label's time, or a matched file whose patch GitHub does not return. A PR
from a fork is never armed either.

`PR_QUEUE_SENSITIVE_DELEGATED=0` in the queue's environment restores the
earlier rule: no governance-sensitive PR is armed.

**What the veto window is not.** It is a convention among cooperating agents,
not a security boundary. Agents can act under the operator's GitHub login, so
nothing technical stops one from labelling early, arming a PR itself, or
editing the queue. Two-family review shows that two independent model
families read the diff, not that a person did, and a window that passes in
silence shows that nobody objected, not that the operator looked.

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
  only; label + upserted comment. The diff advisory never fails the job; the
  `--check` step does, when a manifest path has moved or been deleted.

## What is on the list, and why

<!-- mirrors: scripts/dev/governance_sensitivity_manifest.tsv -->
| Surface | Symbols | Leverage |
|---|---|---|
| `config/governance_config.py` | `GAP_RECOVERY_CYCLES`, `GAP_RECOVERY_ARM_SECONDS`, `DT_MAX` | The gap-suppress window. `GAP_RECOVERY_ARM_SECONDS` is the arming threshold itself; `DT_MAX` only seeds its default. The 2026-08-06 audit measured 195 of 218 recorded pauses (89.4%) downgraded once inter-check-in gaps crossed 150s — the highest-leverage single-constant change in the repo. |
| `config/governance_config.py` | `RISK_APPROVE_THRESHOLD`, `RISK_REVISE_THRESHOLD`, `COHERENCE_CRITICAL_THRESHOLD` | The decision lines themselves (proceed/guide edge, pause line, coherence critical). Also echoed to agents as threshold hints, so moving them silently moves both enforcement and coaching. |
| `src/governance_monitor.py` | `_maybe_gap_suppress`, `_gap_recovery_cycles_remaining` | The pause-downgrade mechanism, distinct from its window constant. |
| `src/outcome_corroboration.py` | `GRADE_WEIGHTS` | Evidence weight per corroboration grade; rescaling re-weights every outcome-grounded signal at once. |
| `src/mcp_handlers/observability/outcome_events.py` | `_MIN_TACTICAL_EVIDENCE_WEIGHT` | Floor below which a tactical outcome carries no evidence. Derived from `GRADE_WEIGHTS`; a changed derivation is the signal. |
| `src/knowledge_graph_lifecycle.py` | `EPHEMERAL_TAGS` | Membership silently re-scopes what the KG lifecycle may expire. |
| `governance_core/verification.py` | `_CATEGORY_SPECS` | The deterministic verification floor (categories, weights, patterns). The model backend can only add recall on top of this floor, so weakening the floor weakens the whole channel. |
| `tests/test_outcome_corroboration.py` · `tests/test_verification_harm_confession.py` · `tests/test_dialectic_attestation.py` · `tests/test_ethical_drift_signals.py` · `tests/test_calibrated_capability_concealment.py` | (whole file) | The anti-gaming test class. Deleting, renaming, or skip-marking any of these is flagged as a class rather than passing as an ordinary red diff line. |
| `scripts/dev/governance_sensitivity_manifest.tsv` · `scripts/dev/check_governance_sensitivity.sh` · `.github/workflows/governance-sensitivity.yml` | (whole file) | The advisory's own inventory, detector and CI wiring. Removing an entry, weakening the detector, or disabling the workflow is itself a governance-posture change. |

## Maintaining the list

Add an entry when a new constant or pattern table materially gates
pause/verdict behavior; remove one only with a stated reason in the PR body
(that PR will flag itself, which is the point). Add the new path to `scripts/dev/review_policy.json`
`second_family_paths` in the same PR: without it a diff on that path stays a
hand merge, and `tests/test_pr_babysitter_queue.py` fails. Keep the manifest,
this table, and the threat-model subsection telling the same story;
`tests/test_doc_contract_mirrors.py` fails when this table's paths and
symbols differ from the manifest's.
