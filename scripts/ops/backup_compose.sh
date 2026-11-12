#!/usr/bin/env bash
# backup_compose.sh — Verified PostgreSQL + Redis + server-data backup for
# Docker Compose deployments (local or cloud VPS).
#
# Stages all artifacts in a per-run directory and publishes mode-0600 files into
# $BACKUP_DIR only after every store and optional encryption step succeeds,
# keeping scripts/ops/check_governance_backup_health.sh accurate even if a
# partial stage fails on the first-ever run:
#   1. PostgreSQL + Apache AGE SQL dump (`governance_<ts>.sql.gz`), verified
#      with `gzip -t` and pg_dump's completion trailer.
#   2. Redis RDB snapshot (`redis_<ts>.rdb`) after a fresh `BGSAVE` advances
#      `LASTSAVE` with `rdb_last_bgsave_status:ok`, verified in-container with
#      `redis-check-rdb` (CRC64 + structure) and checked after copy for the
#      `REDISdddd` header and `0xff` EOF marker.
#   3. File-backed governance server data (`governance_data_<ts>.tar.gz`
#      streamed from `/app/data` while skipping `/app/data/hf` model weights in
#      place, without copying weights to host disk) so the auto-generated
#      `continuity_token_secret` travels with the dump.
#   4. Optional recipient-only `age` encryption (`AGE_RECIPIENT` or
#      `AGE_RECIPIENT_FILE`) — keep the private decryption key off the VPS.
#      When an `age` recipient is set, `BACKUP_KEEP_PLAINTEXT` defaults to `0`
#      so new plaintext dumps are removed inside `.stage_<ts>_*` before
#      publishing to `$BACKUP_DIR` (any orphaned `.stage_*` directory from a
#      prior crash is swept on the next lock acquisition; set
#      `PURGE_PRIOR_PLAINTEXT=1` after verifying `age -d` to delete older
#      unencrypted archives from `$BACKUP_DIR`, and `LOCK_MAX_AGE_SEC` bounds
#      unreadable cross-user locks under `hidepid=2`).
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
LOCK_ORPHAN_SEC="${LOCK_ORPHAN_SEC:-10}"
LOCK_MAX_AGE_SEC="${LOCK_MAX_AGE_SEC:-14400}"

AGE_BIN="${AGE_BIN:-age}"
AGE_RECIPIENT="${AGE_RECIPIENT:-}"
AGE_RECIPIENT_FILE="${AGE_RECIPIENT_FILE:-}"
if [ -n "$AGE_RECIPIENT" ] || [ -n "$AGE_RECIPIENT_FILE" ]; then
    BACKUP_KEEP_PLAINTEXT="${BACKUP_KEEP_PLAINTEXT:-0}"
else
    BACKUP_KEEP_PLAINTEXT="${BACKUP_KEEP_PLAINTEXT:-1}"
fi
PURGE_PRIOR_PLAINTEXT="${PURGE_PRIOR_PLAINTEXT:-0}"
BACKUP_SYNC_CMD="${BACKUP_SYNC_CMD:-}"

STATUS_JSON="$BACKUP_DIR/last_backup_status.json"
LAST_SUCCESS_FILE="$BACKUP_DIR/last_backup_success.txt"
LOCK_DIR="$BACKUP_DIR/.backup.lock"

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
STAGE_DIR=""
LOCK_OWNED=0
LOCK_TOKEN=""
SCRIPT_NAME="$(basename "$0")"
COMPLETED_OK=0
STATUS_WRITTEN=0

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
    STATUS_WRITTEN=1
}

write_status_ok() {
    local pg_out="$1"
    local redis_out="$2"
    local data_out="$3"
    local manifest_out="$4"
    local tmp="${STATUS_JSON}.tmp.$$"
    PG_OUT="$pg_out" REDIS_OUT="$redis_out" DATA_OUT="$data_out" MANIFEST_OUT="$manifest_out" TS=$(date -u +%Y-%m-%dT%H:%M:%SZ) python3 -c '
import json, os
print(json.dumps({
    "status": "ok",
    "timestamp": os.environ["TS"],
    "file": os.environ["PG_OUT"],
    "redis_file": os.environ["REDIS_OUT"],
    "data_file": os.environ["DATA_OUT"],
    "manifest_file": os.environ["MANIFEST_OUT"],
}))
' >"$tmp"
    mv "$tmp" "$STATUS_JSON"
    STATUS_WRITTEN=1
}

