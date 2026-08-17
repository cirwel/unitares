# UNITARES Advanced Bare-Metal Install Playbook (macOS)

> **Tier-1 installs use the release-tagged Docker Compose quickstart in the
> repository README.** This playbook is the advanced source-based path for an
> operator who deliberately wants Homebrew services and a native Python
> process. It is maintained, but it is not the default installation contract.

**Goal:** take a blank macOS install to a state where the governance MCP server is serving on `http://localhost:8767`, an MCP client can perform a check-in successfully, and the dashboard is reachable.

**Audience:** anyone with admin on a Mac and shell familiarity. No prior knowledge of Unitares, EISV, AGE, or the project's history is assumed.

**This doc is grep-verifiable** against `docs/install/cross-machine-surface.md`. If a step claims a value that isn't either (a) the canonical default in source or (b) something the playbook tells you to set, it's a bug — file an issue.

---

## What you'll have when you're done

- Apache AGE + pgvector running on Homebrew PostgreSQL 17, with a `governance` database initialized.
- Redis running locally for production-grade session and identity continuity.
- `python src/mcp_server.py` running on `127.0.0.1:8767`, listening on the MCP transport (`/mcp/`), REST (`/v1/tools/call`), and the dashboard (`/dashboard`).
- A successful round-trip: fresh session → check-in → verdict.
- (Optional) A LaunchAgent so the server restarts at login.

---

## Prerequisites

