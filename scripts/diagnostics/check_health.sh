#!/bin/bash
# Local operational health check for UNITARES governance.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8767/health}"
DB_POSTGRES_URL="${DB_POSTGRES_URL:-postgresql://postgres:postgres@localhost:5432/governance}"
PID_FILE="data/.mcp_server.pid"
EXIT_CODE=0
HTTP_OK=0

postgres_ready() {
    if command -v pg_isready >/dev/null 2>&1; then
        pg_isready -d "$DB_POSTGRES_URL" >/dev/null 2>&1
        return $?
    fi

    if command -v psql >/dev/null 2>&1; then
        psql "$DB_POSTGRES_URL" -Atqc "SELECT 1" >/dev/null 2>&1
        return $?
    fi

    return 1
}

echo "=== UNITARES Health ==="
echo "Repo: $PROJECT_ROOT"
echo "Endpoint: $HEALTH_URL"

echo ""
echo "=== HTTP Health ==="
if RESPONSE="$(curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null)"; then
    HTTP_OK=1
    python3 - "$RESPONSE" <<'PY'
import json
import sys

d = json.loads(sys.argv[1])
status = d.get("status", "?")
version = d.get("version", "?")
uptime = d.get("uptime", {}).get("formatted", "?")
print(f"✓ HTTP: {status} v{version} uptime={uptime}")
PY
else
    echo "✗ HTTP: Not responding"
    EXIT_CODE=1
fi

echo ""
echo "=== PID File ==="
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$PID" ] && ps -p "$PID" > /dev/null 2>&1; then
        echo "✓ PID file: active process $PID"
    elif [ "$HTTP_OK" -eq 1 ]; then
        echo "⚠ PID file: stale or unreadable ($PID_FILE); HTTP is healthy, so this checkout may not own the service"
    else
        echo "✗ PID file: stale or unreadable ($PID_FILE)"
        EXIT_CODE=1
    fi
else
    if [ "$HTTP_OK" -eq 1 ]; then
        echo "⚠ PID file: missing ($PID_FILE); HTTP is healthy, so this checkout may not own the service"
    else
        echo "✗ PID file: missing ($PID_FILE)"
        EXIT_CODE=1
    fi
fi

echo ""
echo "=== PostgreSQL ==="
if postgres_ready; then
    echo "✓ PostgreSQL: reachable via DB_POSTGRES_URL"

    KG_COUNTS="$(
        psql "$DB_POSTGRES_URL" -Atq <<'SQL' 2>/dev/null
LOAD 'age';
SET search_path = ag_catalog, core, audit, public;
WITH durable AS (
    SELECT count(*)::bigint AS durable_count
    FROM knowledge.discoveries
),
graph AS (
    SELECT count(*)::bigint AS graph_count
    FROM cypher('governance_graph', $$ MATCH (d:Discovery) RETURN d $$) AS (d agtype)
)
SELECT durable_count || '|' || graph_count
FROM durable, graph;
SQL
    )"
    if [ -n "$KG_COUNTS" ]; then
        IFS='|' read -r DURABLE_COUNT GRAPH_COUNT <<<"$KG_COUNTS"
        if [ "$DURABLE_COUNT" = "$GRAPH_COUNT" ]; then
            echo "✓ Knowledge graph: durable and AGE counts match ($DURABLE_COUNT)"
        else
            echo "✗ Knowledge graph drift: durable=$DURABLE_COUNT AGE=$GRAPH_COUNT"
            EXIT_CODE=1
        fi
    else
        echo "✗ Knowledge graph: unable to compare durable and AGE counts"
        EXIT_CODE=1
    fi
else
    echo "✗ PostgreSQL: unreachable at DB_POSTGRES_URL ($DB_POSTGRES_URL)"
    EXIT_CODE=1
fi

echo ""
echo "=== Operator Hint ==="
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "System looks healthy."
else
    echo "If HTTP is down, start with: ./scripts/ops/start_with_deps.sh"
    echo "If PID is stale, stop with: ./scripts/ops/stop_unitares.sh"
    echo "If PostgreSQL is down, start the configured instance behind DB_POSTGRES_URL"
fi

exit "$EXIT_CODE"
