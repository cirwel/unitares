# Frontier brief, 2026-09-28 to 2026-10-05: source check and repo mapping

**Status:** note only. No code, schema, protocol, README or positioning change.
Nothing here is built, enrolled, or authorized by merging this document.
**Precedent:** [`frontier-brief-2026-09-24.md`](frontier-brief-2026-09-24.md).

An externally supplied weekly brief proposed building a
`SystemConfigurationManifest` and running a repeated coordination ablation. This
note checks its claims against primary sources where they could be opened, maps
each idea onto what the repository already records, and says what is new.

## TL;DR

- Most of the brief's "best experiments" are already designed, and the
  coordination ablation was **stopped on feasibility** on 2026-10-01
  ([pilot plan §5.1](../evaluations/accountable-coordination-ablation/plumbing-pilot-plan-v0.md)).
  It is not "overdue"; it needs a new premise (a second operator, or a task
  source large enough for the fixed sample in the plan's power table).
- A configuration manifest is mostly a **join over fields the write context
  already carries**, plus a short list of real gaps (below). The first step is
  to measure how populated those fields are live, not to add a schema.
- One brief claim was overstated against its source (LiteTrajEval). One
  detail could not be confirmed (AutoCompact's trajectory count and base model).

## Source check

Opened 2026-10-05 via the arXiv abstract pages only; full texts not read.

| Brief claim | Source says | Verdict |
|---|---|---|
| Agents Are Systems, Not Models: ~54% of outcome variance from rerunning the same configuration; >18,000 trajectories; four scientific tasks | "approximately 54% of the outcome variance coming from repeating the same configuration rather than changing it"; "more than 18,000 agent trajectories" across "four scientific tasks" | Confirmed (abstract). Still one unreplicated preprint |
| Prompting "verify" barely changes behavior; a verification tool changes it substantially | Same sentence in the abstract | Confirmed. The brief's remark that the runner is less released than the trajectories was not checked; the abstract says the benchmark is released |
| AutoCompact: +9.2 and +5.0 points on SWE-bench Verified / SWE-PolyBench Verified; gains hold with a 256K window that never overflows | "9.2% and 5.0%"; "improvements hold across all evaluated inference budgets, with a 256K context window that never overflows" | Partly confirmed. The abstract says percent, not points. **1,052 trajectories and Qwen3-Coder-30B not seen on the abstract page** |
| LiteTrajEval: 20-35 point failure-localization gain, ~6x cheaper, >8x faster than AgentRx; enterprise deployment | "roughly 20-35 percentage points on Magentic-One and up to 23 percentage points on tau-retail"; "about 6x" cost, "more than 8x" time; "deployed in our enterprise agentic platform" | **Overstated in the brief.** 20-35 is Magentic-One only; tau-retail is up to 23. Deployment is an unsupported authors' claim |
| OpenAI withheld GPT-6.1 Astra over scope/authorization and truthful reporting | Not opened (press links only) | **Unchecked.** The brief itself notes no evals, thresholds or system card were published, so this shows a reported veto, not how it was decided |

## Mapping to the repository

### 1. Astra veto: deployability as a multi-invariant verdict

The proposed split (authorized scope, action completeness, truthful reporting,
monitorability, unresolved evidence) corresponds to pieces that exist
separately: effect receipts, outcome binding (`record_result`), and the
[outcome-binding assurance case](../evaluations/assurance-cases/outcome-binding-v0.md).
There is no campaign-level deployability verdict and this note does not propose
one; a verdict that blocks release is an operator-owned policy decision.

The proposed experiment (hidden authorization differences, independently scored
attempted vs permitted vs actual vs claimed) needs a real agent with a shell. It
inherits every containment problem in pilot plan §2.1 and is blocked on the
same substrate decision (D4).

### 2. System score, not model score: the manifest idea

`s22.write_context.v1` (`src/provenance_context.py`) already records, per
write: `harness_id`/`type`/`version`, `model`, `model_provider`, `model_source`,
`transport`, `tool_surface`, `memory_context`, `governance_mode`,
`comparison_key`, `task_type`, `episode_id`, and `episode_fork_kind`. The layer
taxonomy in [`harness-substrate-plurality.md`](harness-substrate-plurality.md)
already says harness, model and tool surface are "context, not collapsed into
identity".

Brief's manifest fields vs that record:

| Field | In S22 today |
|---|---|
| model, harness, tools, memory | Yes (self-declared; see Limits) |
| task information regime (curated vs full role-visible state) | No. This is the variable the 9-24 brief §3 already flagged for the ablation |
| permissions | Partly: `affordance_state`, not a permission set |
| budget / retries / verifier present | No |
| compaction policy | No (only the fork kind) |

Limits that apply to any manifest built from S22: the fields are
agent-declared, so a manifest is a claim about configuration, not evidence of it
unless a harness-side source corroborates it.

The 54% rerun-variance finding is an independent reason to distrust single-run
contrasts. It does not transfer a number to UNITARES tasks. Whether it widens
the paired-difference variance assumed in pilot plan §2.4 is a hypothesis for
whoever reopens that study, not a conclusion.

**Proposed first step (not done):** measure, from the live store, how often each
S22 field above is populated, split by harness, before proposing any new field
or digest. An empty field would read as absent, not as "no configuration".

### 3. AutoCompact: compaction as a governed state transition

`classify_episode_fork` (`src/thread_identity.py`) already infers a
`compaction` fork kind, separate from declared lineage. It is a heuristic
(a Claude Code client with existing thread nodes), not a declaration, so it
says a compaction probably happened, not what state survived it. The brief's rule that a summary
must never expand permissions or erase contrary evidence is a design
constraint on any future handoff format and is consistent with the current
"declared lineage only" posture. The replay experiment (full history vs summary
vs typed state, with a stale hypothesis and a fake-compaction injection) is an
agent-run study with the same containment needs as item 1. No change proposed.

### 4. LiteTrajEval: budgeted projections over immutable trajectories

Whether every raw event store here is immutable was not checked. A diagnostic projection
that records its rule profile, omitted-event count, judge version and budget
would be a new derived artifact. Its value is unmeasured here; the brief's
suggested comparison against full human review is a reasonable first test but
needs a labeled set that does not yet exist in this repository.

## Not done here, deliberately

- No schema change to `s22.write_context.v1`; it is a hot shared surface.
- No amendment to `accountable-coordination-ablation-v0`, and no reopening of
  the 2026-10-01 feasibility stop.
- No README or `PRODUCT_DEFINITION.md` edit. The brief's "evidence, state and
  authority layer" wording is held with the 9-25 positioning disposition until
  the assurance case has been assessed by someone other than its authoring
  session.
- No live measurement and no read of the full papers.

## Watch

OpenAI publishing the Astra evaluation package; independent replication of the
rerun-variance result; AutoCompact code and cross-model results;
adversarial tests of learned compaction against instruction propagation.
