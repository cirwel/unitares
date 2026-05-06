# Cross-Machine Install Surface

A grep-derived inventory of every value in this repo that varies between machines, plus an explicit list of values that **intentionally** vary and must not be unified.

**Audit date:** 2026-04-24 against `chore/install-audit` branch.
**Scope:** unitares server only. Pi-side anima-mcp deferred to v2.
**Acceptance:** the install playbook (`docs/install/PLAYBOOK.md`) must not assume any value classified MUST-FIX below.
**Resolutions:** 10 of 11 MUST-FIX entries and all 4 plist-template gaps addressed in `chore/install-audit-fixes` (PR pending). Regressions now blocked by `tests/test_install_surface.py`. One entry (`health_watchdog.sh`) deferred — see end of MUST-FIX section.

---

## How to use this doc

Every entry has a **disposition**:

- **OK** — already env-var overridable with a sensible default; safe for a stranger's box.
- **MUST-FIX** — operator-specific value shipping as a default; will mislead or break a fresh install.
- **TEMPLATE** — file is a template that needs `__VAR__` substitution at install time.
- **INTENTIONAL** — varies by design; do not unify (see *Intentional Heterogeneity* below).

When adding new files that touch any of the patterns in *Audit Patterns*, re-check this doc and add an entry.

---

## MUST-FIX (operator-specific defaults)

These values bake one operator's environment into code that ships to others. Each must change before a stranger install will work.

| Status | File | Line | Value | Fix |
|--------|------|------|-------|-----|
| ✅ resolved | `scripts/ops/start_unitares.sh` | 14 | `UNITARES_MCP_ALLOWED_HOSTS` default = `192.168.1.151:*,192.168.1.164:*,100.96.201.46:*,gov.cirwel.org` | Default to empty (loopback-only); comment block documents opt-in env var |
| ✅ resolved | `scripts/ops/start_unitares.sh` | 15 | `UNITARES_MCP_ALLOWED_ORIGINS` default = same LAN/Tailscale/`gov.cirwel.org` set | Default empty |
| ✅ resolved | `scripts/ops/start_unitares.sh` | 37 | Prints `https://gov.cirwel.org/v1/tools` example | Generic `https://your-host.example/v1/tools` |
| ✅ resolved | `scripts/ops/start_unitares.sh` | 121 | Prints `Tunnel: https://gov.cirwel.org/mcp/` unconditionally | Conditional print guarded by `CLOUDFLARE_TUNNEL_HOSTNAME` |
| ✅ resolved | `scripts/ops/start_server.sh` | 60 | Same `gov.cirwel.org` example string | Generic example |
| ⏸ deferred | `scripts/ops/health_watchdog.sh` | 28 | Hardcoded Pi Tailscale IP `100.79.215.83` | See *deferred rationale* below |
| ✅ resolved | `requirements-core.txt` | 22 | Comment example uses `https://gov.cirwel.org/v1/tools` | Generic example |
| ✅ resolved | `scripts/ops/com.unitares.ipv6-loopback-proxy.plist.template` | 33 | Hardcoded `/Users/cirwel/projects/unitares/scripts/ops/ipv6_loopback_proxy.py` | `__UNITARES_ROOT__` + `__PYTHON3__` placeholders; install header shows `sed` substitution |

### Deferred: `health_watchdog.sh:28`

Applying the fix as written (`${ANIMA_HEALTH_URL:-}`, skip if unset) silently stops Kenny's running anima/Lumen monitoring because `com.unitares.health-watchdog.plist` does not currently set `ANIMA_HEALTH_URL` — it relied on the hardcoded default. Three paths forward:

1. Operator first adds `ANIMA_HEALTH_URL` env var to the live plist, then the script default is removed in a follow-up PR.
2. Ship a gitignored `<workspace>/scripts/ops/operator.env.local` (proposed; not yet created) that the script sources if present; operator maintains their Pi IP there.
3. Keep the current hardcoded default AND add an `ANIMA_HEALTH_URL` override, with an explicit comment that stranger installs can ignore the (unreachable) default — it just logs a timeout.

