# Scripts Directory

**Last Updated:** 2026-05-27

> **Note:** Most functionality is available via MCP tools. Scripts are for CLI-only interfaces, operations, and maintenance.
>
> **Status:** specialized local-ops reference. This directory supports running and maintaining a deployment; it is not the primary architecture or product entrypoint for the repo.

---

## `dev/` — Development & CI Scripts

| Script | Description |
|--------|-------------|
| `autopilot_closeout.py` | Policy-bounded workflow wrapper for Watcher diagnostics, optional test-cache, closeout, branch hygiene, and explicit ship.sh plan/delivery |
| `bump_epoch.py` | Bump governance epoch |
| `check-wave3-prereq-data-window.sh` | Wave 3 §14 data-window gate over `audit.coordination_measurements` |
| `check_ci_python_version_sync.py` | Verify CI Python version matches project |
| `doc_audit.sh` | Check all three Unitares repos for stale docs |
| `file_lease.py` | Claim BEAM lease-plane `file://` surfaces before code edits; `guard --changed` wraps commands, `hold --changed` refreshes and heartbeats during agent work |
| `parse_update_phase_logs.py` | Parse `[checkin_phases]` and `[enrichment_phases]` timing lines from MCP logs |
| `process_update_loadgen.py` | Drive concurrent `process_agent_update` load against the local governance MCP server |
| `test-cache.sh` | Tree-hash pytest cache (skips if tests already passed against this exact working tree) |
| `update_docs_tool_count.py` | Update tool counts in documentation |
| `version_manager.py` | Version management utilities |
| `with_checkin.py` | Run a command and emit a best-effort `process_agent_update` check-in with command outcome evidence |

### BEAM File Lease Helper

`scripts/dev/file_lease.py` is the local bridge from code-editing sessions to the Elixir lease plane. It claims canonical `file://` surfaces for paths in this worktree.

Common patterns:

```bash
# Hold current and future changed paths while an agent edits.
python3 scripts/dev/file_lease.py hold --changed

# Guard a command with the current changed paths.
python3 scripts/dev/file_lease.py guard --changed -- ./scripts/dev/test-cache.sh

# Check or explicitly claim one file.
python3 scripts/dev/file_lease.py status src/foo.py
python3 scripts/dev/file_lease.py acquire src/foo.py --enforce
```

`hold --changed` refreshes on every heartbeat and releases on interrupt. `guard --changed` snapshots the current changed paths, blocks on contention, runs the command, then releases.

---

## `ops/` — Operational Scripts

The bulk of operational scripts live in `scripts/ops/`.

### Agents & CLI

| Script | Description |
|--------|-------------|
| `mcp_agent.py` | Autonomous MCP agent |
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
Analysis and reporting scripts, including outcome / calibration reporting, tool
counting, EISV PCA analysis, and offline dataset export for validation studies.
Notable validation reports:

| Script | Description |
|--------|-------------|
| `eisv_skeptic_report.py` | Falsifiable report comparing EISV/prior-state predictive lift against simple outcome baselines. |

### `client/`
Client-side utilities including the Ollama MCP bridge and session/freshness helpers.

### `diagnostics/`
Diagnostic scripts for debugging server and agent issues. Notable operator
checks:

| Script | Description |
|--------|-------------|
| `agent_fragmentation.py` | Read-only report for identities with zero or sparse real check-ins, grouped by model/session/thread so fresh-UUID fragmentation is visible. |

### `migration/`
Database maintenance scripts (corpus re-embedding, ghost agent cleanup, knowledge graph maintenance).

### `git-hooks/`
Git hook scripts.

- `pre-commit-combined` is the current default pre-commit hook installed by `scripts/ops/install_git_hooks.sh`
- `pre-commit` is the older script-proliferation-only hook retained for reference

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
