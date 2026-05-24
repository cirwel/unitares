# Scripts Directory

**Last Updated:** 2026-05-24

> **Note:** Most functionality is available via MCP tools. Scripts are for CLI-only interfaces, operations, and maintenance.
>
> **Status:** specialized local-ops reference. This directory supports running and maintaining a deployment; it is not the primary architecture or product entrypoint for the repo.

---

## `dev/` — Development & CI Scripts

| Script | Description |
|--------|-------------|
| `bump_epoch.py` | Bump governance epoch |
| `check_ci_python_version_sync.py` | Verify CI Python version matches project |
| `doc_audit.sh` | Check all three Unitares repos for stale docs |
| `test-cache.sh` | Tree-hash pytest cache (skips if tests already passed against this exact working tree) |
| `update_docs_tool_count.py` | Update tool counts in documentation |
| `version_manager.py` | Version management utilities |
| `with_checkin.py` | Run a command and emit a best-effort `process_agent_update` check-in with command outcome evidence |

---

## `ops/` — Operational Scripts

The bulk of operational scripts live in `scripts/ops/`.

### Agents & CLI

| Script | Description |
|--------|-------------|
| `mcp_agent.py` | Autonomous MCP agent |
| `operator_agent.py` | Operator-level agent with elevated permissions |
| `enroll_resident.py` | Enroll resident identity anchors |

### Server Lifecycle

| Script / file | Description |
|----------------|-------------|
| `ops/com.unitares.governance-backup.plist.template` | LaunchAgent template: daily DB backup at 03:00 (copy to `~/Library/LaunchAgents/`, adjust paths) |
| `start_unitares.sh` | Start the governance MCP server |
| `stop_unitares.sh` | Stop the governance MCP server |
| `start_server.sh` | Alternative server start |
| `start_with_deps.sh` | Start server with all dependencies |

### Backup

| Script | Description |
|--------|-------------|
| `backup_governance.sh` | Backup governance database (auto-starts container, retries, status JSON, optional macOS alert on failure) |
| `check_governance_backup_health.sh` | Exit non-zero if backups are older than `MAX_AGE_SEC` (default 26h); for cron/monitoring |

### Health & Monitoring

| Script | Description |
|--------|-------------|
| `monitor_health.sh` | Health monitoring loop |
| `health_watchdog.sh` | Process watchdog with auto-restart |

### Database & Connections

| Script | Description |
|--------|-------------|
| `emergency_fix_postgres.sh` | Emergency PostgreSQL fixes |
| `cleanup_stale.sh` | General stale data cleanup |
| `backfill_calibration.py` | Calibration maintenance/backfill helper |

### Git & CI

| Script | Description |
|--------|-------------|
| `install_git_hooks.sh` | Install git hooks |
| `update_changelog.py` | Update changelog from commits |
| `version_manager.py` | Version management (ops copy) |

### Launchd Plists (local only)

LaunchAgent plists under `scripts/ops/*.plist` are intentionally untracked because
they contain machine-specific paths and may contain secrets. Keep local copies in
`scripts/ops/` while developing, and install the active versions to:

`~/Library/LaunchAgents/`

---

## Subdirectories

### `age/`
AGE (Apache Graph Extension) utilities — bootstrap SQL, export scripts, sample Cypher queries.

### `analysis/`
Analysis and reporting scripts, including outcome / calibration reporting, tool counting, EISV PCA analysis, and offline dataset export for validation studies.

### `client/`
Client-side utilities including the Ollama MCP bridge and session/freshness helpers.

### `diagnostics/`
Diagnostic scripts for debugging server and agent issues. Notable operator
checks:

| Script | Description |
|--------|-------------|
| `agent_fragmentation.py` | Read-only report for identities with zero or sparse real check-ins, grouped by model/session/thread so fresh-UUID fragmentation is visible. |

### `migration/`
Database maintenance scripts (embeddings backfill, ghost agent cleanup, knowledge graph maintenance).

### `git-hooks/`
Git hook scripts.

- `pre-commit-combined` is the current default pre-commit hook installed by `scripts/ops/install_git_hooks.sh`
- `pre-commit` is the older script-proliferation-only hook retained for reference

### `safeguards/`
Safety-related scripts and checks.

### `archive/`
Archived scripts organized by type — completed migrations, deprecated CLI tools, one-off session scripts.

---

## Adding New Scripts

1. **Is it operational?** Put it in `ops/`
2. **Is it a dev/CI utility?** Put it in `dev/`
3. **Is it a test?** Put it in `tests/`
4. **Is it one-off?** Plan to archive after use
5. **Document it** in this README
6. **Consider MCP** — Can this be an MCP tool instead?

---

## Service Management

```bash
# Restart governance-mcp
launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist

# Check logs
tail -f data/logs/mcp_server.log
tail -f data/logs/mcp_server_error.log
```
