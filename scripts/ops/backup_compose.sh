#!/usr/bin/env bash
# backup_compose.sh — Verified PostgreSQL + Redis + server-data backup for
# Docker Compose deployments (local or cloud VPS).
#
# Produces mode-0600 artifacts in $BACKUP_DIR compatible with
# scripts/ops/check_governance_backup_health.sh (`last_backup_success.txt` and
# `last_backup_status.json`), closing the Compose backup gaps documented in
# docs/operations/cloud-deploy-runbook.md:
#   1. PostgreSQL + Apache AGE SQL dump (`governance_<ts>.sql.gz`), verified
#      with `gzip -t` and pg_dump's completion trailer.
#   2. Redis RDB snapshot (`redis_<ts>.rdb`) after a fresh `BGSAVE` advances
#      `LASTSAVE` with `rdb_last_bgsave_status:ok` and a `REDIS` magic header.
#   3. File-backed governance server data (`governance_data_<ts>.tar.gz` from
#      `/app/data`, excluding the large `/app/data/hf` model cache) so the
#      auto-generated `continuity_token_secret` travels with the dump.
#   4. Optional recipient-only `age` encryption (`AGE_RECIPIENT` or
#      `AGE_RECIPIENT_FILE`) — keep the private decryption key off the VPS —
#      plus an optional `BACKUP_SYNC_CMD` hook (e.g. `rclone sync` to S3/R2/B2).
#
# Note: `.env` itself is NOT copied into these archives; escrow deployment
# credentials separately in a password manager or secret store.

set -euo pipefail
umask 077

DOCKER_BIN="${DOCKER_BIN:-docker}"
PG_CONTAINER="${PG_CONTAINER:-unitares-postgres-age}"
REDIS_CONTAINER="${REDIS_CONTAINER:-unitares-redis}"
GOV_CONTAINER="${GOV_CONTAINER:-unitares-governance-mcp}"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/governance}"
DATABASE="${DATABASE:-governance}"
PG_USER="${PG_USER:-postgres}"
KEEP_COUNT="${KEEP_COUNT:-${KEEP_DAYS:-14}}"
LOG="${LOG:-$BACKUP_DIR/backup.log}"

DUMP_RETRIES="${DUMP_RETRIES:-3}"
DUMP_RETRY_SLEEP_SEC="${DUMP_RETRY_SLEEP_SEC:-5}"
REDIS_BGSAVE_TIMEOUT_SEC="${REDIS_BGSAVE_TIMEOUT_SEC:-120}"

AGE_BIN="${AGE_BIN:-age}"
AGE_RECIPIENT="${AGE_RECIPIENT:-}"
AGE_RECIPIENT_FILE="${AGE_RECIPIENT_FILE:-}"
BACKUP_KEEP_PLAINTEXT="${BACKUP_KEEP_PLAINTEXT:-1}"
BACKUP_SYNC_CMD="${BACKUP_SYNC_CMD:-}"

STATUS_JSON="$BACKUP_DIR/last_backup_status.json"
LAST_SUCCESS_FILE="$BACKUP_DIR/last_backup_success.txt"
LOCK_DIR="$BACKUP_DIR/.backup.lock"

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
PG_FILE="$BACKUP_DIR/governance_${TIMESTAMP}.sql.gz"
REDIS_FILE="$BACKUP_DIR/redis_${TIMESTAMP}.rdb"
DATA_FILE="$BACKUP_DIR/governance_data_${TIMESTAMP}.tar.gz"
TMP_WORK=""

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
}

write_status_error() {
    local detail="${1:-}"
    local tmp="${STATUS_JSON}.tmp.$$"
    DETAIL="$detail" TS=$(date -u +%Y-%m-%dT%H:%M:%SZ) python3 -c '
import json, os
print(json.dumps({"status": "error", "timestamp": os.environ["TS"], "detail": os.environ.get("DETAIL", "")}))
' >"$tmp"
    mv "$tmp" "$STATUS_JSON"
}

