# Repo Scope

**unitares is user- and agent-agnostic.** It is the MCP-native governance Core, not an
operator's personal workspace and not a Claude- or Codex-specific project. What
lives here should make sense to a stranger who maintains the repo and runs any
agent (or no agent) against it. Agent reasoning loops and user-facing harnesses
belong outside Core and integrate through its public MCP, REST, and SDK
contracts.

## Out of scope — do not commit here

- **Career / personal artifacts** — resumes, cover letters, job-application
  materials, portfolio demos built for a specific employer. These live in
  `~/career/` (see the operator's workfolder convention), not in the product
  repo. *(This is why the FRT sandbagging demo was removed from `demos/`.)*
- **Per-vendor agent/tool config** — `.claude/`, `.codex/`, `.cursor/`, etc.
  are machine-local state. They stay in `.gitignore` and are **never**
  whitelisted back in. *(PR #1039 committed `.claude/settings.json` via a
  `!.claude/settings.json` exception — that was reverted.)*
- **Per-vendor attribution conventions** — settings that strip or rewrite commit
  / PR attribution for one agent vendor belong in the operator's local
  `~/.claude` (or the adapter **plugin** repo, which is the canonical
  vendor-specific surface), not in this agnostic repo.
- **Personal contact info** — personal emails / identifiers. Use the project's
  public contact surfaces only.
- **Chat-session / AI-deliberation register** — committed docs, code comments,
  and PR descriptions should read as product engineering, not as a continued
  personal chat session or an AI-review scratchpad. The guard flags, in changed
  files and in PR bodies: operator-local paths (`/Users/cirwel`), second-person
  address of the operator (`per your guidance`, `your overlay`, `you flagged`,
  `questions for Kenny`), and exposed AI-review process (`council pass`,
  `council fold`, `live-verifier`, `three-lane council`). It deliberately does
  **not** flag the real product vocabulary `operator`, `council`, or `dialectic`,
  and does not scan gitignored runtime/session data. Working notes belong in
  `~/projects/_notes-archive/<repo>/`; ship clean docs. A PR that legitimately
  discusses these patterns (this guard, a register cleanup, meta-docs) can opt
  the PR-body lint out with the HTML comment `<!-- scope-guard: allow-register -->`.
- **Session-attribution links** — `claude.ai/code/session` URLs and the
  `Claude-Session:` trailer. The Claude Code harness appends these to commit
  messages and PR bodies by default, but they tie the public repo to a private
  session, signal AI authorship, and are dead links to anyone but the operator
  (the commit-level analogue of `Co-Authored-By`, which this repo also omits).
  The guard checks three places, in three separate steps: changed files, the PR
  body, and **the PR's commit messages** — a trailer stripped from one still
  fails on another. Fixing the PR body needs a fresh `pull_request` event
  (close/reopen or push); a rerun replays the body GitHub snapshotted at trigger
  time. `<!-- scope-guard: allow-register -->` exempts the register check only —
  this check and the operator-local-path check always run, on every PR.

## Metered model-cloud dependencies

The execution-cost policy (`CLAUDE.md` → *Execution-cost policy*) keeps the repo
**user-agnostic**: the core must run free / self-hosted, so a metered model API
is never *required* on the default path (an installer without a paid key — a solo
dev, not just a funded company — can always run it). Metered models are welcome
as an **opt-in, off-by-default backend**; what's forbidden is *forcing* a paid
API on every installer. The guard makes that line executable, flagging only the
"forces it on everyone" signals in changed files: `anthropics/claude-code-action`
in a `.github/workflows/` file, an `import`/`from anthropic` SDK import (no local
fallback), and a **hardcoded** `api.openai.com` / `api.anthropic.com` endpoint. It
deliberately does **not** flag the free/opt-in paths — a config-driven `base_url`
(env override) passes, the `openai` client is allowed (it also drives a **local
Ollama** server), and the orchestrator may spawn the `claude` CLI by design. A
deliberate opt-in metered backend can register in `repo-scope-allow.txt`.

## Why a guard, not just this doc

Memory and per-vendor instruction files (`CLAUDE.md`, `AGENTS.md`) do not
reliably prevent leakage — and they only reach the agent that reads them.
`scripts/dev/check-repo-scope.sh` is a vendor-neutral hard gate:

- **pre-commit hook** — fast local feedback (install via
  `scripts/ops/install_git_hooks.sh`).
- **`.github/workflows/repo-scope.yml`** — CI, which catches cloud/web agent
  sessions that bypass local hooks. Beyond changed files it lints two surfaces a
  file guard cannot see: the **PR body** and **commit messages in `base..HEAD`**.

If the guard flags something that genuinely belongs here, add the path to
`scripts/dev/repo-scope-allow.txt` (prefer moving the file out instead).

## Session-attribution footers are removed, not tolerated

Agent harnesses append `_Generated by [Claude Code](…/session_…)_` to a pull
request body *after* the body is submitted. The author cannot strip it before
opening the PR, so the session-link check used to fail on the first run of every
agent-opened PR, with a hand-edit of the description as the only remedy.

CI now removes that footer and rewrites the body before linting
(`scripts/dev/pr_body_attribution.py`). The distinction is deliberate: the link
must not sit in the public body, so the fix is to delete it, **not** to make the
check ignore it. Ignoring it would leave the leak in place and retire the alarm.

The strip is narrow, and `tests/test_pr_body_attribution.py` pins both halves:

| Body contains | Outcome |
|---|---|
| Only a trailing harness footer | Footer removed, body rewritten, guard passes |
| A session link written into the description | Survives the strip — **guard fails** |
| Both | Footer removed, remaining link still **fails** |

A fork PR gets a read-only token, so the rewrite cannot happen there and the
guard fails as it did before; edit the description by hand.