Option 2 is cleanest for cross-machine portability. Not applied in this PR because it requires an operator-side change (creating the `.local` file) that coincides with the script change.

---

## TEMPLATE GAP (gitignored plists with no in-repo template)

`.gitignore` excludes `scripts/ops/*.plist` because installed copies contain secrets. Only three templates are tracked: `governance-mcp.plist` (sanitized), `chronicler.plist.template`, `ipv6-loopback-proxy.plist.template`. The four below exist on the operator's disk but **a stranger has nothing to copy from**.

| LaunchAgent | Status | Notes |
|------------|--------|-------|
| `com.unitares.governance-mcp.plist` | Tracked, sanitized with `/PATH/TO/UNITARES`, `GENERATE_YOUR_OWN_TOKEN` | Rename to `.template` for naming consistency (low priority) |
| `com.unitares.chronicler.plist.template` | Tracked template using `__UNITARES_ROOT__`, `__HOME__` | OK — reference pattern |
| `com.unitares.ipv6-loopback-proxy.plist.template` | ✅ Resolved in this PR | `__UNITARES_ROOT__` + `__PYTHON3__` placeholders applied |
| `com.unitares.sentinel.plist.template` | ✅ Added in this PR | Sanitized template created |
| `com.unitares.sentinel-beam.plist.template` | ✅ Added in Wave 1 cutover PR | Sanitized BEAM Sentinel cutover target |
| `com.unitares.vigil.plist.template` | ✅ Added in this PR | Sanitized template created |
| `com.unitares.gateway-mcp.plist.template` | ✅ Added in this PR | Sanitized template created |
| `com.unitares.governance-backup.plist.template` | ✅ Added in this PR | Sanitized template created |

`tests/test_install_surface.py::test_plist_template_uses_placeholders` now enforces that any `<string>` in a `*.plist.template` contains no hardcoded `/Users/` path and that each template declares at least one `__UNITARES_ROOT__` or `__HOME__` placeholder.

---

## ARCHITECTURE GAPS (block stranger install entirely)

Beyond per-line edits, two structural issues will prevent a fresh install:

1. ~~**`unitares-core` is a private compiled wheel.**~~ ✅ **Resolved 2026-04-24.** `governance_core/` is now folded into this repo at top level (pure Python). The `unitares-core` repo is archived at v2.3.0 (LICENSE, README, tag preserved as historical reference) but no longer participates in the install path. CI no longer requires `UNITARES_CORE_TOKEN`; fork PRs run CI freely. Rationale: the paper (v6, Zenodo DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)) had already disclosed the four-component drift decomposition, so the IP-protection rationale for a separate compiled wheel had dissolved. Folding back removed two-repo coordination overhead with no IP cost.
2. **Apple Silicon assumed in scripts.** `/opt/homebrew/opt/postgresql@17/bin` is hardcoded in:
   - `scripts/ops/emergency_fix_postgres.sh:6`
   - `scripts/ops/backup_governance.sh:10`
   - `scripts/ops/start_with_deps.sh:12`
   - `db/postgres/README.md:30` (instructional, but no Intel alternative shown)

   On an Intel Mac the prefix is `/usr/local/opt/postgresql@17/bin`. **Fix:** replace each with `PG_BIN="$(brew --prefix postgresql@17)/bin"`. The `chronicler.plist.template:48` PATH already covers both prefixes — pattern to follow.
3. **Apache AGE has no Homebrew formula.** `db/postgres/README.md` walks through `git clone apache/age && make && make install` against `pg_config`. Real failure modes: Xcode CLT missing; `bison`/`flex` versions mismatch; AGE tag not pinned to a known-good against PG 17. The install playbook needs to pin AGE 1.7.0 explicitly and surface build failures with a link to the AGE issue tracker.

---

## OK (already env-var overridable, defaults are correct)

These appear in the audit but need no change. Listed so future audits don't re-flag them.