write_status_ok() {
    local pg_out="$1"
    local redis_out="$2"
    local data_out="$3"
    local tmp="${STATUS_JSON}.tmp.$$"
    PG_OUT="$pg_out" REDIS_OUT="$redis_out" DATA_OUT="$data_out" TS=$(date -u +%Y-%m-%dT%H:%M:%SZ) python3 -c '
import json, os
print(json.dumps({
    "status": "ok",
    "timestamp": os.environ["TS"],
    "file": os.environ["PG_OUT"],
    "redis_file": os.environ["REDIS_OUT"],
    "data_file": os.environ["DATA_OUT"],
}))
' >"$tmp"
    mv "$tmp" "$STATUS_JSON"
}

cleanup() {
    if [ -n "$TMP_WORK" ] && [ -d "$TMP_WORK" ]; then
        rm -rf "$TMP_WORK"
    fi
    rm -f "${PG_FILE}.tmp" "${REDIS_FILE}.tmp" "${DATA_FILE}.tmp"
    if [ -d "$LOCK_DIR" ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ]; then
        rm -rf "$LOCK_DIR"
    fi
}
trap cleanup EXIT

acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$$" > "$LOCK_DIR/pid"
        return 0
    fi
    local old_pid
    old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    if [ -n "$old_pid" ] && ! kill -0 "$old_pid" 2>/dev/null; then
        log "WARN: removing stale backup lock from exited PID $old_pid"
        rm -rf "$LOCK_DIR"
        if mkdir "$LOCK_DIR" 2>/dev/null; then
            echo "$$" > "$LOCK_DIR/pid"
            return 0
        fi
    fi
    log "ERROR: another backup_compose.sh run holds $LOCK_DIR (PID ${old_pid:-unknown})"
    write_status_error "backup lock held by PID ${old_pid:-unknown}"
    exit 1
}

fail() {
    local msg="$1"
    log "ERROR: $msg"
    write_status_error "$msg"
    echo "backup_compose.sh: $msg" >&2
    exit 1
}

redis_cli() {
    if [ -n "${REDIS_PASSWORD:-}" ]; then
        "$DOCKER_BIN" exec -i -e "REDISCLI_AUTH=$REDIS_PASSWORD" "$REDIS_CONTAINER" redis-cli "$@"
    else
        "$DOCKER_BIN" exec -i "$REDIS_CONTAINER" redis-cli "$@"
    fi
}

verify_pg_dump() {
    local target="$1"
    if ! gzip -t "$target" 2>/dev/null; then
        log "ERROR: backup gzip failed integrity test (gzip -t)"
        return 1
    fi
    # Consume via `|| true` subshell so early pipe close never trips pipefail.
    local tail_out
    tail_out=$(gunzip -c "$target" 2>/dev/null | tail -15 || true)
    if ! printf '%s\n' "$tail_out" | grep -q "PostgreSQL database dump complete"; then
        log "ERROR: backup missing pg_dump completion marker — likely truncated"
        return 1
    fi
    return 0
}

dump_postgres() {
    local attempt=1
    while [ "$attempt" -le "$DUMP_RETRIES" ]; do
        rm -f "${PG_FILE}.tmp"
        if "$DOCKER_BIN" exec -i "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$DATABASE" >/dev/null 2>&1 \
            && "$DOCKER_BIN" exec -i "$PG_CONTAINER" pg_dump -U "$PG_USER" "$DATABASE" | gzip > "${PG_FILE}.tmp" \
            && verify_pg_dump "${PG_FILE}.tmp"; then
            mv "${PG_FILE}.tmp" "$PG_FILE"
            return 0
        fi
        rm -f "${PG_FILE}.tmp"
        if [ "$attempt" -lt "$DUMP_RETRIES" ]; then
            log "WARN: pg_dump failed (attempt $attempt/$DUMP_RETRIES), retrying in ${DUMP_RETRY_SLEEP_SEC}s"
            sleep "$DUMP_RETRY_SLEEP_SEC"
        fi
        attempt=$((attempt + 1))
    done
    return 1
}

