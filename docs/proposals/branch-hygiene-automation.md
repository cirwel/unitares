# Branch Hygiene Automation

**Status:** PARTIAL — Phase 1 dry-run branch sweep shipped via PR #197 (Vigil hygiene). Subsequent phases remain proposal.
**Author:** Claude (b47c38ac)
**Reviewers:** dialectic-knowledge-architect, feature-dev:code-reviewer

## Problem

Multi-agent fleet (Claude/Codex/Cursor/dispatch/Watcher) creates branches at edit-session cadence. Each agent: creates worktree → commits → ships PR → exits. PR merges hours/days later, after the agent is gone. Nothing closes the loop.

Manual triage on 2026-04-26: 90 orphan origin branches + 24 worktrees, almost all from agents that shipped successfully but never cleaned up.

Existing infrastructure with adjacent role: Vigil (launchd cron, 30min, janitorial — health checks, KG groundskeeping, test runs). No branch hygiene step.

## Design

**Two surfaces, sequenced.** Ship the cron sweep first; defer the webhook until empirically necessary.

### Surface 1 (Phase 1 + 2): Vigil branch-hygiene job

**Form:** new launchd job `com.unitares.vigil-hygiene.plist`, weekly at Sunday 03:00. NOT a step inside Vigil's `run_cycle` — `CYCLE_TIMEOUT=120s` cannot accommodate `git cherry` over ~100 branches plus a `git fetch --prune`. Separate plist, separate `StandardOutPath`.

**Code:** `agents/vigil-hygiene/agent.py` (new sibling to `agents/vigil/`). Single entrypoint, single sweep, exits.

**Steps (each gated on previous success):**

1. `git -C <repo> fetch --prune origin` — drop stale tracking refs
2. **Local `[gone]` cleanup:** for each local branch with `[gone]` upstream:
   - If associated worktree exists, check it is clean (see "clean check" below) → if clean, `git worktree remove`; if dirty, log + skip
   - `git branch -D <branch>`
3. **Origin orphan sweep:** for each origin branch with no open PR:
   - Skip if matches keepalive rule (see below)
   - Run `git cherry master origin/<branch>`
   - If output is **empty** (zero commits) → SKIP, log warning (ambiguous; could be empty branch or fetch failure)
   - If every line starts with `-` (patch-id matches a master commit, i.e. squash-merged) → delete origin branch
   - If any `+` line → SKIP, append to "holds" report
4. **Stale-worktree sweep:** for each path under `.worktrees/`:
   - If branch is missing or `[gone]`, and worktree is clean → `git worktree remove`

**"Clean worktree" check (load-bearing — code-reviewer caught this):**
```
clean = (git -C <path> status --porcelain output is empty)
       AND NOT exists(<path>/.git/rebase-merge/head-name)
       AND NOT exists(<path>/.git/CHERRY_PICK_HEAD)
       AND NOT exists(<path>/.git/MERGE_HEAD)
       AND NOT exists(<path>/.git/BISECT_LOG)
```

**Keepalive rules (never delete):**
- `master`, `main`
- Branch matching any open PR head ref (queried via `gh pr list --json headRefName`)
- Branch with last commit newer than 24h
- Branch is `feat/branch-hygiene-automation` (ourselves, during rollout)