cleanup() {
    local rc=$?
    if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then
        rm -rf "$STAGE_DIR"
    fi
    if [ "$LOCK_OWNED" = "1" ]; then
        if [ "$COMPLETED_OK" != "1" ] && [ "$rc" -ne 0 ] && [ "$STATUS_WRITTEN" != "1" ]; then
            log "ERROR: backup aborted unexpectedly (exit $rc)" || true
            write_status_error "backup aborted unexpectedly (exit $rc)" || true
        fi
        if [ -d "$LOCK_DIR" ] && [ -n "$LOCK_TOKEN" ] \
            && [ "$(cat "$LOCK_DIR/token" 2>/dev/null || true)" = "$LOCK_TOKEN" ]; then
            rm -rf "$LOCK_DIR"
        fi
    fi
}
trap cleanup EXIT

is_lock_older_than() {
    local threshold="$1"
    python3 - "$LOCK_DIR" "$threshold" <<'PY'
import os, sys, time
lock_dir, max_age = sys.argv[1], float(sys.argv[2])
try:
    age = time.time() - os.path.getmtime(lock_dir)
except OSError:
    sys.exit(1)
sys.exit(0 if age >= max_age else 1)
PY
}

is_live_backup_holder() {
    local pid="$1"
    local recorded_cmd
    recorded_cmd=$(cat "$LOCK_DIR/cmd" 2>/dev/null || true)
    [ -n "$recorded_cmd" ] || recorded_cmd="backup_compose.sh"
    local cmdline
    cmdline=$(ps -ww -p "$pid" -o args= 2>/dev/null || ps -p "$pid" -o args= 2>/dev/null || true)
    python3 - "$pid" "$recorded_cmd" "$cmdline" "$LOCK_DIR" "$LOCK_MAX_AGE_SEC" <<'PY'
import os, shlex, sys, time
pid_s, recorded_cmd, cmdline, lock_dir, max_age_s = sys.argv[1:6]
try:
    pid = int(pid_s)
    if pid <= 0:
        sys.exit(1)
    os.kill(pid, 0)
except (ValueError, ProcessLookupError):
    sys.exit(1)
except PermissionError:
    pass

cmdline = cmdline.strip()
if cmdline:
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    exe = os.path.basename(tokens[0]) if tokens else ""
    shells = {"sh", "bash", "dash", "zsh", "ksh"}
    is_backup = (
        exe == recorded_cmd
        or (exe in shells and any(os.path.basename(t) == recorded_cmd for t in tokens[1:]))
    )
    sys.exit(0 if is_backup else 1)

# `ps` could not read args (e.g. hidepid=2): fall back to LOCK_MAX_AGE_SEC.
try:
    age = time.time() - os.path.getmtime(lock_dir)
except OSError:
    sys.exit(1)
sys.exit(0 if age < float(max_age_s) else 1)
PY
}

record_lock_metadata() {
    LOCK_TOKEN="$$_$(date +%s)"
    LOCK_OWNED=1
    printf '%s\n' "$SCRIPT_NAME" > "$LOCK_DIR/cmd"
    printf '%s\n' "$LOCK_TOKEN" > "$LOCK_DIR/token"
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
}

acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        record_lock_metadata
        return 0
    fi
    local old_pid
    old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    if [ -n "$old_pid" ] && ! is_live_backup_holder "$old_pid"; then
        log "WARN: removing stale backup lock from exited or reused PID $old_pid"
        rm -rf "$LOCK_DIR"
        if mkdir "$LOCK_DIR" 2>/dev/null; then
            record_lock_metadata
            return 0
        fi
    elif [ -z "$old_pid" ] && is_lock_older_than "$LOCK_ORPHAN_SEC"; then
        log "WARN: removing orphaned backup lock without PID file"
        rm -rf "$LOCK_DIR"
        if mkdir "$LOCK_DIR" 2>/dev/null; then
            record_lock_metadata
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
    local tail_out
    tail_out=$(gunzip -c "$target" 2>/dev/null | tail -15 || true)
    if ! printf '%s\n' "$tail_out" | grep -q "PostgreSQL database dump complete"; then
        log "ERROR: backup missing pg_dump completion marker — likely truncated"
        return 1
    fi
    return 0
}

