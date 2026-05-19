# R6 candidate envelope evaluation — 2026-05-19

**Scope:** Closes the outstanding R6 plan-row question: *"decide whether broader candidate-envelope fields have enough evidence to promote."* Read-only evaluation against today's H1/H3 dogfood keys. Does NOT promote or demote anything.

**Companion to:** `docs/ontology/harness-substrate-plurality.md` (envelope definition + 2026-05-08 audit), `docs/ontology/r6-episode-fork-response-shape.md` (the two fields already promoted), `docs/ontology/plan.md` row R6.

## Inputs

Diagnostic: `scripts/diagnostics/s22_candidate_envelope_coverage.py` against the 2026-05-19 dogfood keys.

- `r6-h1-2026-05-19` (H1 = same UNITARES UUID, two distinct Hermes models): 1 `agent_state` row, 0 KG rows. `r6_dogfood.py --assess` returned `decision=complete`, `reason=same_identity_distinct_models_observed`.
- `r6-h3-2026-05-19` (H3 = fresh identity, shared Hermes memory context): 5 `agent_state` rows, 0 KG rows. `r6_dogfood.py --assess` returned `decision=complete`, `reason=fresh_identity_shared_memory_context_observed`, with 2 distinct agent UUIDs and 3 Hermes-comparable entries.

## Field coverage by promotion group

### Promoted-core (must be present)

| Field | H1 (n=1) | H3 (n=5) | Notes |
|---|---|---|---|
| `schema` | 1/1 ✓ | 5/5 ✓ | |
| `context_source` | 1/1 ✓ | 5/5 ✓ | |
| `harness_type` | 1/1 ✓ | **2/5** | Gaps on non-Hermes H3 writers |
| `transport` | 1/1 ✓ | 5/5 ✓ | |
| `model_provider` | 1/1 ✓ | **2/5** | Same writer gap as `harness_type` |
| `model` | 1/1 ✓ | **2/5** | Same writer gap |
| `tool_surface` | 1/1 ✓ | **2/5** | Same writer gap |
| `comparison_key` | 1/1 ✓ | 5/5 ✓ | |
| `task_label` | 1/1 ✓ | 5/5 ✓ | |
| `task_outcome` | 1/1 ✓ | 5/5 ✓ | |
| `governance_mode` | 1/1 ✓ | 5/5 ✓ | |

**Finding:** The four "harness-shape" fields (`harness_type`, `model`, `model_provider`, `tool_surface`) are gappy on H3's mixed-harness flow even though they are nominally promoted-core. Three of the five H3 rows are produced by writers that don't fill them. This is **not a candidate-envelope concern** — it's a promoted-core wiring gap.

### Fork discriminator (R6 v2 promoted 2026-05-08)

| Field | H1 (n=1) | H3 (n=5) |
|---|---|---|
| `episode_fork_kind` | 1/1 ✓ | 5/5 ✓ |
| `identity_lineage_fork` | 1/1 ✓ | 5/5 ✓ |

**Finding:** The 2026-05-08 durable-provenance follow-up (`build_s22_write_context` + `classify_fork_for_s22_context` + the three-site re-stamp) holds. Both fields are now reliably present on every persisted row.

### Optional

| Field | H1 (n=1) | H3 (n=5) |
|---|---|---|
| `memory_context` | 1/1 ✓ | 5/5 ✓ |
| `verification_source` | 1/1 ✓ | 2/5 |
| `thread_id` | 1/1 ✓ | 4/5 |
| `session_resolution_source` | 0/1 | 4/5 |
| `parent_agent_id` | 0/1 | 2/5 |
| `spawn_reason` | 0/1 | 2/5 |

**Finding:** Coverage is consistent with the field's nature — `memory_context` always present when the writer opts in; `parent_agent_id` and `spawn_reason` populate only when meaningful (which is the spec). No action.

### Candidate (deferred until targeted dogfood earns them)

| Field | H1 (n=1) | H3 (n=5) | Verdict |
|---|---|---|---|
| `affordance_state` | 0/1 | 0/5 | No evidence |
| `harness_id` | 0/1 | 0/5 | No evidence |
| `episode_id` | 0/1 | 0/5 | No evidence |
| `invocation_id` | 0/1 | 0/5 | No evidence |
| `process_instance_id` | 0/1 | 0/5 | No evidence |
| `identity_assurance` | 0/1 | 0/5 | No evidence (note: it's available on identity responses; not flowing into provenance) |
| `label_at_write` | 0/1 | 0/5 | No evidence |
| `agent_uuid` | 0/1 | 0/5 | No evidence |
| `client_session_id` | 0/1 | 0/5 | No evidence |
| `locus` | **1/1** | **2/5** | Borderline signal |

**Finding:** Nine of ten candidate fields have **zero population across both keys**. The H1/H3 dogfood, which is the canonical "earn promotion" channel, is producing essentially no evidence for the broader candidate envelope. `locus` is the only candidate seeing intermittent use.

## Recommendation

**Do not promote any candidate-envelope field on this evidence.** The data says:

1. The broader candidate envelope is not earning its keep. Nine of ten fields have 0% population on the dogfood that exists specifically to give them evidence. Promoting them now would be performative — adding required-field status to data the system isn't producing.

2. `locus` is the only candidate showing any signal (1/1 + 2/5). That's still below any reasonable promotion threshold, but it's the field worth watching. If a future dogfood pass deliberately exercises it (the field captures situated transport context — Discord thread, MCP session, etc.), it could earn promotion. The other nine are effectively dormant.

3. **There is a promoted-core wiring gap that surfaced incidentally:** H3's non-Hermes writers (3 of 5 rows) don't populate `harness_type` / `model` / `model_provider` / `tool_surface`. These are nominally promoted-core, not candidate. Worth a follow-up trace: which writer produced those 3 rows, and why doesn't it fill harness shape? This is a separate concern from candidate-envelope promotion — file as R6 follow-up.

## Closes / opens

**Closes:** R6 plan-row's "next R6 work" item — "decide whether broader candidate-envelope fields have enough evidence to promote." Decision recorded here: no promotion this cycle.

**Opens (small):** trace and fix the H3 promoted-core gap on non-Hermes writers (3 of 5 rows missing `harness_type` and siblings). This is a wiring issue, not an ontology decision.

**Does not change:** the fork-discriminator promotion (already shipped 2026-05-08). The optional-field semantics. The candidate-envelope membership — `affordance_state` / `harness_id` / etc. stay listed as candidates, not dropped, since the right next step is "exercise them in a targeted dogfood" not "remove them."

## Source commands

```bash
python3 scripts/diagnostics/r6_dogfood.py --experiment h1 --assess --json
python3 scripts/diagnostics/r6_dogfood.py --experiment h3 --assess --json
python3 scripts/diagnostics/s22_candidate_envelope_coverage.py --comparison-key r6-h1-2026-05-19 --json
python3 scripts/diagnostics/s22_candidate_envelope_coverage.py --comparison-key r6-h3-2026-05-19 --json
```
