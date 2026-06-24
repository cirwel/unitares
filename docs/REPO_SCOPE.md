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