verify_redis_rdb() {
    local target="$1"
    python3 - "$target" <<'PY'
import sys
path = sys.argv[1]
try:
    with open(path, "rb") as fh:
        head = fh.read(9)
        fh.seek(0, 2)
        size = fh.tell()
        if size < 18:
            sys.exit(1)
        fh.seek(-9, 2)
        tail = fh.read(9)
except OSError:
    sys.exit(1)
ok = head[:5] == b"REDIS" and head[5:9].isdigit() and len(tail) == 9 and tail[0] == 0xFF
sys.exit(0 if ok else 1)
PY
}

verify_data_archive() {
    local target="$1"
    if ! gzip -t "$target" 2>/dev/null; then
        return 1
    fi
    python3 - "$target" <<'PY'
import sys, tarfile
try:
    with tarfile.open(sys.argv[1], "r:gz") as tf:
        names = tf.getnames()
except Exception:
    sys.exit(1)
ok = "data" in names or any(n.startswith("data/") for n in names)
sys.exit(0 if ok else 1)
PY
}

dump_postgres() {
    local out="$1"
    local attempt=1
    while [ "$attempt" -le "$DUMP_RETRIES" ]; do
        rm -f "${out}.tmp"
        if "$DOCKER_BIN" exec -i "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$DATABASE" >/dev/null 2>&1 \
            && (set -o pipefail; "$DOCKER_BIN" exec -i "$PG_CONTAINER" pg_dump -U "$PG_USER" "$DATABASE" | gzip > "${out}.tmp") \
            && verify_pg_dump "${out}.tmp"; then
            chmod 600 "${out}.tmp"
            mv "${out}.tmp" "$out"
            return 0
        fi
        rm -f "${out}.tmp"
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
        if ! info=$(redis_cli INFO persistence); then
            return 1
        fi
        info=$(printf '%s' "$info" | tr -d '\r')
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
    local out="$1"
    if ! wait_redis_idle; then
        return 1
    fi

    local before_save now_sec
    if ! before_save=$(redis_cli LASTSAVE); then
        return 1
    fi
    before_save=$(printf '%s' "$before_save" | tr -d ' \t\r\n')
    if [ -z "$before_save" ]; then
        return 1
    fi
    now_sec=$(date +%s)
    if [ "$before_save" = "$now_sec" ]; then
        sleep 1
    fi

    local bgsave_out
    if ! bgsave_out=$(redis_cli BGSAVE 2>&1); then
        return 1
    fi
    bgsave_out=$(printf '%s' "$bgsave_out" | tr -d '\r')
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
        if ! after_save=$(redis_cli LASTSAVE); then
            return 1
        fi
        after_save=$(printf '%s' "$after_save" | tr -d ' \t\r\n')
        if ! info=$(redis_cli INFO persistence); then
            return 1
        fi
        info=$(printf '%s' "$info" | tr -d '\r')
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

    if ! "$DOCKER_BIN" exec -i "$REDIS_CONTAINER" redis-check-rdb "${rdb_dir}/${rdb_name}" >/dev/null 2>&1; then
        log "ERROR: in-container redis-check-rdb failed for ${rdb_dir}/${rdb_name}"
        return 1
    fi
    if ! "$DOCKER_BIN" cp "${REDIS_CONTAINER}:${rdb_dir}/${rdb_name}" "${out}.tmp"; then
        log "ERROR: docker cp failed for Redis RDB"
        rm -f "${out}.tmp"
        return 1
    fi
    if ! verify_redis_rdb "${out}.tmp"; then
        log "ERROR: copied Redis snapshot failed REDIS header / EOF marker check"
        rm -f "${out}.tmp"
        return 1
    fi
    chmod 600 "${out}.tmp"
    mv "${out}.tmp" "$out"
    return 0
}

dump_governance_data() {
    local out="$1"
    # Stream /app/data from inside the container while skipping /app/data/hf in
    # place so multi-GB Hugging Face weights never touch host backup disk.
    if ! "$DOCKER_BIN" exec -i "$GOV_CONTAINER" python -c '
import os, sys, tarfile
root = "/app/data"
if not os.path.isdir(root):
    sys.exit(2)
def _skip_hf(ti):
    if ti.name == "data/hf" or ti.name.startswith("data/hf/"):
        return None
    return ti
with tarfile.open(fileobj=sys.stdout.buffer, mode="w:gz") as tf:
    tf.add(root, arcname="data", filter=_skip_hf)
' > "${out}.tmp"; then
        log "ERROR: streaming /app/data from $GOV_CONTAINER failed"
        rm -f "${out}.tmp"
        return 1
    fi
    if ! verify_data_archive "${out}.tmp"; then
        log "ERROR: governance-data archive failed integrity or root check"
        rm -f "${out}.tmp"
        return 1
    fi
    chmod 600 "${out}.tmp"
    mv "${out}.tmp" "$out"
    return 0
}

