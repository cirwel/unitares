# Coordination ablation: plumbing pilot plan (v0)

**Status:** DRAFT plan, 2026-10-01. Documentation only. Nothing here is built,
run, enrolled, or authorized by merging this document.
**Protocol:** [`accountable-coordination-ablation-v0`](../../proposals/registered/accountable-coordination-ablation-v0.md)
(registered at #2249, `74bd59cd`). This plan does not amend it.
**Purpose:** decide whether the registered four-arm study is buildable and
affordable before anyone constructs task families or writes an enrollment.

---

## 1. What the protocol allows before enrollment

The protocol's *Feasibility and sample size* section permits, before
enrollment, "an unscored plumbing pilot" that exercises **synthetic
fixtures**. Its purpose is to verify:

- schema validation;
- treatment separation;
- forced discontinuity;
- receipt completeness;
- fail-closed behavior.

It adds: "Synthetic success proves only plumbing." Its *Explicitly not
authorized* list includes "a scored or live-model run".

So this plan has two stages with different standing:

| Stage | Model calls | Standing |
|---|---|---|
| **0. Scripted plumbing** | None. Agents are deterministic scripts | Inside what registration already permits |
| **1. Cost probe** | A live model, on non-confirmatory synthetic families, unscored | **Not permitted by registration.** Needs a separate, explicit operator authorization (Section 6) |

Stage 0 answers "does the harness keep arms separate and fail closed?" Stage
1 answers "what does one family cost per arm?". That figure is the input the
protocol's sample-size justification needs, and nothing in the record supplies
it today.

## 2. What exists and what must be built

Read-only inventory of `master` at `4e4540b82`. No prior UNITARES experiment
ran an agent loop against a repository task, so most of the harness is new.

### Reusable

| Need | Existing piece |
|---|---|
| Content-addressed, create-only receipts | `write_content_addressed_receipt` in `scripts/eval/run_kg_agent_adoption.py` (`O_EXCL` then `link`, modes 0600/0700, refuses a conflicting rewrite); envelope `unitares.content-addressed-receipt.v0` |
| Enrollment artifact pattern | `docs/evaluations/kg-agent-adoption/enrollment-v0.schema.json`; `docs/evaluations/orientation-constraint-set/enrollment-v1.json` (self-digested) |
| Private raw-output directory | `_require_external_private_output` in `scripts/eval/run_orientation_constraint_set.py` (outside the repo, mode 0700) |
| Clean-source preflight | `_assert_clean_named_pushed_head` (same file): clean tree, HEAD equals pushed upstream |
| Deny-external guard and its proof | `_deny_external_operations` and `_probe_deny_all_guard` in `run_kg_agent_adoption.py` |
| Throwaway stack | `docker-compose.yml` (Postgres+AGE, Redis, lease plane, governance) with host ports set by `GOVERNANCE_HOST_PORT` and `LEASE_PLANE_HOST_PORT`; CI precedent `.github/workflows/docker-quickstart.yml` |
| Headless agents without host hooks | Flag sets in `scripts/dev/review_gate.py:1610-1640` and `src/mcp_handlers/support/host_adapter.py:44-97` (Codex `--ignore-user-config --ephemeral --disable hooks --disable plugins`; Claude `-p` with an explicit tool list). `host_adapter.py:398-458` parses token usage |
| Arm C/D capabilities | identity and lineage (`onboard` with `parent_agent_id`); findings (`knowledge` store/search/details); review (`dialectic`); effect receipts (lease plane `POST /v1/effects`); outcome binding (`sync_state` `prediction_id` with `record_result`) |
| Tool-restriction precedent | the fixed six-tool proxy `src/gateway_server.py` + `src/gateway/tools.py` |

### To build

| Component | Why it does not exist |
|---|---|
| **Outer-ledger receipt schema** | The protocol's receipt table (registration, assignment, runtime, resources, isolation, discontinuity, effects, validation, terminal state) has no JSON Schema yet |
| **Episode harness** | Nothing runs N agents against a repository task in worktrees with a scheduled discontinuity and a sealed scorer |
| **Arm B message adapter** | `/v1/msg/*` needs a UNITARES identity proof and persists messages in Postgres until TTL. Arm B would then receive part of arm C's treatment, and messages could survive the discontinuity. Arm B needs an experiment-only, in-memory adapter that reuses only the envelope shape |
| **Per-arm tool filter** | The server has no per-agent allowlist; `use_tool` reaches every tool regardless of advertisement. Arm C must be *unable* to call review, effects, or outcome binding, not merely unprompted. A filtering proxy on the gateway pattern is the enforceable option |
| **Handoff and reconstruction for arms C and D** | No `reconstruct` or `handoff` tool exists. The protocol's arm C promises "reconstruction for handoff". The pilot must define it as a harness step: the successor process is given its predecessor's lineage and runs a fixed set of `knowledge` reads. The pilot should check that this definition is buildable without inventing a new server tool |
| **Synthetic task families and scorers** | Two or three small repositories with planted defects, hidden tests, and at least four variants each. They are **permanently ineligible** for any confirmatory cohort |

## 3. Isolation

The study and both pilot stages must leave production untouched. That is also
why they cannot run against the live server: experiment check-ins and outcome
rows would enter the population the registered 2026-12-01 outcome-grounding
read draws from.

**Known contamination routes, each with a preflight assertion:**

1. **Host hooks in child agents.** The KG adoption canary recorded that a
   background auto-check-in attached state and outcome rows to its identity
   ("durable calibration isolation is falsified"). Every child agent runs with
   plugins, hooks, and user config disabled. Preflight asserts this by
   checking that no request from the episode reaches the production port.
2. **Inherited production settings.** At startup the server loads
   `~/.env.mcp` (`src/mcp_server.py:41-73`), which holds the production
   database URL. Every setting the stack needs is exported explicitly, and
   preflight asserts that the effective database URL is not production. That
   explicit exports win is the expected python-dotenv behavior; it is
   **unverified** and must be tested in Stage 0.
3. **PID and lock files.** `cleanup_existing_server_processes` sends SIGTERM
   to the PID in the PID file. Experiment servers use their own
   `UNITARES_SERVER_PID_FILE`, `UNITARES_SERVER_LOCK_FILE`, and data directory.
4. **Orchestrator defaults.** The orchestrator's ledger database and lease
   plane default to production. Either repoint all of them or spawn agents
   directly from the harness; the plan prefers direct spawning for the pilot.
5. **Worktree reaping.** `scripts/dev/worktree_reaper.py` removes worktrees
   whose PR merged. Experiment worktrees live on branches with no PR, in a
   private directory, never under `/tmp`.
6. **Concurrent stacks.** `docker-compose.yml` fixes every `container_name`,
   so two stacks cannot coexist. Use an override file or run arms
   sequentially.

**A policy conflict needs an operator call (Section 6, D2).**
- `CLAUDE.md` says not to create additional PostgreSQL instances or
  databases.
- The protocol requires separate storage namespaces per arm.
- The documented Docker quickstart is the sanctioned way to stand up a
  separate stack.
- The plan proposes using it, with containers torn down after each run.

## 4. Stage 0: scripted plumbing

Agents are deterministic scripts that issue a fixed sequence of tool calls per
arm. No model is called.

| # | Check | Passing behavior | Injected fault that must fail |
|---|---|---|---|
| P1 | Receipt schema | Every episode writes one receipt that validates and is content-addressed | Drop a required section: the run is marked invalid |
| P2 | Arm separation | A has no messaging; B's messages exist only in memory; C's calls to review, effects, or binding are refused by the filter; D's succeed | A scripted arm-C call to `dialectic` or `record_result` that is *not* refused fails the check |
| P3 | Discontinuity | The scheduled process replacement happens at the declared point; the receipt names predecessor and successor and the reconstruction artifact digest | Arm B: a message sent before the discontinuity is readable after it, so the run fails |
| P4 | Isolation preflight and postflight | Production port, production database URL, and host hooks are absent before the run, and production is untouched after it | Point one variable at production: preflight refuses to start |
| P5 | Fail-closed ledger | A missing ledger sink or a failed receipt write invalidates the episode | Make the ledger directory read-only: no "ungoverned control result" is produced |
| P6 | Scorer sealing | The scorer runs from a digest-pinned copy that agents cannot read | Place the hidden tests inside the agent's worktree: postflight flags scorer leakage |
| P7 | Budget accounting | Calls, tokens (zero here), wall time, and interventions are recorded per arm | Exceeding a ceiling ends the episode with the right terminal state |

**Exit criterion.** Every check passes, and every injected fault is caught.
The output is a plumbing receipt. It is not evidence about coordination.

## 5. Stage 1: cost probe (separately authorized)

Run only after Stage 0 passes and the operator authorizes it.

- **Families:** the same 2 or 3 synthetic families, one variant per arm. They
  stay permanently ineligible for the confirmatory cohort, as the protocol's
  planning-only rule requires.
- **Model:** one frontier model at one reasoning level, identical across
  arms. Local models are not proposed: recorded experience on this fleet is
  that most fail at multi-step tool navigation, so they would measure tool
  failure, not coordination. That is metered, opt-in spending, which the
  execution-cost policy permits but does not require.
- **Hard caps:** a total spend ceiling and a per-episode token and wall-clock
  ceiling, fixed before the first call. Reaching one is a terminal state, not
  a reason to raise it.
- **Recorded per family per arm:**
  - model calls, input and output tokens, wall time;
  - interventions, infrastructure time, and infrastructure failures.
- **Not computed:** any arm comparison of scores. Scorers run to prove the
  path works; their results are recorded as plumbing and never contrasted
  across arms.
- **Output:** a cost table, plus a projection of the confirmatory study's
  cost at the family counts a power procedure might require. The power
  procedure itself remains a separate, planning-only step.

There is no reliable prior figure to plan from. The one timed precedent, the
issue #2168 capture stage, was a hand-run *reconstruction* exercise, not
patching. Its arms were not independent, so its 89-minute and 113-minute arm
times are not a cost basis.

## 6. Operator decisions this plan needs

| # | Decision | Recommendation |
|---|---|---|
| D1 | Build Stage 0 | Yes. It is permitted, model-free, and reusable by the confirmatory study |
| D2 | Isolation substrate | The documented Docker Compose stack on non-production ports, torn down after each run, in place of a second Postgres on the host |
| D3 | Authorize Stage 1 | Decide after Stage 0 passes, with a named model and a spend ceiling |
| D4 | Arm B transport | An experiment-only in-memory adapter, not `/v1/msg` |
| D5 | Arm C restriction | A filtering proxy; client-side tool flags alone are not enforcement |

## 7. What this plan does not do

- It does not enroll a cohort, select confirmatory task families, or freeze
  seeds or sample size.
- It does not compute or report any arm contrast, including from Stage 1.
- It does not touch production data, the registered outcome-grounding read,
  or any EISV instrument. EISV stays telemetry inside the experiment stack and
  never assigns arms, labels outcomes, or decides success.
- It does not add a production switch that turns accountability off. Arm
  differences exist only inside the experiment harness.
