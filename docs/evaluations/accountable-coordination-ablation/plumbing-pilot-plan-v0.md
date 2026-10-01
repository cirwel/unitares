# Coordination ablation: plumbing pilot plan (v0)

**Status:** DRAFT, 2026-10-01, revised the same day after adversarial review
(Section 8). Documentation only. Nothing here is built, run, enrolled, or
authorized by merging this document.
**Protocol:** [`accountable-coordination-ablation-v0`](../../proposals/registered/accountable-coordination-ablation-v0.md)
(registered at #2249, `74bd59cd`). This plan does not amend it.
**Recommendation:** do **not** build the pilot yet. Review showed that the
registered treatments cannot be faithfully run on this host as specified, and
that the confirmatory study is probably beyond a solo operator. Section 5
lists what has to be decided first, in order.

---

## 1. What the protocol allows before enrollment

Before enrollment the protocol permits "an unscored plumbing pilot" on
**synthetic fixtures** to verify schema validation, treatment separation,
forced discontinuity, receipt completeness and fail-closed behavior. It adds
that "synthetic success proves only plumbing". It also lists "a scored or
live-model run" as not authorized. The first draft of this plan proposed two
stages:

- **Stage 0:** scripted, model-free plumbing checks.
- **Stage 1:** an unscored live-model cost probe.

Review found that Stage 1 is a protocol deviation as written. Its legitimate
route is the protocol's amendment log, not an out-of-band authorization.
Scoring arm outputs during a probe also lets a solo operator see per-arm
results before freezing prompts and ceilings. The scorer path should be proven
only with known-good and known-bad reference patches.

## 2. Why the pilot cannot be built as first drafted

### 2.1 Isolation requires OS-level containment

The first draft relied on environment variables, port checks and
host-hook flags. A live agent with a shell, which a patching task needs, can
bypass all of them on this host:

- **Production Postgres trusts every local connection.**
  `/opt/homebrew/var/postgresql@17/pg_hba.conf` is `trust` for local sockets,
  127.0.0.1 and ::1. `psql -h localhost governance` reaches production
  whatever `DB_POSTGRES_URL` says.
- **`gh` is authenticated for the host user**, so an agent can publish
  externally.
- **Backend routes beyond MCP are reachable with curl.**
  - The governance REST endpoint is `POST /v1/tools/call`
    (`src/http_api.py:290-291`).
  - The lease plane exposes `/v1/effects` and `/v1/msg/*`
    (`elixir/lease_plane/lib/unitares_lease_plane/http_router.ex:161`,
    `:658-761`).

  A tool-filtering proxy covers none of these.
- **Same-user file access crosses every boundary.** An agent can read other
  arms' worktrees, which hold sibling variants of the same family. It can also
  read the scorer copy and earlier episodes' raw output. A 0700 directory owned
  by the same user is not a boundary.
- **Host agent configuration leaks into every arm.**
  - `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` describe UNITARES and the
    knowledge graph, so they would prime arms A and B.
  - Claude.ai connectors, one of which reaches production UNITARES through a
    tunnel, load unless MCP configuration is strict.
  - `scripts/dev/review_gate.py:1632-1640` runs `claude -p` with an allowed
    tool list only; nothing there disables hooks, plugins or user instructions.
  - `host_adapter.py:84-88` uses `--safe-mode --tools ""`, which leaves no
    tools at all.
  - The Claude configuration that keeps an explicit MCP server while dropping
    hooks, memory and user instructions reads auth only from
    `ANTHROPIC_API_KEY`. An isolated Claude child is therefore a metered-API
    child. That is a cost-policy decision; subscription auth is not available
    on that path.
- **Inherited credentials.** `load_server_env` (`src/mcp_server.py:41-75`)
  calls `load_dotenv` without `override`. Exported values win, but every key
  not exported still loads, including `GITHUB_TOKEN`. Children also inherit the
  harness's own environment.
- **Agent spawning defaults to production.**
  - The orchestrator defaults its lease plane to `127.0.0.1:8788`
    (`elixir/agent_orchestrator/config/config.exs:7`).
  - Child hooks default to `localhost:8767` (`agent_runner.ex:83`).
  - Dialectic dispatch defaults to the host orchestrator
    (`src/mcp_handlers/dialectic/orchestrator_dispatch.py:56`).

**What would actually work:** each arm's agents run inside a container. The
container's network reaches only that arm's experiment stack and the model
endpoint. It holds no host credentials and no host agent configuration. It
mounts only that agent's worktree, with no access to scorer, ledger or other
arms' files. The deny-external guard
(`scripts/eval/run_kg_agent_adoption.py:466-484`) is an in-process offline
guard and cannot constrain separate agent CLIs.

### 2.2 The treatments do not survive operationalization

- **Arm D's extra treatment is inert or confounded in a sealed stack.** That
  pushes D−C toward zero by construction and the primary D−B toward C−B.
  - **Review needs a reviewer.**
    - Auto-selection is off by default (`src/mcp_handlers/dialectic/reviewer.py:79-96`).
    - The default synthetic reviewer is a local model invoked on thesis
      submission (`dialectic/handlers.py:2538`, `:2993`).
    - The compose stack configures no review model.
    - With no reviewer, D's reviews never complete. With one, it is a second
      model whose tokens are spent server-side, which breaks the protocol's
      matched-model and common-ceiling rules (protocol lines 53-63).
  - **Effect receipts gate nothing.** `record_only` "observes without
    acquiring" (`http_router.ex:157-158`), and editing files in one's own
    worktree is not a governed effect.
  - **Outcome binding cannot change the outcome it binds.** The real outcome is
    a sealed scorer agents cannot see.
- **Arm C still contains policy intervention.** Storing a finding passes
  `check_agent_can_operate` (`src/mcp_handlers/knowledge/handlers.py:1807`),
  which refuses paused identities (`agent_auth.py:178`). The friendly tool
  envelopes also return `next_action`, `risk_summary` and `recovery_hint`.
  The protocol withholds policy intervention from C.
- **Reconstruction defined by the harness is not the product.** The first draft
  had the harness feed a successor its predecessor's lineage and a fixed set of
  `knowledge` reads. That is the harness writing a handoff note, not an agent
  using UNITARES. Whether reconstruction is agent-invoked (the product) or
  harness-invoked (a different treatment) changes what C and D test.
- **Arm B is weaker than "ordinary" coordination.** Ordinary harnesses
  summarize on compaction, resume sessions and pass notes. The protocol's B
  withholds all structured handoff. A positive D−B could therefore come from
  having *any* handoff.
- **The worktree is an unwithheld durable channel in every arm.** Agents in A
  and B can leave notes, commits, stashes or branches for their successor, and
  live models do so routinely. Scripted agents never do, so a scripted
  discontinuity check passes by construction. Agent topology (shared or
  separate worktrees within an arm) is also undefined.

### 2.3 Blinding with one person

The protocol requires evaluators and validator maintainers to stay blind until
scores, missingness and receipt-completeness are frozen (protocol lines
189-195). The enrollment must name a blinded evaluator. A solo operator
configures arms, maintains validators, judges invalidity and owns
publication. One invalidity criterion, "missing cost data capable of changing
a contrast", cannot be judged without knowing the contrast. That leaves three
routes:

- every rule written as code frozen at enrollment, with arm labels coded and a
  zero intervention budget;
- a second person;
- declaring the integrity conditions unattainable.

### 2.4 Feasibility

Planning arithmetic, not a power analysis. For a paired comparison at
α = 0.05 two-sided and 80% power, families n ≈ 7.84·σd²/δ².

**The target effect δ is not chosen here.** It is the smallest D−B difference
worth detecting, and choosing it is an operator decision (D1). The figures
below use δ = 15 percentage points purely for illustration. n scales with
1/δ²: δ = 10 points needs 2.25 times as many families, and δ = 20 points about
0.56 times as many.

- **Binary per-family success.** With σd² of 0.3 to 0.4 and δ = 15 points, n
  is about 105 to 140 families.
- **Fractional scores.** With σd near 0.3 and δ = 15 points, n is about 31.
- **Construction load.** Each family needs at least four difficulty-matched,
  unfamiliar, sealed variants. That is roughly 124 to 560 hand-built variants
  plus four multi-agent episodes per family.
- **Difficulty matching.** It must happen without confirmatory scores, which
  pushes it toward isomorphic variants. Any cross-arm leak then becomes a
  direct answer key.
- **A probe would not fix the cost basis.** Two or three small synthetic
  families give 8 to 12 heavy-tailed, censored, likely low-cost points. That is
  not a planning basis for the confirmatory study.
- **The protocol's own penalty.** An underpowered run returns "unsupported",
  which the protocol requires publishing. That would read as evidence of no
  value when it is evidence of nothing.

## 3. Reusable pieces (corrected)

| Need | Existing piece |
|---|---|
| Create-only content-addressed receipts | `write_content_addressed_receipt`, `scripts/eval/run_kg_agent_adoption.py:1029-1055` (`O_EXCL` temp file, hard-link create, modes 0600/0700); envelope `unitares.content-addressed-receipt.v0`. A writer convention, not protection against another process of the same user |
| Enrollment pattern | `docs/evaluations/kg-agent-adoption/enrollment-v0.schema.json`; `docs/evaluations/orientation-constraint-set/enrollment-v1.json` |
| Private output and clean-source preflight | `_require_external_private_output` and `_assert_clean_named_pushed_head`, `scripts/eval/run_orientation_constraint_set.py:104-168` |
| Experiment stack | `docker-compose.yml`, self-contained at the data layer. Caveats: fixed `container_name`s; named volumes survive unless `down -v`; default host ports equal production's; the lease plane image is pulled, not built from the frozen source, unless `--build`; it is the empty-install product |
| Codex isolation flags | `codex exec --ignore-user-config --ephemeral --disable hooks --disable plugins` (`scripts/dev/review_gate.py:1622-1623`). Whether `--ignore-user-config` also skips the global `AGENTS.md` is unverified, and exec JSONL does not report the model used (`host_adapter.py:452-461`) |

## 4. To build, if the study proceeds

- an outer-ledger receipt schema covering the protocol's receipt table;
- a per-arm container sandbox meeting Section 2.1;
- an episode harness with a frozen discontinuity boundary and frozen
  successor inputs;
- an experiment-only arm-B transport. `/v1/msg` needs identity proofs and
  persists messages, so it cannot be used;
- a per-arm capability boundary enforced at the network and credential level,
  not by tool name;
- synthetic families with history-free snapshots, permanently ineligible for
  confirmatory use;
- **Stage 0 checks redesigned so they cannot pass by construction:**
  - allowed and forbidden capability probes for every arm, run from inside each
    arm's real sandbox and launch path, with model inference stubbed;
  - a predecessor-generated canary that B must fail to recover and C/D must
    recover through declared reads;
  - an empty-store canary after each teardown;
  - replay and conflicting-reuse probes for bound outcomes;
  - scorer proofs with reference patches only.

## 5. Decisions needed, in order

| # | Decision | Recommendation |
|---|---|---|
| D1 | **Feasibility gate first.** Fix, before building anything, the family count, construction hours and spend above which the study is infeasible as registered | Set it now. First freeze the target effect δ as an explicit operator choice, then apply the Section 2.4 arithmetic with the variance stated as an assumption. The 31 to 140 range holds only at δ = 15 points |
| D2 | **Amend the protocol (v0.1) or not.** Points an amendment would settle: (a) agent-invoked reconstruction; (b) arm B including ordinary note handoff; (c) arm D's review by an in-arm peer on the matched model, charged to the common budget; (d) whether arms A and C are kept, since they roughly double cost while the primary contrast is D−B; (e) the route for a live-model probe | Amend before any build. Without (a) to (c), D−B does not test the product claim |
| D3 | **Blinding route** | Code-frozen adjudication with coded arm labels, or a second person. If neither, say in the enrollment that the integrity conditions cannot be met |
| D4 | **Containment substrate** | Per-arm containers with an outbound allowlist. A Linux host would also allow OpenShell-style kernel enforcement |
| D5 | **Model access for isolated agents** | An isolated Claude child is metered-API only. Decide between a metered budget and a Codex-only harness, whose model identity is not reported on the exec path |
| D6 | **Arm C's policy boundary** | Decide whether a setting that exists only inside the sealed experiment stack may disable pause gating and governance envelopes for C. The protocol forbids any production or agent-selectable switch |
| D7 | **Exception to "no additional PostgreSQL"** | Per-arm, per-family stacks mean many Postgres instances. Record the waiver in the enrollment. If granted, add the same scoped rule to both `CLAUDE.md` and `AGENTS.md`: the prohibition sits in their shared contract, which `scripts/dev/check-shared-contract.sh` keeps byte-identical |

## 6. Side finding: local database trust

Production Postgres trusts every local connection (Section 2.1). That matters
beyond this study. Any process running as any local user can read and write
the governance record, which is the same weakness the forecast-mirror review
found for the operator token. It is not changed here. Tightening it is an
operator decision with deployment consequences, since residents and tools
connect without passwords today.

## 7. What this plan does not do

- It does not enroll a cohort, select families, freeze seeds or sample size,
  or compute any arm contrast.
- It does not touch production data, the registered outcome-grounding read,
  or any EISV instrument.
- It does not add a production switch that turns accountability off.

## 8. Review record

The first draft (`5e80fe2e7`) received three independent read-only reviews on
2026-10-01.

| Pass | Result |
|---|---|
| Adversarial design review | 19 findings (7 P1): containment bypasses including the trust-auth database; the worktree as an unwithheld channel; arm D inert or confounded; harness reconstruction not the product; arm B weaker than ordinary coordination; Stage 1 as a protocol deviation; blinding; feasibility arithmetic |
| Code-claim verification | About 25 citations confirmed. Corrected: the headless flag sets, the scope of the deny-external guard, the orchestrator defaults, the proxy's bypassability, the compose details |
| Codex | FINDINGS(10), 3 P1: environment-only isolation, volumes persisting across sequential arms, proxy bypass. P2: scorer and ledger boundaries, scripted checks passing by construction, policy intervention inside arm C, an unmatched reviewer model, effect and binding semantics, the discontinuity canary, blinding |

All findings were accepted; none was rebutted. The first draft's two-stage
build plan is superseded by Sections 2, 4 and 5.