wait_redis_idle() {
    local elapsed=0
    while [ "$elapsed" -lt "$REDIS_BGSAVE_TIMEOUT_SEC" ]; do
        local info
        info=$(redis_cli INFO persistence | tr -d '\r') || return 1
        if printf '%s\n' "$info" | grep -q "^rdb_bgsave_in_progress:0$" \
            && ! printf '%s\n' "$info" | grep -q "^aof_rewrite_in_progress:1$"; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

dump_redis() {
    wait_redis_idle || return 1

    local before_save now_sec
    before_save=$(redis_cli LASTSAVE | tr -d ' \t\r\n') || return 1
    now_sec=$(date +%s)
    # LASTSAVE has 1-second resolution; if a save completed earlier inside the
    # same wall-clock second, wait for the next second so LASTSAVE strictly advances.
    if [ "$before_save" = "$now_sec" ]; then
        sleep 1
    fi

    local bgsave_out
    bgsave_out=$(redis_cli BGSAVE 2>&1 | tr -d '\r') || return 1
    case "$bgsave_out" in
        *"Background saving started"*|*"Background saving scheduled"*) ;;
        *)
            log "ERROR: unexpected redis BGSAVE response: $bgsave_out"
            return 1
            ;;
    esac

    local elapsed=0
    while [ "$elapsed" -lt "$REDIS_BGSAVE_TIMEOUT_SEC" ]; do
        local after_save info
        after_save=$(redis_cli LASTSAVE | tr -d ' \t\r\n') || return 1
        info=$(redis_cli INFO persistence | tr -d '\r') || return 1
        if [ "$after_save" != "$before_save" ] \
            && printf '%s\n' "$info" | grep -q "^rdb_bgsave_in_progress:0$"; then
            if ! printf '%s\n' "$info" | grep -q "^rdb_last_bgsave_status:ok$"; then
                log "ERROR: Redis BGSAVE finished with non-ok rdb_last_bgsave_status"
                return 1
            fi
            break
        fi
        if printf '%s\n' "$info" | grep -q "^rdb_bgsave_in_progress:0$" \
            && printf '%s\n' "$info" | grep -q "^rdb_last_bgsave_status:err$"; then
            log "ERROR: Redis BGSAVE failed (rdb_last_bgsave_status:err)"
            return 1
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    if [ "$elapsed" -ge "$REDIS_BGSAVE_TIMEOUT_SEC" ]; then
        log "ERROR: timed out waiting for Redis LASTSAVE to advance"
        return 1
    fi

    local rdb_dir="/data"
    local rdb_name="dump.rdb"
    local cfg_dir cfg_name
    cfg_dir=$(redis_cli CONFIG GET dir 2>/dev/null | tr -d '\r' | sed -n '2p' || true)
    cfg_name=$(redis_cli CONFIG GET dbfilename 2>/dev/null | tr -d '\r' | sed -n '2p' || true)
    [ -n "$cfg_dir" ] && rdb_dir="$cfg_dir"
    [ -n "$cfg_name" ] && rdb_name="$cfg_name"

    "$DOCKER_BIN" cp "${REDIS_CONTAINER}:${rdb_dir}/${rdb_name}" "${REDIS_FILE}.tmp"
    local magic
    magic=$(head -c 5 "${REDIS_FILE}.tmp" 2>/dev/null || true)
    if [ "$magic" != "REDIS" ]; then
        log "ERROR: copied Redis snapshot missing REDIS header"
        rm -f "${REDIS_FILE}.tmp"
        return 1
    fi
    chmod 600 "${REDIS_FILE}.tmp"
    mv "${REDIS_FILE}.tmp" "$REDIS_FILE"
    return 0
}

dump_governance_data() {
    TMP_WORK=$(mktemp -d "$BACKUP_DIR/.gov_data_tmp.XXXXXX")
    "$DOCKER_BIN" cp "${GOV_CONTAINER}:/app/data" "$TMP_WORK/data"
    # Exclude `/app/data/hf` (docker-compose.full.yml's multi-GB HuggingFace
    # model cache) while retaining `secrets/` (`continuity_token_secret`) and
    # other file-backed server state.
    rm -rf "$TMP_WORK/data/hf"
    tar -C "$TMP_WORK" -czf "${DATA_FILE}.tmp" data
    if ! gzip -t "${DATA_FILE}.tmp" 2>/dev/null; then
        log "ERROR: governance-data archive failed gzip -t"
        return 1
    fi
    chmod 600 "${DATA_FILE}.tmp"
    mv "${DATA_FILE}.tmp" "$DATA_FILE"
    rm -rf "$TMP_WORK"
    TMP_WORK=""
    return 0
}

