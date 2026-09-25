#!/bin/bash
# Cleanup stale processes and resources from governance-mcp
# Run: ./scripts/cleanup_stale.sh [--dry-run]
# Override project root via UNITARES_ROOT env var; otherwise auto-derived.

PROJECT_DIR="${UNITARES_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

DRY_RUN=false
if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN - no processes will be killed ==="
fi

echo "=== Governance MCP Cleanup ==="
echo ""

# 1. Find stale MCP server processes
echo "Checking for stale MCP server processes..."
STALE_PIDS=$(pgrep -f "mcp_server" 2>/dev/null)
if [[ -n "$STALE_PIDS" ]]; then
    echo "Found MCP server processes:"
    ps -p $(echo $STALE_PIDS | tr '\n' ',') -o pid,etime,command 2>/dev/null | head -10
    if [[ "$DRY_RUN" == false ]]; then
        echo "Killing..."
        echo $STALE_PIDS | xargs kill 2>/dev/null
        echo "Done."
    fi
else
    echo "No stale MCP server processes found."
fi
echo ""

# 2. Check for stale lock files
# Never delete lock files by age: a stuck holder's file can be old, and
# unlinking it lets a newcomer lock a fresh file at the same path, so two
# writers get in. lock_cleanup removes only files no process holds.
echo "Checking for stale lock files..."
LOCK_DIR="$PROJECT_DIR/data/locks"
if [[ -d "$LOCK_DIR" ]]; then
    LOCK_ARGS=(--lock-dir "$LOCK_DIR" --max-age 1800)
    if [[ "$DRY_RUN" == true ]]; then
        LOCK_ARGS+=(--dry-run)
    fi
    (cd "$PROJECT_DIR" && python3 -m src.lock_cleanup "${LOCK_ARGS[@]}")
else
    echo "Lock directory not found (OK if not using file locks)."
fi
echo ""

# 3. Check for orphaned heartbeat files
echo "Checking heartbeat freshness..."
HEARTBEAT_FILE="$PROJECT_DIR/data/mcp_heartbeat.json"
if [[ -f "$HEARTBEAT_FILE" ]]; then
    AGE_MINUTES=$(( ($(date +%s) - $(stat -f %m "$HEARTBEAT_FILE")) / 60 ))
    if [[ $AGE_MINUTES -gt 10 ]]; then
        echo "Heartbeat is stale ($AGE_MINUTES min old)"
        if [[ "$DRY_RUN" == false ]]; then
            rm -f "$HEARTBEAT_FILE"
            echo "Removed stale heartbeat file."
        fi
    else
        echo "Heartbeat is fresh ($AGE_MINUTES min old)."
    fi
else
    echo "No heartbeat file (server not running)."
fi
echo ""

# 4. Summary
echo "=== Cleanup complete ==="
if [[ "$DRY_RUN" == true ]]; then
    echo "This was a dry run. Run without --dry-run to actually clean up."
fi
