# KG Agent-Adoption Pilot v0

**Status:** DRAFT / offline-fixture canary passed / enrollment unreviewed / no scored run authorized

**Decision date:** 2026-08-27

**Decision:** Build a reversible test of earned agent dependency. Start with
read-only shared memory, then bounded delegation, then shadow alerts. Keep EISV
as background proprioception rather than a product-facing judgment.

## Why this exists

The target is not lock-in through incompatibility. It is a dependency agents
keep because replacing it would lose verified utility, while retaining a tested
exit. That distinction is load-bearing: a system cannot credibly claim to be
the best substrate if leaving it destroys the user's memory or workflow.

Internal agents can find many implementation defects, but they share prompts,
terminology, operator habits, credentials, and repository context. An outside
adopter contributes evidence that cannot be manufactured by adding more runs to
the same environment:

- uncoordinated task distributions and tool-selection habits;
- clean-room interpretation of names, schemas, errors, and recovery receipts;
- independent integration pressure at the portability boundary;
- revealed replacement cost under incentives not controlled by UNITARES;
- evidence that value survives without an operator teaching the intended path.

Feedback is useful, but the stronger artifact is an independently reproducible
trace: a fresh agent found prior work, used a material source, improved an
objective result, and could export the same memory into a substitute.

## Contract

The pilot tests this chain without inferring motive:

```text
eligible task
  -> tool available or context surfaced
  -> backend reachable
  -> result delivered
  -> material source used
  -> objective outcome scored
  -> net utility after latency, tokens, failures, and intervention
```

Catalog exposure, a tool call, a citation, and a successful outcome are separate
events. Missing cost or outcome data is `unknown`, never zero. The task chain,
not an individual turn, is the inference unit.

Usage volume may retire an instrument or surfacing lever. It cannot retire the
underlying shared-memory, coordination, or safety capability by itself.

## Seven experimental cells

The UNITARES and substitute backends must expose the same neutral
`search_prior_work` name, description, input schema, and normalized result
shape. The substitute is deterministic lexical retrieval over a frozen flat
snapshot of the same source corpus.

| Surface arm | Backend | Behavior |
|---|---|---|
| unavailable | none | No retrieval tool and no injected prior work |
| passive | UNITARES KG | Tool appears only in the catalog |
| passive | lexical substitute | Same catalog surface, substitute backend |
| surfaced-withdrawn | UNITARES KG | One step-1 reminder, absent thereafter |
| surfaced-withdrawn | lexical substitute | Same reminder, substitute backend |
| injected | UNITARES KG | Frozen-query results inserted on every eligible step |
| injected | lexical substitute | Same injection, substitute backend |

Assignment is fixed for a complete chain with at least three substantive steps.
Every cell receives a fresh agent identity and fresh model context; context is
retained within its chain. A frozen digest-derived cyclic schedule
counterbalances cell order. Reminder withdrawal is observed, not described as
voluntary or organic adoption.

Primary paired contrasts are:

1. surfaced-withdrawn UNITARES minus surfaced-withdrawn lexical substitute;
2. surfaced-withdrawn UNITARES minus unavailable.

Passive and injected cells are mechanism diagnostics. Post-withdrawal retrieval
is secondary continuation telemetry and cannot establish intent.

## Frozen inputs and scoring

Before enrollment, each chain declares:

- task family and at least three steps;
- frozen injection query for every eligible step;
- objective answer key and material-support mapping;
- expected source IDs present in both backend snapshots;
- provider, model, sampling seed, analysis seed, and schedule digest;
- utility weights for quality, latency, tokens, failures, invalid citations,
  and operator intervention;
- explicit handling for each missing cost.

Preflight must verify both backends retrieve their registered source. A scored
run also requires an awaited `audit.events` write/read canary. Failure blocks
the run; it must not be misread as non-use.

The initial harness supports exact enrollment/task/schedule binding, schedule
materialization, deterministic offline-fixture preflight, content-addressed
canary receipts, strict complete-result validation, and offline summarization.
Live model execution, live KG parity, production plumbing, and production audit
writes remain disabled until a later enrollment is reviewed and explicitly
enabled.

### Offline-fixture review and canary

Governed review session `8e76d528d0baa5ba` returned **HOLD** on calling the
design or production plumbing reviewed before executable evidence existed. Its
conditions narrowed the current executable scope to
`offline_fixture_validation` and found three concrete defects: a registered
source missed deterministic retrieval, impossible spoofed result rows accepted
by summarization, and SQLite restore retaining stale rows in a world-readable
file. Those defects are now fail-closed.