encrypt_if_configured() {
    local src="$1"
    if [ -z "$AGE_RECIPIENT" ] && [ -z "$AGE_RECIPIENT_FILE" ]; then
        printf '%s\n' "$src"
        return 0
    fi
    local out="${src}.age"
    if [ -n "$AGE_RECIPIENT_FILE" ]; then
        "$AGE_BIN" -R "$AGE_RECIPIENT_FILE" -o "${out}.tmp" "$src"
    else
        "$AGE_BIN" -r "$AGE_RECIPIENT" -o "${out}.tmp" "$src"
    fi
    if [ ! -s "${out}.tmp" ]; then
        rm -f "${out}.tmp"
        return 1
    fi
    chmod 600 "${out}.tmp"
    mv "${out}.tmp" "$out"
    if [ "$BACKUP_KEEP_PLAINTEXT" = "0" ]; then
        rm -f "$src"
    fi
    printf '%s\n' "$out"
}

prune_series() {
    local pattern="$1"
    local files
    files=$(ls -1t "$BACKUP_DIR"/$pattern 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) || true)
    if [ -n "$files" ]; then
        printf '%s\n' "$files" | xargs rm -f 2>/dev/null || true
    fi
}

# --- main ---

acquire_lock

if [ -n "$AGE_RECIPIENT" ] && [ -n "$AGE_RECIPIENT_FILE" ]; then
    fail "set AGE_RECIPIENT or AGE_RECIPIENT_FILE, not both"
fi
if [ -n "$AGE_RECIPIENT_FILE" ] && [ ! -r "$AGE_RECIPIENT_FILE" ]; then
    fail "AGE_RECIPIENT_FILE is not readable: $AGE_RECIPIENT_FILE"
fi

log "Starting Compose backup ($TIMESTAMP)"

dump_postgres || fail "PostgreSQL dump or verification failed"
dump_redis || fail "Redis BGSAVE or RDB verification failed"
dump_governance_data || fail "governance-data archive failed"

PG_FINAL=$(encrypt_if_configured "$PG_FILE") || fail "age encryption failed for $PG_FILE"
REDIS_FINAL=$(encrypt_if_configured "$REDIS_FILE") || fail "age encryption failed for $REDIS_FILE"
DATA_FINAL=$(encrypt_if_configured "$DATA_FILE") || fail "age encryption failed for $DATA_FILE"

if [ -n "$BACKUP_SYNC_CMD" ]; then
    log "Running BACKUP_SYNC_CMD"
    BACKUP_DIR="$BACKUP_DIR" \
    PG_BACKUP="$PG_FINAL" \
    REDIS_BACKUP="$REDIS_FINAL" \
    DATA_BACKUP="$DATA_FINAL" \
        sh -c "$BACKUP_SYNC_CMD" || fail "BACKUP_SYNC_CMD failed"
fi

TS_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "$TS_ISO" > "${LAST_SUCCESS_FILE}.tmp.$$"
mv "${LAST_SUCCESS_FILE}.tmp.$$" "$LAST_SUCCESS_FILE"
write_status_ok "$PG_FINAL" "$REDIS_FINAL" "$DATA_FINAL"
log "Backup complete + verified: $PG_FINAL, $REDIS_FINAL, $DATA_FINAL"

if [ "$BACKUP_KEEP_PLAINTEXT" != "0" ]; then
    prune_series "governance_*.sql.gz"
    prune_series "redis_*.rdb"
    prune_series "governance_data_*.tar.gz"
fi
if [ -n "$AGE_RECIPIENT" ] || [ -n "$AGE_RECIPIENT_FILE" ]; then
    prune_series "governance_*.sql.gz.age"
    prune_series "redis_*.rdb.age"
    prune_series "governance_data_*.tar.gz.age"
fi

if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 200 ]; then
    tail -200 "$LOG" > "${LOG}.tmp.$$" && mv "${LOG}.tmp.$$" "$LOG"
fi

exit 0
