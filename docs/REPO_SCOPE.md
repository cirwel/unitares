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
  are machine-local state. The guard refuses tracked files under any of them
  and **never** lets `.gitignore` whitelist them back in; `.claude/` is also
  in `.gitignore`. *(PR #1039 committed `.claude/settings.json` via a
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
  `council fold`, `live-verifier`, and, in files only, `three-lane council`).
  It deliberately does **not** flag the real product vocabulary `operator`,
  `council`, or `dialectic`, and does not scan gitignored runtime/session data.
  In files it also flags a cited harness-memory file name (`feedback_*.md`,
  `project_*.md`): that memory is machine-local, so cite the repo artifact or
  the knowledge-graph entry instead. Working notes belong in
  `~/projects/_notes-archive/<repo>/`; ship clean docs. A PR that legitimately
  discusses these patterns (this guard, a register cleanup, meta-docs) can skip
  the PR-body register check with the HTML comment
  `<!-- scope-guard: allow-register -->`; the operator-local path check on the
  PR body always runs.
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
  sessions that bypass local hooks. Beyond changed files it lints one surface a
  file guard cannot see: the **PR body**, for operator-local paths and chat
  register.

If the guard flags something that genuinely belongs here, add the path to
`scripts/dev/repo-scope-allow.txt` (prefer moving the file out instead).

## Session links are not checked

Until 2026-10-14 the guard also refused `claude.ai/code/session` links and the
`Claude-Session:` trailer in files, commit messages and PR bodies, and CI
rewrote PR bodies to strip the harness-appended footer. The operator removed
that rule on 2026-10-14: a session link in a PR body or commit message is
allowed, and nothing rewrites a PR body any more. The commits that carry the
old trailer were never rewritten and need no action.
