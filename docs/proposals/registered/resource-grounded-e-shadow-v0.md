# Resource-grounded E: shadow study pre-registration (v0)

**Status: registers at the merge of its introducing PR** (issue
[#2410](https://github.com/cirwel/unitares/issues/2410)). Registration fixes the
estimand, arms, metrics, read conditions and decision rule below. It does not
change the governance server, a verdict, a threshold, a weight, a stored field,
or any input of the registered 2026-12-01 outcome-grounding read. Collection is
opt-in per operator machine.

## 1. Question

E is documented as an estimate of operational capacity, but its inputs are
behavioral proxies (decision success, legacy coherence, complexity calibration,
outcome success, and blends of response structure and recent tool success rate;
`src/behavioral_sensor.py`). None of them measures how much capacity an agent has
left, and about a quarter of E's weight is currently dead travel from the pinned
legacy `C(V)` level
([contract, "Dead travel in E and I"](../../ontology/eisv-proprioception-contract.md)).

The study asks one question: **does adding observed resource state to current
EISV predict near-term operational events better than both current EISV alone and
the raw resource signal alone?** If the raw signal does as well, E should not
absorb it.

This is a new measurement process. It is not a re-run of, a supplement to, or an
input to the 2026-12-01 read
([stop rule](eisv-outcome-grounding-stop-rule-v0.md)). The contract lists E's
referent join among the questions that do not wait on that read. It is also
distinct from the [incremental-value ablation](../eisv-incremental-value-ablation-v1.md),
which tests EISV against direct-evidence baselines and adds no new signal.

## 2. Estimand and scope

Prediction of near-term **operational events** in the operator's own
interactive agent sessions. Nothing here measures, or may be described as
measuring, governance quality, misbehavior, safety, or harm. Resource scarcity is
not evidence of epistemic or ethical failure.

Population: sessions of one operator on one host harness (Claude Code). A result
says nothing about other operators, other harnesses, or cross-harness transfer;
those are untested in v0 and are disclosed as such.

## 3. Instrument

The collector is `hooks/resource-observe` plus `scripts/resource_shadow.py` in
the governance plugin, row schema `resource_shadow.v1`. It is default off
(`UNITARES_RESOURCE_SHADOW=1` enables it), local only (no network path; nothing
reaches the governance server), and appends one JSON line per event to
`~/.unitares/resource-shadow/<UTC-date>.jsonl`.

| Event | Written on | Fields beyond `schema, ts, host, event, session_id, agent_uuid` |
|---|---|---|
| `stop` | every turn end | `ctx_tokens_band`, `model` |
| `tool-failure` | every failed tool call | `tool_name` (sanitized), `is_interrupt` |
| `pre-compact` | every context compaction | `compact_trigger` (`auto` / `manual`), `ctx_tokens_band`, `model` |

`ctx_tokens_band` is the lower bound of a fixed band ladder (0, 8k, 16k, 32k, 64k,
128k, 192k, 256k, 384k, 512k, 768k, 1M) over the occupied context of the last
main-chain assistant message present in the transcript when the hook runs.
The host does not guarantee that a turn's final message is written before the
Stop hook fires, so a `stop` row can reflect the previous assistant message of the
same turn; at band resolution this is usually the same band, and the analysis
treats the value as occupancy near the turn end, not at an exact instant.
Occupancy, not headroom, is recorded because the transcript carries token usage
but not the context-window size. Tool input, error text, response text and
working directory are never recorded, and the extract (section 9) keeps only the
fields listed above even if a source row carries others.

The hooks run asynchronously. The host cancels unfinished asynchronous hooks when
a non-interactive (`-p`) session exits, so rows from headless sessions can be
missing. The population is therefore interactive sessions; a session whose `stop`
rows are not contiguous with its tool-failure and compaction rows is not
detectable from the rows alone, and this limitation is disclosed rather than
corrected.

## 4. Unit, outcomes and horizon

**Prediction point.** Each `stop` row with a non-null `ctx_tokens_band` and a
non-null `agent_uuid`.

**Horizon.** The next **K = 5** `stop` rows of the same `session_id`. A point with
fewer than 5 later `stop` rows in its session is dropped (right-censored), and the
dropped count is reported.

**Outcomes**, each scored strictly after the prediction point and before the end
of the horizon:

- **O1, auto-compaction:** a `pre-compact` row with `compact_trigger = auto`.
- **O2, tool failure:** a `tool-failure` row with `is_interrupt = false`.

**Not observable in v1, dropped and disclosed:** forced handoffs, failed
consultations as distinct from tool calls, and escalations that a lower-cost path
would have resolved. Task complexity is also unobserved by the collector, so the
issue's task-complexity baseline is not run.

## 5. Arms

All arms are logistic regressions on the features listed, with no interaction
terms, no regularization search and no feature selection.

| Arm | Features at the prediction point |
|---|---|
| **A0 persistence** | count of the same outcome event in the session's previous 5 turns |
| **A1 elapsed** | turn index within the session; cumulative non-interrupt tool failures so far |
| **A2 raw resource** | `ctx_tokens_band` (log2 of band + 1) |
| **A3 current EISV** | E, I, S, V of the latest persisted state for `agent_uuid` at or before the point |
| **A4 EISV + resource** | A3 features plus the A2 feature |

EISV state is read as E = `state_json->>'E'`, I = `integrity`, S = `entropy`,
V = `volatility`, non-synthetic rows only. A state older than **30 minutes** at the
prediction point counts as missing; points missing A3 features are dropped from
every arm so all arms score the same points, and the drop count is reported.

E already blends recent tool success rate, so A3 carries some past tool-failure
information. That is why A0 exists: an arm credits nothing on O2 unless it beats
persistence.

## 6. Split

Sessions are the grouping unit. Order sessions by their first `stop` timestamp;
the earliest 60% train every arm, and the latest 40% are the held-out test set.
Nothing is tuned on the test set.

## 7. Metrics and inference

**Primary metric:** held-out Brier score, per outcome.

**Primary contrast, per outcome:** `Brier(A4)` against the better of `Brier(A2)`
and `Brier(A3)`, both better-of chosen on the **test** set (so the contrast is
conservative against A4). A4 must also beat A0.

**Inference:** a session-clustered bootstrap (resample test sessions with
replacement, 2,000 replicates, fixed seed 2410) of each Brier difference;
one-sided p-value; Holm correction across the two outcomes.

**Also reported, not decision-bearing:** reliability tables (10 equal-count bins),
false-positive rate at the base-rate threshold, and for every mean the n and the
median. No ranking statistic (AUC or similar) is reported without its
bootstrap p-value.

## 8. Read conditions

The analysis runs **once**, on the first frozen extract (section 9) whose held-out
set holds at least **40 sessions** and, for an outcome to be read, at least **30
events** of that outcome. An outcome that falls short is `INCONCLUSIVE`. It is not
negative.

Checking these counts may count events. It must not compute any association
between a predictor and an outcome before the single read.

## 9. Extraction and data handling

- Analysis runs only on **frozen extracts** written by
  `scripts/analysis/resource_shadow_extract.py`: shadow rows plus the joined EISV
  states, each with a SHA-256, in a manifest that also records this document's
  SHA-256. The live database is never re-queried for the read.
- `core.agent_state` keeps 90 days, so an extract is taken at least every **30
  days** while collection runs. Extracts are cumulative; the read uses the latest
  one that meets section 8.
- Extracts stay on the operator's machine, outside any repository. Only
  aggregate results are published.

## 10. Decision rule

- **PASS** (for an outcome): A4 beats both the better of A2/A3 and A0 with
  Holm-adjusted p ≤ 0.05. PASS licenses a separate, versioned proposal to change
  E's semantics, which goes through its own adversarial design review. PASS
  changes nothing by itself.
- **THRESHOLDS SUFFICE:** A2 is at least as good as A4 on held-out Brier. The
  finding is that raw resource thresholds can be used by policy directly and E
  should not absorb them.
- **INCONCLUSIVE:** anything else, including an unmet read condition.

## 11. Guardrails

- Low budget must never lower I, and must never on its own suggest a riskier agent.
- V keeps its current definition.
- No result from this study may be joined into, or cited as, evidence for or
  against the 2026-12-01 read.
- No biological or other analogy is claimed as validated by any outcome here.

## 12. Amendments

Before the first extract, amendments are logged below with a date and reason.
After it, the only permitted changes are disclosed deviations, logged below; the
estimand, arms, metrics and decision rule are frozen.

| Date | Change | Reason |
|---|---|---|
| (none) | | |