encrypt_stage_artifact() {
    local src="$1"
    if [ -z "$AGE_RECIPIENT" ] && [ -z "$AGE_RECIPIENT_FILE" ]; then
        printf '%s\n' "$src"
        return 0
    fi
    local out="${src}.age"
    if [ -n "$AGE_RECIPIENT_FILE" ]; then
        if ! "$AGE_BIN" -R "$AGE_RECIPIENT_FILE" -o "${out}.tmp" "$src"; then
            log "ERROR: age exited non-zero for $src"
            rm -f "${out}.tmp"
            return 1
        fi
    else
        if ! "$AGE_BIN" -r "$AGE_RECIPIENT" -o "${out}.tmp" "$src"; then
            log "ERROR: age exited non-zero for $src"
            rm -f "${out}.tmp"
            return 1
        fi
    fi
    if [ ! -s "${out}.tmp" ]; then
        log "ERROR: age produced empty output for $src"
        rm -f "${out}.tmp"
        return 1
    fi
    chmod 600 "${out}.tmp"
    mv "${out}.tmp" "$out"
    if [ "$BACKUP_KEEP_PLAINTEXT" = "0" ]; then
        rm -f "$src"
    fi
    printf '%s\n' "$out"
    return 0
}

write_manifest() {
    local manifest="$1"
    shift
    if ! python3 - "$manifest" "$@" <<'PY'
import hashlib, pathlib, sys
manifest = pathlib.Path(sys.argv[1])
lines = []
for raw in sys.argv[2:]:
    p = pathlib.Path(raw)
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            digest.update(chunk)
    lines.append(f"{digest.hexdigest()}  {p.name}\n")
manifest.write_text("".join(lines), encoding="utf-8")
PY
    then
        rm -f "$manifest"
        return 1
    fi
    if [ ! -s "$manifest" ]; then
        rm -f "$manifest"
        return 1
    fi
    chmod 600 "$manifest" || return 1
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

case "$KEEP_COUNT" in
    ''|*[!0-9]*|0) fail "KEEP_COUNT must be a positive integer (got '$KEEP_COUNT')" ;;
esac

if [ -n "$AGE_RECIPIENT" ] && [ -n "$AGE_RECIPIENT_FILE" ]; then
    fail "set AGE_RECIPIENT or AGE_RECIPIENT_FILE, not both"
fi
if [ -n "$AGE_RECIPIENT_FILE" ] && [ ! -r "$AGE_RECIPIENT_FILE" ]; then
    fail "AGE_RECIPIENT_FILE is not readable: $AGE_RECIPIENT_FILE"
fi
if [ "$BACKUP_KEEP_PLAINTEXT" = "0" ] && [ -z "$AGE_RECIPIENT" ] && [ -z "$AGE_RECIPIENT_FILE" ]; then
    fail "BACKUP_KEEP_PLAINTEXT=0 requires AGE_RECIPIENT or AGE_RECIPIENT_FILE"
fi
if [ "$PURGE_PRIOR_PLAINTEXT" = "1" ] && [ "$BACKUP_KEEP_PLAINTEXT" != "0" ]; then
    fail "PURGE_PRIOR_PLAINTEXT=1 requires AGE_RECIPIENT or AGE_RECIPIENT_FILE with BACKUP_KEEP_PLAINTEXT=0"
fi

acquire_lock
# Sweep any leftover staging dirs from a prior SIGKILL, OOM, or host reboot now
# that we hold the exclusive backup lock.
rm -rf "$BACKUP_DIR"/.stage_*
STAGE_DIR=$(mktemp -d "$BACKUP_DIR/.stage_${TIMESTAMP}_XXXXXX")

STAGE_PG="$STAGE_DIR/governance_${TIMESTAMP}.sql.gz"
STAGE_REDIS="$STAGE_DIR/redis_${TIMESTAMP}.rdb"
STAGE_DATA="$STAGE_DIR/governance_data_${TIMESTAMP}.tar.gz"

