#!/usr/bin/env bash
# backup_governance.sh — Daily PostgreSQL backup for UNITARES governance DB
# Scheduled via ~/Library/LaunchAgents/com.unitares.governance-backup.plist
#
# Dumps the governance database from native Homebrew PostgreSQL@17,
# compresses with gzip, and retains the last N backup files.

set -euo pipefail

PG_BIN="/opt/homebrew/opt/postgresql@17/bin"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/governance}"
DATABASE="${DATABASE:-governance}"
PG_USER="${PG_USER:-postgres}"
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
# Keep the last N backup files (not calendar days; filename list order).
KEEP_DAYS="${KEEP_DAYS:-14}"
LOG="${LOG:-$BACKUP_DIR/backup.log}"

# Wait for Postgres (handles slow restarts).
PG_READY_ATTEMPTS="${PG_READY_ATTEMPTS:-30}"
PG_READY_SLEEP_SEC="${PG_READY_SLEEP_SEC:-2}"

# pg_dump transient failure retries.
DUMP_RETRIES="${DUMP_RETRIES:-3}"
DUMP_RETRY_SLEEP_SEC="${DUMP_RETRY_SLEEP_SEC:-5}"

STATUS_JSON="$BACKUP_DIR/last_backup_status.json"
LAST_SUCCESS_FILE="$BACKUP_DIR/last_backup_success.txt"

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M)
BACKUP_FILE="$BACKUP_DIR/governance_${TIMESTAMP}.sql.gz"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
}

write_status_error() {
    local detail="${1:-}"
    DETAIL="$detail" TS=$(date -u +%Y-%m-%dT%H:%M:%SZ) python3 -c '
import json, os
print(json.dumps({"status": "error", "timestamp": os.environ["TS"], "detail": os.environ.get("DETAIL", "")}))
' >"$STATUS_JSON"
}

write_status_ok() {
    local backup_path="$1"
    FILE="$backup_path" TS=$(date -u +%Y-%m-%dT%H:%M:%SZ) python3 -c '
import json, os
print(json.dumps({"status": "ok", "timestamp": os.environ["TS"], "file": os.environ["FILE"]}))
' >"$STATUS_JSON"
}

alert_failure() {
    local msg="$1"
    if [ "${UNITARES_BACKUP_NO_ALERT:-0}" = "1" ]; then
        return 0
    fi
    if [ "$(uname -s)" = "Darwin" ]; then
        osascript -e "display notification \"${msg//\"/\\\"}\" with title \"Governance backup failed\"" 2>/dev/null || true
    fi
}

ensure_postgres_running() {
    if "$PG_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
        return 0
    fi

    log "WARN: PostgreSQL not reachable; attempting to start via brew services"
    brew services start postgresql@17 2>/dev/null || true
    sleep 2

    if ! "$PG_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
        log "ERROR: PostgreSQL not reachable after start attempt"
        write_status_error "PostgreSQL not reachable"
        alert_failure "Governance backup: PostgreSQL not running"
        return 1
    fi

    log "PostgreSQL is running"
    return 0
}

wait_for_postgres() {
    local i=0
    while [ "$i" -lt "$PG_READY_ATTEMPTS" ]; do
        if "$PG_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DATABASE" >/dev/null 2>&1; then
            return 0
        fi
        i=$((i + 1))
        sleep "$PG_READY_SLEEP_SEC"
    done
    log "ERROR: Postgres not ready after ${PG_READY_ATTEMPTS} attempts (${PG_READY_SLEEP_SEC}s)"
    write_status_error "Postgres not ready (pg_isready timeout)"
    alert_failure "Governance backup: database not ready"
    return 1
}

run_pg_dump() {
    "$PG_BIN/pg_dump" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$DATABASE" | gzip >"$BACKUP_FILE"
}

# A clean pg_dump exit isn't proof the artifact is restorable: a disk-full or
# interrupted gzip can leave a valid-prefix-but-truncated file. Verify the gzip
# is intact AND the dump carries pg_dump's completion marker (cheap restore-level
# check short of a full pg_restore drill; mirrors lumen-backup's integrity_check).
verify_backup_artifact() {
    if ! gzip -t "$BACKUP_FILE" 2>/dev/null; then
        log "ERROR: backup gzip failed integrity test (gzip -t)"
        return 1
    fi
    local tail_out
    tail_out=$(gunzip -c "$BACKUP_FILE" 2>/dev/null | tail -15) || true
    if ! printf '%s\n' "$tail_out" | grep -q "PostgreSQL database dump complete"; then
        log "ERROR: backup missing pg_dump completion marker — likely truncated"
        return 1
    fi
    return 0
}

# --- main ---

if ! ensure_postgres_running; then
    exit 1
fi

if ! wait_for_postgres; then
    exit 1
fi

log "Starting backup to $BACKUP_FILE"

attempt=1
while [ "$attempt" -le "$DUMP_RETRIES" ]; do
    if run_pg_dump && verify_backup_artifact; then
        SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        log "Backup complete + verified: $BACKUP_FILE ($SIZE)"
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        echo "$ts" >"$LAST_SUCCESS_FILE"
        write_status_ok "$BACKUP_FILE"
        break
    fi
    rm -f "$BACKUP_FILE"
    if [ "$attempt" -lt "$DUMP_RETRIES" ]; then
        log "WARN: pg_dump failed (attempt $attempt/$DUMP_RETRIES), retrying in ${DUMP_RETRY_SLEEP_SEC}s"
        sleep "$DUMP_RETRY_SLEEP_SEC"
    fi
    attempt=$((attempt + 1))
done

if [ ! -f "$BACKUP_FILE" ]; then
    log "ERROR: pg_dump failed after $DUMP_RETRIES attempts"
    write_status_error "pg_dump failed"
    alert_failure "Governance backup: pg_dump failed"
    exit 1
fi

# Prune old backups (keep last N files)
PRUNED=$(ls -1t "$BACKUP_DIR"/governance_*.sql.gz 2>/dev/null | tail -n +$((KEEP_DAYS + 1)) | wc -l | tr -d ' ')
ls -1t "$BACKUP_DIR"/governance_*.sql.gz 2>/dev/null | tail -n +$((KEEP_DAYS + 1)) | xargs rm -f 2>/dev/null || true
if [ "$PRUNED" -gt 0 ]; then
    log "Pruned $PRUNED old backup(s)"
fi

# Trim log (keep last 200 lines)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 200 ]; then
    tail -200 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit 0