| Pattern | Where it's centralized | Why OK |
|---------|----------------------|--------|
| Bind address (`127.0.0.1` / `0.0.0.0`) | `src/mcp_listen_config.py` | Single source; `UNITARES_BIND_ALL_INTERFACES` and `UNITARES_MCP_HOST` env vars override |
| Governance port `8767` | `src/mcp_server.py:531` (`DEFAULT_PORT`) | `--port` CLI arg + `SERVER_PORT` env var override; this is the canonical port |
| MCP / REST / WS / health URLs in agents | `agents/common/config.py` | All env-var-fallback defaults to `http://localhost:8767` |
| DB connection string | `os.environ.get("DB_POSTGRES_URL", "...")` everywhere | Env var wins; default DSN works on a fresh Homebrew Postgres because Homebrew uses trust auth on localhost (the literal `postgres:postgres` password is illustrative — Homebrew ignores it) |
| Tailscale CGNAT range `100.64.0.0/10` | `src/http_api.py:147` | This is the entire Tailscale network spec, not a specific operator's IP — correct as a constant |
| LAN / private network ranges `192.168.0.0/16`, `10.0.0.0/8` | `src/http_api.py:148-149` | RFC 1918 ranges, machine-independent |
| `~/Library/LaunchAgents` install path | All plist install instructions | Standard macOS path, identical across machines |
| `~/.unitares/anchors`, `~/backups/governance` | `scripts/ops/rotate-secrets.sh`, `backup_governance.sh` | Use `${HOME}` correctly |
| `$HOME` substitution in chronicler template | `scripts/ops/com.unitares.chronicler.plist.template` | Pattern to follow for the other plist templates |

---

## INTENTIONAL HETEROGENEITY (do not unify)

**This section exists so a future grep-audit doesn't "fix" things that are intentional. Memory anchor: `MEMORY.md` *Ports & Endpoints — DO NOT NORMALIZE*.**

| Value | Where it appears | Why heterogeneous |
|-------|------------------|------------------|
| Port `8767` | Governance MCP (Mac) — `src/mcp_server.py`, dashboards, all governance clients | Canonical governance port |
| Port `8766` | Anima MCP (Pi) — `scripts/ops/health_watchdog.sh`, `config/claude-desktop-mcp-config.json`, plus skill docs | Canonical anima/Lumen port; lives on a different host |
| Port `8768` | Gateway MCP (Mac) — `src/gateway/constants.py`, `src/gateway_server.py` | Reduced-surface proxy (6 tools vs 76) for weak external clients; same host as 8767 but **different process** |
| `claude-ai_UNITARES` and `unitares-governance` MCP names | MCP client configs across plugin repos | Stable IDs that external clients persist; renaming churns user state |

**The pattern that will trip a future agent:** they'll see `8767` everywhere in governance code and `8766` in one watchdog line, "fix" the watchdog to `8767`, and silently break the anima health check. Reference the `DEFINITIVE_PORTS.md` table before changing any port literal.

---

## Audit Patterns (reproduce this audit)

Run these from the repo root. Excludes `.git`, `.worktrees`, `data/`, `papers/`, `__pycache__`, and `tests/` (test fixtures legitimately use any of these strings).

```bash
# Operator path
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' -g '!tests/**' '/Users/cirwel'

# Operator's home LAN / Tailscale IPs
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' -g '!tests/**' '\b100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b|\b192\.168\.1\.[0-9]{1,3}\b'

# Operator's domain
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' 'cirwel\.org|lumen\.local|\.ts\.net'

# Apple Silicon / Intel brew prefix
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' '/opt/homebrew|/usr/local/opt/postgres'

# Bind addresses + ports (read alongside DEFINITIVE_PORTS.md)
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' -g '!tests/**' '\b(8766|8767|8768|5432)\b'
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' -g '!tests/**' '\b127\.0\.0\.1\b|\b0\.0\.0\.0\b'

# DB credentials literal
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' 'postgres:postgres@localhost'

# Plist install path
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' 'Library/LaunchAgents|launchctl'

# Personal identifiers
rg -n --hidden -g '!.git' -g '!.worktrees' -g '!data/' -g '!papers/**' -g '!**/__pycache__/**' -g '!tests/**' 'hikewa|@gmail|kenny'
```

A drift in the **MUST-FIX** count between this audit and a future re-run is a regression — either fix the new entry or add it here with justification.
