# KG Agent-Adoption Pilot v0

**Status:** DRAFT / offline fixture independently reviewed / live-plumbing
canary HOLD / no scored run authorized

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
live-model/network/KG/audit/production-database operations. The receipt remains
immutable evidence of the pre-promotion state: content digest
`b009eeddcc732be5cd463cef913c2cee218463ed3fb81d055db02d714201f9c2`
binds enrollment digest
`872e28cc35f8d7f280a7b4523b506a171cc8f622775b864e77b8965f90992387`
and review HOLD session `8e76d528d0baa5ba`. Independent follow-up session
`aeb10fba0bb89400` approved only `offline_fixture_validation`. The
checked-in enrollment therefore changes four review metadata fields to
`offline_fixture_reviewed` while the original receipt and digest stay
unchanged. Any material fixture change requires another review.

### Live production-plumbing canary

Governed review session `d13aa3554f8dbccb` authorized one bounded canary:
read a pinned root and run the six frozen FTS/OR/top-five queries through a
fresh `canary_agent_adoption*` identity, prove its telemetry is excluded from
adoption and calibration, append one neutral
`infrastructure.audit_write_read_canary.v1` row, and preserve HOLD.

The source map, captured production-plugin probe, and receipt are:

- `docs/evaluations/kg-agent-adoption/live-source-map-v0.json`;
- `docs/evaluations/kg-agent-adoption/live-mcp-probe-v0.json`;
- `docs/evaluations/kg-agent-adoption/live-plumbing-canary-v0.receipt.json`.

Receipt content digest
`fc4507b8af3b98a20b9ab3b795c7bbdac9144991efa1ded743c372d26e7e1608`
records adverse results instead of tuning around them:

- all four logical sources are `derived_projection` mappings to one live
  discovery with `byte_equivalent=false`;
- that discovery was absent from the first five results for five queries and
  ranked fifth for the sixth;
- two details reads and six searches were attributed to
  `canary_agent_adoption_de0df826`, while measured-state, outcome, and
  `agent_adoption.*` event counts for that identity were zero at immediate
  postflight;
- exactly one audit row was appended, event
  `5458a6e8-5288-4e2e-bce3-f0f2e4e4095a`, with null agent/session and both
  counting flags false;
- immediate client validation failed because PostgreSQL returned encoded JSON.
  No retry or replacement row was written. A read-only recovery exactly
  validated the existing row, so the receipt distinguishes persistence and
  recovery from a passing immediate write/read canary;
- the documented full MCP endpoint on port 8767 was unavailable. The live
  Codex UNITARES plugin transport produced the captured probe. This transport
  drift remains a deployment-integrity failure, not evidence of adoption.

Before the bound probe, canary identity
`1951df9d-cd64-4d49-a9b4-66935f3dee79` also completed two details reads and
six searches, then aborted before the audit append when the same encoded
`audit.tool_usage.payload` shape reached an object-only validator. Two later
HTTP endpoint attempts failed before onboarding and produced no KG or audit
operation. These attempts remain excluded by the canary label and are not
silently folded into the bound probe's operation counts.

That immediate zero was not durable. At `2026-08-28T04:48:39Z`, the slot's
background `auto_checkin` attributed 50 runtime-window tool calls to the
canary and wrote one measured state plus one low-trust
`agent_reported_tool_result` completion outcome. The original receipt remains
immutable evidence of its point-in-time read; this later observation falsifies
durable calibration isolation. Future canaries must run in a genuinely separate
slot/process or explicitly restore and verify the parent binding before a
quiet-period read. The runner now labels its zero as point-in-time and never as
durable exclusion.

Therefore `logical_source_parity=false`,
`audit_recording_path_proven=false`, `behavioral_evidence=false`, and
`scored_run_authorized=false`. The frozen corpus, queries, tags, ranking
parameters, and K must not be changed in response to these results.

Follow-up runner `scripts/eval/run_kg_agent_adoption_live_canary_v1.py` closes
the two canary-harness defects without rewriting that historical receipt. A
fresh probe must originate from a standalone process through the direct
loopback full-MCP endpoint on port 8767; captured Codex-plugin evidence is
accepted only for read-only recovery and can never authorize a fresh audit
append. Exact audit readback now goes through the application audit-query API,
which filters by event UUID and normalizes JSON before validation instead of
bypassing that seam with raw SQL. This is hardened plumbing, not a new result:
no v1 live receipt exists yet, and durable calibration exclusion still requires
an isolated quiet-period read.

An operator-authorized Claude host review (execution
`ex-513ede34-bbcf-4813-a470-3e975586e119`) independently agreed with HOLD and
prioritized orchestration control-plane durability, transport integrity,
first-read JSON fidelity, stronger source provenance, and preserving the
retrieval misses as adverse evidence. It proposed future identity-based audit
exclusion rather than relying on count flags; that is a prospective contract
question because this governed run explicitly required null agent/session.

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

PR #1939 added execution IDs and a structural terminal envelope, but did not
close owner-scoped result authorization, lost-ack spawn replay, restart-durable
results, cancellation terminal receipts, or the Python timeout/reconciliation
loop. Orchestration therefore remains manually supervised, advisory, and
outside this adoption canary.

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
