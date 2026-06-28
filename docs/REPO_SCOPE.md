# Repo Scope

**unitares is user- and agent-agnostic.** It is a governance MCP server, not an
operator's personal workspace and not a Claude- or Codex-specific project. What
lives here should make sense to a stranger who maintains the repo and runs any
agent (or no agent) against it.

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
  and exempts stored session data under `src/data/`. Working notes belong in
  `~/projects/_notes-archive/<repo>/`; ship clean docs. A PR that legitimately
  discusses these patterns (this guard, a register cleanup, meta-docs) can opt
  the PR-body lint out with the HTML comment `<!-- scope-guard: allow-register -->`.
- **Claude session-attribution links** — `claude.ai/code/session` / a
  `Claude-Session:` trailer. They tie the public repo to a private session,
  signal AI authorship, and are dead links to anyone but the operator (the
  commit-level analogue of `Co-Authored-By`, which this repo also omits). The
  guard flags them only in **committed file content** — it does **not** gate PR
  bodies or commit messages, because the Claude Code harness injects the trailer
  there automatically with no setting to disable it, so a blocking check would
  reject every Claude PR. Omitting the trailer from bodies/commits is a
  best-effort convention, not a CI gate.

## Why a guard, not just this doc

Memory and per-vendor instruction files (`CLAUDE.md`, `AGENTS.md`) do not
reliably prevent leakage — and they only reach the agent that reads them.
`scripts/dev/check-repo-scope.sh` is a vendor-neutral hard gate:

- **pre-commit hook** — fast local feedback (install via
  `scripts/ops/install_git_hooks.sh`).
- **`.github/workflows/repo-scope.yml`** — CI, which catches cloud/web agent
  sessions that bypass local hooks.

If the guard flags something that genuinely belongs here, add the path to
`scripts/dev/repo-scope-allow.txt` (prefer moving the file out instead).