**Reporting (intentionally minimal):**
- Structured log line per sweep: `{branches_pruned, worktrees_removed, origin_orphans_deleted, holds_count, holds: [...], duration_s}`
- One Prometheus-style counter exposed via existing `/metrics` if it exists (add later if not). NO new dashboard panel. NO KG `hygiene_sweep` discovery emission. (Both removed per dialectic's scope-creep flag.)

**Identity attribution (load-bearing — dialectic caught this):**
Action attributed to **governance-mcp's existing resident identity**, NOT a new "vigil-hygiene-cleanup" label. Per identity.md doctrine ("agent's first MCP call is sole identity source"; label-is-not-identity). The job is a substrate-anchored cron actor; if a separate identity is wanted, it must be earned via the existing resident-identity path (plist + UUID + behavioral trajectory), not invented in-process. For Phase 1, attribute to `vigil@unitares` (it's a Vigil-family job) with event field `triggered_by: scheduled_hygiene_sweep`.

**Phase 1 (PR 1): dry-run mode.** Default `DRY_RUN=true`. All steps EXCEPT actual deletions execute; deletions are logged as "would delete X". Run for one week, verify outputs match manual triage expectations on a fresh sample.

**Phase 2 (PR 2): live mode.** Flip `DRY_RUN=false` after Phase 1 verifies. No other changes.

### Surface 2 (Phase 3, conditional): GitHub webhook

**Decision gate:** only build if Phase 1+2 demonstrate that 7-day latency is actually a problem. Otherwise skip — see §"Why webhook is conditional" below.

If built:

**Endpoint:** new subdomain `webhooks.cirwel.org` (NOT `gov.cirwel.org`). Per dialectic: HMAC-shared-secret and bearer-token authn models shouldn't share a hostname; future webhooks (Discord, CI) land cleanly on a dedicated surface.

**Wiring:**
- Cloudflare tunnel: `webhooks.cirwel.org/v1/github` → governance-mcp port 8767 (or a dedicated webhook process if isolation matters more)
- GitHub repo webhook on `CIRWEL/unitares`: event = `pull_request`, content type JSON, secret = `GITHUB_WEBHOOK_SECRET` from `~/.config/cirwel/secrets.env`

**Handler (Starlette, async):**
1. `Content-Length` check; reject 413 if > 1 MB
2. `raw = await request.body()` — **must be raw bytes, NOT `await request.json()`** (GitHub doesn't canonicalize JSON)
3. Verify `X-Hub-Signature-256` HMAC against `raw` + secret. Reject 401 on mismatch
4. `payload = json.loads(raw)`
5. Verify `payload["repository"]["full_name"] == "CIRWEL/unitares"` (defense against valid HMAC from different repo)
6. If `payload["action"] != "closed"` OR `payload["pull_request"]["merged"] != true` → return 200, no action
7. Extract `branch = payload["pull_request"]["head"]["ref"]`
8. **Defensive path check:** before any deletion, resolve worktree path and verify it's under `<repo>/.worktrees/`; if not, refuse and log
9. **All git operations via `loop.run_in_executor(None, subprocess.run, ...)`** — direct `await asyncio.create_subprocess_exec()` will deadlock per anyio-asyncio gotcha (`CLAUDE.md`, `src/http_api.py:142-199`)
10. Same clean-check as Vigil sweep (incl. rebase/cherry-pick sentinels)
11. Emit governance event via `post_finding` (sync, executor-safe path used by other agents) — NOT a direct DB await

**Identity:** same rule as Vigil — attribute to governance-mcp's resident identity, `triggered_by: github_webhook`.

## Why webhook is conditional

The dialectic review's strongest counter:

> The proposal is over-architecting a problem that has a 10-line solution at the `ship.sh` level... The webhook surface buys you maybe a 4-hour latency improvement over a daily Vigil run, on a problem that took 90 branches and presumably weeks to become noticeable.

Honest assessment: weekly Vigil sweep delivers ~7-day worst-case latency. Webhook delivers ~minutes. The difference matters only if 90→0 once isn't sustainable and we'd accumulate a meaningful backlog between Sunday runs. **Decide after one month of Phase 2 data.** If holds-count stays < 5 and Sunday sweep cleanly drains the queue, webhook is unjustified.

Phase 3 stays in this proposal as a contingent design, not a commitment.

## Code locations

```
agents/vigil-hygiene/agent.py             (new) — Phase 1+2
agents/vigil-hygiene/cherry.py            (new) — git cherry parsing, pure function
agents/vigil-hygiene/clean_check.py       (new) — worktree-clean predicate, pure function
launchd/com.unitares.vigil-hygiene.plist  (new) — Phase 1
config/secrets.env.example                (mod) — add GITHUB_WEBHOOK_SECRET (Phase 3)
docs/install/cloudflare-tunnel.md         (mod) — webhooks.cirwel.org route (Phase 3)
src/mcp_handlers/github_webhook.py        (new) — Phase 3 only
src/mcp_server.py                         (mod) — register webhook route (Phase 3 only)
tests/test_vigil_hygiene_cherry.py        (new) — Phase 1
tests/test_vigil_hygiene_clean_check.py   (new) — Phase 1
tests/test_vigil_hygiene_sweep.py         (new) — Phase 1, integration
tests/test_github_webhook.py              (new) — Phase 3
```

## Test cases (explicit, per code-reviewer's gap analysis)

**Phase 1 — `vigil-hygiene`:**
- `git cherry` empty output → branch SKIPPED, not deleted (regression guard for the "newly-created empty branch" false-positive)
- Force-pushed branch with new `+` commits, no open PR → SKIPPED, appears in holds
- Branch newer than 24h → SKIPPED regardless of cherry result
- Worktree paused mid-rebase (`.git/rebase-merge/head-name` exists) → NOT removed
- Worktree paused mid-cherry-pick (`.git/CHERRY_PICK_HEAD` exists) → NOT removed
- `git worktree remove` returns non-zero → handled as distinct failure path, not silent
- `_should_run_hygiene()` (or launchd cadence) — only Phase-1 if we go gate-route; if separate plist, this is launchd's job
- Branch matching open-PR head ref → SKIPPED
- Worktree path traversal in payload (Phase 3 only) → REFUSED
- DRY_RUN=true → log "would delete X", verify nothing deleted

**Phase 3 — webhook:**
- HMAC computed over raw body, NOT re-serialized JSON (regression guard)
- HMAC mismatch → 401, no action
- `repository.full_name` mismatch → 200, no action, log
- `action: closed, merged: false` (PR closed without merge) → 200, no action
- `action: closed, merged: true` → cleanup runs
- Payload > 1 MB → 413
- Concurrent webhook + Vigil sweep → both idempotent, no double-delete error

## Risks (revised)

| Risk | Mitigation |
|------|-----------|
| anyio-asyncio deadlock in webhook | All git/DB calls via `run_in_executor`; events via `post_finding` sync path |
| `git cherry` false-negative on rewritten squash | Phase 1 dry-run validates against manual triage before live |
| Force-pushed branches accumulate as permanent holds | Holds-count is reported; if drift > 10 over 30 days, escalate (out of scope for this proposal) |
| HMAC secret leak | Secret rotation procedure in `docs/install/cloudflare-tunnel.md` (Phase 3) |
| Race between webhook and Vigil | Idempotent — `git worktree remove` on absent path returns non-zero, handled |
| Identity erosion (synthetic actor labels) | Attribute to existing resident identity per identity.md, never invent in-process labels |

## Non-goals (explicit)

- Auto-merging PRs
- Stale-PR closing (separate concern, separate proposal)
- Cleanup of branches with genuine unmerged work — always HOLD
- Cross-repo support (extend later if needed)
- Cross-machine cleanup (Pi worktrees handled by Pi-side hygiene job, not by Mac webhook — per dialectic)
- Salvage: opening draft PRs for HOLD branches — separate intent, separate failure modes (per dialectic)
- KG `hygiene_sweep` discovery emission (no EISV grounding for the channel — would be surface sprawl)
- Dashboard panel (separate feature, separate review surface)
- Tunable per-branch keepalive (24h global rule is fine until empirically not)

## Open question — single, unresolved

Should the holds-count escalation path (force-pushed orphans drifting up) be addressed in a Phase 4, or accepted as operator-attention burden? Defer answer until Phase 2 data exists.
