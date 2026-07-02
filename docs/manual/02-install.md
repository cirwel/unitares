# 2 · Installation

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)

There are two supported paths. Pick one:

- **Docker** — fastest, one command, brings up Postgres + Redis + the server together. Best for trying it out and for most deployments.
- **Bare-metal** — lower overhead, what the maintainer runs in production. You install PostgreSQL + Apache AGE + pgvector yourself.

**Requirements either way:** Python 3.12+, PostgreSQL with Apache AGE and pgvector. Redis is the de-facto primary session store in production; the server boots without it in a degraded local-only mode (fine for the demo — sessions won't persist across restarts).

## 2.1 Docker (recommended quickstart)

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` drives a synthetic agent through seven check-ins — clean work, then confidence drifting from results, then confusion — and prints the verdict at each step. When it finishes, the server is live: point any MCP client at `http://localhost:8767/mcp/` and open the dashboard at `http://localhost:8767/dashboard`.

### Port conflicts

If `5432` (Postgres), `6379` (Redis), or `8767` (server) is already taken, pick alternate **host** ports — the container-internal ports don't change:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 \
  docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

## 2.2 Bare-metal

The condensed version is below. For a **zero-assumption, step-by-step macOS playbook** with expected output and a troubleshooting table at every step, follow [`../install/PLAYBOOK.md`](../install/PLAYBOOK.md) instead — this section is the summary.

### 2.2.1 Dependencies

You need PostgreSQL 16+ (examples use 17) with **Apache AGE** and **pgvector** compiled and installed, plus Python 3.12+. AGE is not in most package managers — you build it from source against your exact `pg_config`. On macOS/Homebrew:

```bash
brew install postgresql@17 pgvector python@3.12 git
brew services start postgresql@17
pg_isready -h localhost -p 5432           # → accepting connections

# Build Apache AGE 1.7.0 against your PG 17
export PG_CONFIG="$(brew --prefix postgresql@17)/bin/pg_config"
git clone --depth 1 --branch PG17/v1.7.0-rc0 https://github.com/apache/age.git /tmp/age-build
cd /tmp/age-build && make PG_CONFIG="$PG_CONFIG" && make install PG_CONFIG="$PG_CONFIG" && cd -
```

### 2.2.2 Create the database and apply schema

```bash
createdb -h localhost -p 5432 governance
export DB_POSTGRES_URL="postgresql://localhost:5432/governance"
export DB_AGE_GRAPH=governance_graph

psql "$DB_POSTGRES_URL" -f db/postgres/init-extensions.sql
psql "$DB_POSTGRES_URL" -f db/postgres/schema.sql
psql "$DB_POSTGRES_URL" -f db/postgres/partitions.sql
psql "$DB_POSTGRES_URL" -f db/postgres/knowledge_schema.sql
psql "$DB_POSTGRES_URL" -f db/postgres/embeddings_schema.sql
psql "$DB_POSTGRES_URL" -f db/postgres/graph_schema.sql
```

> On Homebrew Postgres the `postgres` superuser doesn't exist — your macOS username is the superuser and the URL above uses local trust auth. If you see `role "postgres" does not exist`, that's why; the DSN omits the user on purpose. DB bring-up detail: [`db/postgres/README.md`](../../db/postgres/README.md).

### 2.2.3 Python environment

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-full.txt
```

`requirements-full.txt` is the default (server, tests, handler dev). `requirements-core.txt` is a 2-package subset (`mcp` + `numpy`) for thin stdio/proxy clients only. The EISV ODE engine (`governance_core/`) lives directly in this repo — no separate install step.

**Signal-only mode (skip the ODE math model):** `export UNITARES_DISABLE_ODE=1`. Verdicts then come from the behavioral-EISV path alone; the dashboard shows a reduced-diagnostic banner. Useful on CI runners without numpy build deps.

## 2.3 Verify the install

```bash
curl -s http://127.0.0.1:8767/health/live          # → {"status":"alive"}

curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"tool":"onboard","arguments":{"purpose":"install verification"}}' \
  | python3 -m json.tool                            # → JSON containing "agent_uuid" + EISV
```

You're done when `/health/live` returns alive, `onboard` returns an `agent_uuid`, the dashboard at `/dashboard` loads (fleet metrics, even if zeroed), and there are no error log lines from the server.

## 2.4 What install does *not* cover here

The bare-metal playbook deliberately excludes the Pi/Lumen side ([anima-mcp](https://github.com/cirwel/anima-mcp)), multi-host fleet deployments, custom AGE/pgvector versions, and production hardening (TLS, bearer rotation, multi-tenant isolation). Production hardening is [chapter 6](06-operating.md) and the [operator runbook](../operations/OPERATOR_RUNBOOK.md).

---

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)