| Requirement | Why | How to check |
|------------|-----|--------------|
| macOS 12+ (Monterey or newer) | Homebrew formulae for PG 17 + Apple Silicon support | `sw_vers -productVersion` |
| Xcode Command Line Tools | Required to build Apache AGE from source | `xcode-select -p` (should print a path; if not, `xcode-select --install`) |
| Homebrew | Package manager | `brew --version` (if missing, see [brew.sh](https://brew.sh)) |
| ~2 GB free disk | Postgres data dir + Python venv + AGE build | `df -h ~` |
| Network access to GitHub + PyPI + Homebrew | For installs | — |

**Architecture note:** the playbook works on both Apple Silicon (`arm64`) and Intel (`x86_64`). It uses `$(brew --prefix postgresql@17)` everywhere instead of hardcoding `/opt/homebrew/...`, so you don't need to know which machine you're on.

---

## Step 1 — Install Homebrew packages

```bash
brew install postgresql@17 pgvector redis python@3.12 git
brew services start postgresql@17
brew services start redis
```

**Expected:**

```bash
pg_isready -h localhost -p 5432
# /tmp:5432 - accepting connections

redis-cli ping
# PONG
```

**If `pg_isready` says "no response":** wait 5 seconds (Postgres takes a moment to start) and retry. If still failing, `brew services list | grep postgresql` should show `started`. If it says `error`, run `brew services restart postgresql@17` and check `~/Library/Logs/Homebrew/postgresql@17/server.log`.

**If `redis-cli ping` does not print `PONG`:** run
`brew services restart redis` and retry. The server can start without Redis only
in degraded local-only mode; that is acceptable for a demo, not durable
operator continuity.

---

## Step 2 — Build Apache AGE 1.7.0 against PostgreSQL 17

AGE is not in Homebrew. You build it from source against the exact `pg_config` from your Homebrew Postgres.

```bash
export PG_CONFIG="$(brew --prefix postgresql@17)/bin/pg_config"
git clone --depth 1 --branch PG17/v1.7.0-rc0 https://github.com/apache/age.git /tmp/age-build
cd /tmp/age-build
make PG_CONFIG="$PG_CONFIG"
make install PG_CONFIG="$PG_CONFIG"
cd -
```

**Expected:** `make install` ends without errors, and:

```bash
ls "$(brew --prefix postgresql@17)/lib/postgresql/age.dylib"
# /opt/homebrew/.../lib/postgresql/age.dylib
```

**If `make` fails with `bison: command not found`:** `brew install bison flex` and prepend them to PATH: `export PATH="$(brew --prefix bison)/bin:$PATH"`. Re-run `make`.

**If `make install` fails with permission errors:** the Homebrew postgres lib dir is user-writable on a normal install. If yours isn't (rare), check `ls -la "$(brew --prefix postgresql@17)/lib/postgresql/"` — the directory should be owned by your user, not root. Don't `sudo` this; that creates a root-owned file Homebrew can't manage later.

---

## Step 3 — Create the database, install extensions, apply schema

```bash
git clone https://github.com/cirwel/unitares.git
cd unitares

createdb -h localhost -p 5432 governance

export DB_POSTGRES_URL="postgresql://localhost:5432/governance"
export DB_AGE_GRAPH=governance_graph

./scripts/install/bootstrap_postgres.sh --apply
```

**Expected:** each `psql` prints zero errors. Verify:

```bash
psql "$DB_POSTGRES_URL" -c "SELECT extname, extversion FROM pg_extension WHERE extname IN ('age', 'vector');"
#  extname | extversion
# ---------+-----------
#  age     | 1.7.0
#  vector  | <installed version>
```

**On Homebrew Postgres**, the `postgres` superuser doesn't exist by default — your macOS username is the superuser, and the connection above (no user, no password) uses local trust auth. If you see `role "postgres" does not exist`, that's the reason; the URL above already omits the user/password.

---

## Step 4 — Python virtualenv and dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-full.txt
```

**Expected:** all packages install cleanly. The EISV ODE engine (`governance_core/`) lives directly in this repo — no separate install step.

**To skip the ODE entirely** (e.g., a CI runner without numpy build deps, or you only need the behavioral-EISV verdict path):

```bash
export UNITARES_DISABLE_ODE=1
```

Verdicts then come from the behavioral EISV path alone; the dashboard shows a banner indicating reduced diagnostic detail.

---

## Step 5 — Start the server

```bash
python src/mcp_server.py --port 8767
```

**Expected:** within 3 seconds you see log lines ending with something like:

```
Uvicorn running on http://127.0.0.1:8767
```

The server binds to `127.0.0.1` only by default — it is not reachable from your LAN. That's intentional; see the *Optional: expose on LAN* section if you need otherwise.

---

## Step 6 — Verify (the acceptance test)

In a second terminal:

```bash
# Health
curl -s http://127.0.0.1:8767/health/live
# {"status":"alive"}

# Exercise the real REST wrapper and preserve its returned session binding
./scripts/unitares health
./scripts/unitares onboard install-smoke "bare-metal install verification" force
./scripts/unitares update "bare-metal install verified" 0.2 0.9

# Dashboard
open http://127.0.0.1:8767/dashboard
```

**You're done when:** the dashboard loads, the CLI prints a UUID and a verdict,
`redis-cli ping` prints `PONG`, and there are no error log lines from the server
in your first terminal.

---

## Step 7 — (Optional) Connect a Claude Code / Cursor / Claude Desktop client

Follow [`docs/integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md) for the
current client-specific command or configuration. That document is the one
canonical source; this playbook intentionally does not duplicate client JSON.

Once the client connects and calls `start_session`, the server logs record that
fresh process identity. That is the full acceptance: stranger box → working
governance fleet of one.

---

## Optional: install as a LaunchAgent (auto-start at login)

```bash
# 1. Render the template with your paths and a generated secret token
sed -e "s|/PATH/TO/UNITARES|$PWD|g" \
    -e "s|/PATH/TO/PYTHON3|$PWD/.venv/bin/python|g" \
    -e "s|/YOUR/HOME|$HOME|g" \
    -e "s|GENERATE_YOUR_OWN_TOKEN|$(openssl rand -hex 32)|g" \
    -e "s|GENERATE_YOUR_OWN_SECRET|$(openssl rand -hex 32)|g" \
    scripts/ops/com.unitares.governance-mcp.plist \
    > ~/Library/LaunchAgents/com.unitares.governance-mcp.plist

# 2. Load it
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist

# 3. Verify
launchctl list | grep unitares
curl -s http://127.0.0.1:8767/health/live
```

The plist's other tunables (DB DSN, allowed hosts, log paths) are inline at the top of the rendered file. Defaults bind loopback-only and use the trust-auth Postgres connection from Step 3.

---

## Optional: expose on LAN or via a tunnel

The server defaults to `127.0.0.1`. To expose:

```bash
# LAN — bind all interfaces, allow your LAN's Host header
export UNITARES_BIND_ALL_INTERFACES=1
export UNITARES_MCP_ALLOWED_HOSTS="<your-lan-ip>:*,<your-hostname>.local"
export UNITARES_MCP_ALLOWED_ORIGINS="http://<your-lan-ip>:*"
python src/mcp_server.py --port 8767
```

For a Cloudflare tunnel: see `docs/operations/OPERATOR_RUNBOOK.md`. Anything beyond loopback should also have `UNITARES_MCP_BEARER_TOKENS` set; otherwise REST is open.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `pg_isready: no response` | Postgres not started | `brew services restart postgresql@17`, wait, retry |
| `redis-cli ping` does not return `PONG` | Redis not started | `brew services restart redis`, wait, retry |
| `make` fails on AGE with `bison` errors | Apple's old bison shadows Homebrew's | `brew install bison && export PATH="$(brew --prefix bison)/bin:$PATH"` |
| `psql: error: connection to server ... failed: FATAL: role "postgres" does not exist` | Homebrew Postgres uses your username | Export the trust-auth DSN from Step 3; do not add a synthetic `postgres` role just to satisfy a stale default |
| `Address already in use` on port 8767 | Server already running | `lsof -i :8767` — stop the duplicate or use `--port 18767` and set `UNITARES_URL=http://127.0.0.1:18767` |
| `relation "agents" does not exist` on first call | Schema not applied | Re-run `./scripts/install/bootstrap_postgres.sh --apply` from Step 3 |
| `extension "age" is not available` | AGE built against wrong `pg_config` | Verify `$PG_CONFIG` points to your Homebrew PG 17, rebuild |
| Dashboard loads but is empty | Expected — no agents yet | Run the CLI onboarding commands in Step 6 |

For more, see `docs/guides/TROUBLESHOOTING.md`.

---

## What this playbook deliberately does NOT cover

- **Pi / Lumen side (anima-mcp).** That's a separate install and a separate audit; deferred until cross-machine surface for anima-mcp is grep-checked.
- **Multi-host fleet deployments.** This is a single-host install. Coordinated multi-host setups (governance on one Mac, anima on a Pi, dashboard on a third) require additional networking decisions outside the scope of "smooth install."
- **Custom AGE / pgvector versions.** Pinned to AGE 1.7.0 + pgvector latest because that pairing is what the schema was developed against.
- **Production hardening** (TLS, bearer rotation, multi-tenant isolation). Those belong in a separate ops runbook, not in a stranger's first install.

---

## Acceptance test (one-liner)

If you trust the playbook and just want to know it worked:

```bash
redis-cli ping \
  && ./scripts/unitares health \
  && ./scripts/unitares onboard install-smoke "bare-metal smoke" force \
  && ./scripts/unitares update "bare-metal smoke passed" 0.2 0.9
```

If those commands print `PONG`, a UUID, and a verdict, the install is correct.