log "Starting Compose backup ($TIMESTAMP)"

if ! dump_postgres "$STAGE_PG"; then
    fail "PostgreSQL dump or verification failed"
fi
if ! dump_redis "$STAGE_REDIS"; then
    fail "Redis BGSAVE or RDB verification failed"
fi
if ! dump_governance_data "$STAGE_DATA"; then
    fail "governance-data archive failed"
fi

if ! STAGE_PG_OUT=$(encrypt_stage_artifact "$STAGE_PG"); then
    fail "age encryption failed for PostgreSQL dump"
fi
if ! STAGE_REDIS_OUT=$(encrypt_stage_artifact "$STAGE_REDIS"); then
    fail "age encryption failed for Redis RDB"
fi
if ! STAGE_DATA_OUT=$(encrypt_stage_artifact "$STAGE_DATA"); then
    fail "age encryption failed for governance-data archive"
fi

STAGE_MANIFEST="$STAGE_DIR/manifest_${TIMESTAMP}.sha256"
if ! write_manifest "$STAGE_MANIFEST" "$STAGE_PG_OUT" "$STAGE_REDIS_OUT" "$STAGE_DATA_OUT"; then
    fail "SHA-256 manifest generation failed"
fi

# Run off-site upload BEFORE publishing into $BACKUP_DIR so a failing sync
# never leaves a fresh governance_*.sql.gz in $BACKUP_DIR that satisfies
# check_governance_backup_health.sh's mtime fallback.
if [ -n "$BACKUP_SYNC_CMD" ]; then
    log "Running BACKUP_SYNC_CMD"
    if ! BACKUP_DIR="$BACKUP_DIR" \
        BACKUP_STAGE_DIR="$STAGE_DIR" \
        PG_BACKUP="$STAGE_PG_OUT" \
        REDIS_BACKUP="$STAGE_REDIS_OUT" \
        DATA_BACKUP="$STAGE_DATA_OUT" \
        MANIFEST_FILE="$STAGE_MANIFEST" \
        sh -c "$BACKUP_SYNC_CMD"; then
        fail "BACKUP_SYNC_CMD failed"
    fi
fi

# Publish all staged files into $BACKUP_DIR only after capture, encryption, and sync succeeded.
for staged_item in "$STAGE_DIR"/*; do
    [ -e "$staged_item" ] || continue
    chmod 600 "$staged_item"
    mv "$staged_item" "$BACKUP_DIR/$(basename "$staged_item")"
done
rmdir "$STAGE_DIR"
STAGE_DIR=""

PG_FINAL="$BACKUP_DIR/$(basename "$STAGE_PG_OUT")"
REDIS_FINAL="$BACKUP_DIR/$(basename "$STAGE_REDIS_OUT")"
DATA_FINAL="$BACKUP_DIR/$(basename "$STAGE_DATA_OUT")"
MANIFEST_FINAL="$BACKUP_DIR/$(basename "$STAGE_MANIFEST")"

TS_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "$TS_ISO" > "${LAST_SUCCESS_FILE}.tmp.$$"
mv "${LAST_SUCCESS_FILE}.tmp.$$" "$LAST_SUCCESS_FILE"
write_status_ok "$PG_FINAL" "$REDIS_FINAL" "$DATA_FINAL" "$MANIFEST_FINAL"
COMPLETED_OK=1
log "Backup complete + verified: $PG_FINAL, $REDIS_FINAL, $DATA_FINAL"

if [ "$BACKUP_KEEP_PLAINTEXT" = "0" ] && [ "$PURGE_PRIOR_PLAINTEXT" = "1" ]; then
    rm -f "$BACKUP_DIR"/governance_*.sql.gz \
          "$BACKUP_DIR"/redis_*.rdb \
          "$BACKUP_DIR"/governance_data_*.tar.gz 2>/dev/null \
        || log "WARN: could not remove one or more prior plaintext dumps"
else
    prune_series "governance_*.sql.gz"
    prune_series "redis_*.rdb"
    prune_series "governance_data_*.tar.gz"
fi
prune_series "governance_*.sql.gz.age"
prune_series "redis_*.rdb.age"
prune_series "governance_data_*.tar.gz.age"
prune_series "manifest_*.sha256"

if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 200 ]; then
    tail -200 "$LOG" > "${LOG}.tmp.$$" && mv "${LOG}.tmp.$$" "$LOG"
fi

exit 0