The content-addressed receipt at
`docs/evaluations/kg-agent-adoption/offline-fixture-canary-v0.receipt.json`
proves only the frozen offline fixture: 14 schedule rows across two complete
seven-cell blocks, backend-neutral prompt/tool rendering, retrieval of every
registered source, adversarial negative cases, runtime network/process denial,
exact private SQLite replacement, atomic receipt interruption handling, and zero
live-model/network/KG/audit/production-database operations. The receipt itself
records the review HOLD and keeps the enrollment `unreviewed`; a later
evidence-based review must independently decide whether to grant the narrower
`offline_fixture_reviewed` status.

## Audit envelope

Use generic `audit.events`, not `audit.outcome_events`.

`agent_adoption.run.v1` records experiment and enrollment digests, expected
chains/steps, schedule digest, provider/model, source snapshot digests, and
preflight/canary receipts.

`agent_adoption.step.v1` records experiment, chain instance, task family, cell,
step index, eligibility, catalog/context/injection state, reminder withdrawal,
backend reachability, receipt state, half-open timestamps, tool calls/failures,
delivered and materially used source IDs, objective quality, decomposed costs
and missingness, net utility, output hash, and operator intervention.

Raw prompts and model responses remain outside the repository in a private
`0700` directory. Audit rows carry hashes and bounded metadata. Do not add
experiment fields to the strict `audit.tool_usage.payload` allowlist; correlate
live calls by resolved agent/session and the step's half-open interval.

## Orchestration dependency gate

Delegated inference is useful only when completion is unambiguous. The first
hardening slice therefore validates a terminal-answer envelope server-side:

- `status`: `complete`, `needs_input`, or `declined`;
- `complete` requires a non-empty answer;
- malformed, running, plan-only, or otherwise nonterminal output fails closed;
- timeout errors disclose whether execution started and may still be running;
- recovery never recommends a blind retry of possibly-running work.

This does not make asynchronous delegation durable. Owner-scoped persistent
results, idempotency, cancellation reconciliation, and bounded polling remain a
separate prerequisite before exposing an async receipt.

## Alert dependency gate

Alerts start in `off|shadow`, never live surface mode. A shadow candidate must
have a deterministic content ID, replay input hash, measurement provenance,
policy/config version, suppression or degradation reason, and explicit
`delivery=not_delivered` with `reason=shadow_mode`.

The current `include_memory_suggestions=true` path is requested recall, not a
proactive alert population. Historical `mirror_signal.emit.surfaced` is also not
a delivery receipt because its bit is response-mode-derived while KG and review
content can appear in other response modes.

Promotion requires all preregistered budgets to clear together:

- confirmed-useful precision with a lower confidence bound;
- severe misses from independently reviewed sampled negatives;
- interruptions per task-hour, duplicates, latency, and response-token cost;
- net attention after useful savings, false positives, interruptions, and
  severe-miss costs.

No labels or denominator means `insufficient_data`, not success.

## Exit proof

The existing `complete_package` export contains metadata, history, and
validation only. It is not a knowledge export and its schema must not claim one.

The offline portability artifact is a neutral JSONL bundle plus a versioned
manifest containing record/byte counts and SHA-256 hashes. Full discovery
documents preserve IDs, links, status, tags, details, and provenance. The first
exit drill must:

1. reject corruption and unknown versions;
2. restore idempotently;
3. prove equality of the complete documents;
4. run retrieval through a narrow SQLite substitute with UNITARES unavailable.

The repository utility proves the format and substitute path over supplied
records. It does not yet claim production database extraction or a scratch
Postgres restore drill.

## Promotion and stop rules

The pilot can advance only when receipt completeness, retrieval parity, outcome
scoring, and cost capture are all adequate for a confirmatory enrollment.
Synthetic lift proves plumbing, not adoption.

Stop or redesign when:

- source snapshots drift or cell schemas differ;
- identities or model contexts leak across cells;
- expected sources fail preflight;
- audit canary/readback fails;
- missing costs would change the contrast sign;
- measured benefit exists only in injected, oracle-query conditions;
- the substitute matches UNITARES within the frozen uncertainty bound;
- exit restoration or substitute retrieval fails.

Zero retrieval after a verified full funnel may demote the reminder or catalog
lever. It does not justify deleting shared memory. Conversely, high use without
quality and exit proof is dependency, but not earned dependency.

## Explicitly not authorized

- live autonomous alert surfacing, acknowledgements, or Discord fanout;
- EISV/CIRS measurement-to-actuation;
- silent in-memory fallback when shared memory is unavailable;
- async orchestration polling without durable owner-scoped receipts;
- production import/restore MCP tools;
- live KG parity or production-plumbing claims from the offline-fixture canary;
- efficacy, preference, portability, or de-facto-standard claims from the
  plumbing pilot.
