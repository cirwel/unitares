#!/usr/bin/env bash
# Deploy the Surface Lease Plane from a DEDICATED clean worktree pinned to
# origin/master — never from a developer working tree.
#
# Why: the running service starts via `mix run` against a checkout on disk. If
# that checkout is the dev tree, it drifts — stale feature branches, uncommitted
# edits to the very files being served. On 2026-06-02 the running lease plane
# was serving un-reviewed local edits to http_router.ex because it ran from
# ~/projects/unitares while that checkout sat on a feature branch with WIP, and
# a merged fix (#568) was NOT actually live. This is the
# running-process-vs-master-commit drift class (feedback_running-process-vs-
# master-commit.md). BEAM hot-code-reload is the eventual answer (see the
# operator runbook "Hot code reload"); until that's automated, this script makes
# full-restart deploys reproducible from a clean tree.
#
# Idempotent: creates the deploy worktree if missing, fast-forwards it to
# origin/master (never a destructive reset), recompiles, restarts the
# LaunchAgent, and verifies health.
#
# Shared blocks (lock, plist preflight, ff) live in deploy-lib.sh.
set -euo pipefail

# --apply-migrations is explicit because DDL remains an operator decision. It
# is schema-only: the process exits after apply+verification so coordinated
# migrations can keep both writer runtimes quiesced and restart them in their
# required order. The separate --dialectic-writers-quiesced flag is an
# operator attestation, not something this wrapper may infer from DDL consent.
# Run this script again without either flag to deploy code.
APPLY_MIGRATIONS="${UNITARES_DEPLOY_APPLY_MIGRATIONS:-0}"
DIALECTIC_WRITERS_QUIESCED=0
for arg in "$@"; do
  case "$arg" in
    --apply-migrations) APPLY_MIGRATIONS=1 ;;
    --dialectic-writers-quiesced) DIALECTIC_WRITERS_QUIESCED=1 ;;
    *) echo "[deploy] unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$DIALECTIC_WRITERS_QUIESCED" == 1 && "$APPLY_MIGRATIONS" != 1 ]]; then
  echo "[deploy] REFUSING: --dialectic-writers-quiesced is valid only with --apply-migrations." >&2
  exit 2
fi
if [[ "$APPLY_MIGRATIONS" == 1 && "$DIALECTIC_WRITERS_QUIESCED" != 1 ]]; then
  echo "[deploy] REFUSING: migration apply also requires the separate --dialectic-writers-quiesced attestation." >&2
  echo "[deploy] Block submissions and stop/unload both governance-mcp and lease-plane writers first." >&2
  exit 2
fi

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_LEASE_PLANE_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.lease-plane"
PLIST="${UNITARES_LEASE_PLANE_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PLIST_BUDDY="${UNITARES_PLIST_BUDDY:-/usr/libexec/PlistBuddy}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

# Preflight parity with the sibling scripts: if the plist still loads from the
# dev checkout, the restart below targets the OLD location and the health
# probe passes against it. (Historically this script had no preflight — the
# exact false-success gap the preflight exists to close.)
deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY" \
  --allow-env UNITARES_LEASE_PLANE_ALLOW_DEV \
  --recipe "[deploy]   sed -i '' 's|$REPO|$DEPLOY|g' \"$PLIST\"   # or re-render the plist template against $DEPLOY"

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
PREV="$DEPLOY_LIB_PREV"
FORWARD_REVISION="$(git -C "$DEPLOY" rev-parse HEAD)"
MIGRATION_APPLY_ATTEMPTED=0

recover_migration_checkout() {
  local reason="$1"
  if [[ "$MIGRATION_APPLY_ATTEMPTED" == 1 ]]; then
    echo "[deploy] FORWARD RECOVERY REQUIRED — $reason" >&2
    echo "[deploy] A migration apply was attempted; PREV may be below the migration-078 runtime floor." >&2
    echo "[deploy] Keeping both dialectic writers stopped and retaining the reviewed forward revision." >&2
    launchctl bootout "gui/$UID_NUM/com.unitares.governance-mcp" >/dev/null 2>&1 || :
    launchctl bootout "gui/$UID_NUM/com.unitares.lease-plane" >/dev/null 2>&1 || :
    deploy_lib_restore_reviewed_worktree \
      "$TAG" "$DEPLOY" "$FORWARD_REVISION" || :
    return 1
  fi
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV"
}

verify_migration_revision() {
  if deploy_lib_verify_expected_revision "$TAG" "$DEPLOY" HEAD; then
    return 0
  fi
  recover_migration_checkout "the reviewed migration revision changed" || :
  return 1
}

# The lease plane reads the same schema as the MCP. In particular, both the
# legacy and frontier-aware dialectic writers reference transcript_revision;
# a healthy process on code ahead of migration 077 would fail only when a
# resolution arrives. Refuse that half-deploy here, before compile/restart.
# Migration 078 is a code/schema version floor, not a generically additive
# change: pre-078 BEAM and old Python sweepers must not restart over it.
MIGRATE="$DEPLOY/scripts/dev/apply_migrations.py"
if [[ ! -f "$MIGRATE" ]]; then
  echo "[deploy] REFUSING: migration checker is missing from the reviewed deploy revision." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi

# Resolve the database exactly as the launchd service entrypoint does. launchd
# does not inherit the operator shell; its installed plist may persist HOME, a
# non-default secrets path, or a direct database URL.  Read that actual plist
# before sourcing secrets so the migration check cannot target DB A while the
# restarted service targets DB B.
DEPLOY_AMBIENT_SECRETS_ENV="${UNITARES_SECRETS_ENV:-}"
DEPLOY_AMBIENT_DB_URL="${UNITARES_LEASE_PLANE_DATABASE_URL:-}"
DEPLOY_OPERATOR_HOME="$HOME"
unset UNITARES_SECRETS_ENV UNITARES_LEASE_PLANE_DATABASE_URL

read_lease_plist_env() {
  "$PLIST_BUDDY" \
    -c "Print :EnvironmentVariables:$1" "$PLIST" 2>/dev/null
}

if [[ ! -r "$PLIST" ]] \
   || ! "$PLIST_BUDDY" -c 'Print :EnvironmentVariables' \
        "$PLIST" >/dev/null 2>&1; then
  echo "[deploy] REFUSING: $PLIST has no readable EnvironmentVariables dictionary." >&2
  echo "[deploy] The migration preflight cannot prove which environment the restarted lease plane will use." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi

PLIST_HOME="$(read_lease_plist_env HOME || true)"
PLIST_SECRETS_ENV="$(read_lease_plist_env UNITARES_SECRETS_ENV || true)"
PLIST_DB_URL=""
PLIST_DB_URL_PRESENT=0
if PLIST_DB_URL="$(read_lease_plist_env UNITARES_LEASE_PLANE_DATABASE_URL)"; then
  PLIST_DB_URL_PRESENT=1
fi
if [[ -z "$PLIST_HOME" || "$PLIST_HOME" != /* ]]; then
  echo "[deploy] REFUSING: $PLIST must provide an absolute EnvironmentVariables/HOME." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
if [[ -n "$PLIST_SECRETS_ENV" && "$PLIST_SECRETS_ENV" != /* ]]; then
  echo "[deploy] REFUSING: plist UNITARES_SECRETS_ENV must be absolute so runtime database parity is provable." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
if [[ "$PLIST_DB_URL_PRESENT" == 1 && -z "$PLIST_DB_URL" ]]; then
  echo "[deploy] REFUSING: plist UNITARES_LEASE_PLANE_DATABASE_URL is present but empty." >&2
  echo "[deploy] Elixir treats that empty value as configured, so substituting the migration default would target a different database." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi

export HOME="$PLIST_HOME"
SECRETS_FILE="${PLIST_SECRETS_ENV:-$PLIST_HOME/.config/unitares/secrets.env}"
if [[ -z "$PLIST_SECRETS_ENV" && ! -f "$SECRETS_FILE" \
      && -f "$PLIST_HOME/.config/cirwel/secrets.env" ]]; then
  SECRETS_FILE="$PLIST_HOME/.config/cirwel/secrets.env"
fi
if [[ -n "$DEPLOY_AMBIENT_SECRETS_ENV" && "$DEPLOY_AMBIENT_SECRETS_ENV" != "$SECRETS_FILE" ]]; then
  echo "[deploy] REFUSING: ambient UNITARES_SECRETS_ENV is not inherited by the lease-plane LaunchAgent." >&2
  echo "[deploy] Configure the persisted service environment or update $SECRETS_FILE instead." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
if [[ -n "$PLIST_SECRETS_ENV" ]]; then
  export UNITARES_SECRETS_ENV="$PLIST_SECRETS_ENV"
fi
if [[ -n "$PLIST_DB_URL" ]]; then
  export UNITARES_LEASE_PLANE_DATABASE_URL="$PLIST_DB_URL"
fi
if [[ -f "$SECRETS_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
fi
# The runtime HOME was needed while sourcing because a secrets file may expand
# it. The deploy helper's plist-hash baseline belongs to the invoking operator,
# however; leaving the service HOME active here could miss a changed-plist
# baseline and incorrectly choose kickstart for a cached launchd definition.
export HOME="$DEPLOY_OPERATOR_HOME"
deploy_var_is_exported() {
  declare -p "$1" 2>/dev/null | grep -q '^declare -x '
}
if [[ -n "${UNITARES_LEASE_PLANE_DATABASE_URL:-}" ]] \
   && ! deploy_var_is_exported UNITARES_LEASE_PLANE_DATABASE_URL; then
  echo "[deploy] REFUSING: UNITARES_LEASE_PLANE_DATABASE_URL is assigned but not exported in $SECRETS_FILE." >&2
  echo "[deploy] start.sh execs mix, so an unexported value would not reach the service." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
if [[ -n "${LEASE_PLANE_BEARER_TOKEN:-}" ]] \
   && ! deploy_var_is_exported LEASE_PLANE_BEARER_TOKEN; then
  echo "[deploy] REFUSING: LEASE_PLANE_BEARER_TOKEN is assigned but not exported in $SECRETS_FILE." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
RUNTIME_DB_URL="${UNITARES_LEASE_PLANE_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/governance}"
if [[ -n "$DEPLOY_AMBIENT_DB_URL" && "$DEPLOY_AMBIENT_DB_URL" != "$RUNTIME_DB_URL" ]]; then
  echo "[deploy] REFUSING: ambient UNITARES_LEASE_PLANE_DATABASE_URL is not inherited by the lease-plane LaunchAgent." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
if [[ -n "${UNITARES_DEPLOY_DB_URL:-}" && "$UNITARES_DEPLOY_DB_URL" != "$RUNTIME_DB_URL" ]]; then
  echo "[deploy] REFUSING: UNITARES_DEPLOY_DB_URL does not match the lease-plane runtime database." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
if [[ -z "${LEASE_PLANE_BEARER_TOKEN:-}" ]]; then
  echo "[deploy] REFUSING: LEASE_PLANE_BEARER_TOKEN is absent from the service secrets file." >&2
  deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
  exit 1
fi
MIGRATE_DBURL=(--db-url "$RUNTIME_DB_URL")

if [[ "$APPLY_MIGRATIONS" == 1 ]]; then
  for writer_label in com.unitares.governance-mcp com.unitares.lease-plane; do
    if launchctl print "gui/$UID_NUM/$writer_label" >/dev/null 2>&1; then
      echo "[deploy] REFUSING: $writer_label is still loaded; writer quiescence is not established." >&2
      echo "[deploy] Stop/unload both dialectic writers, then retry the schema-only command." >&2
      deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
      exit 1
    fi
  done
fi

verify_migration_revision
echo "[deploy] migration preflight: is the live DB in sync with the deploy-worktree manifest?"
migration_check_rc=0
python3 "$MIGRATE" --check "${MIGRATE_DBURL[@]}" || migration_check_rc=$?
verify_migration_revision
if [[ "$migration_check_rc" != 0 ]]; then
  if [[ "$APPLY_MIGRATIONS" == 1 ]]; then
    verify_migration_revision
    echo "[deploy] applying pending migrations (operator opt-in) BEFORE restart"
    # From the first apply attempt onward the database may be partially or
    # fully beyond PREV. Never put arbitrary pre-078 code back on disk: leave
    # the reviewed forward tree in place and keep writers stopped for repair.
    MIGRATION_APPLY_ATTEMPTED=1
    if ! python3 "$MIGRATE" --apply --dialectic-writers-quiesced \
       "${MIGRATE_DBURL[@]}" \
       || ! python3 "$MIGRATE" --check "${MIGRATE_DBURL[@]}"; then
      echo "[deploy] FAILED — migrations did not reach sync; leaving the reviewed forward worktree in place and NOT restarting." >&2
      echo "[deploy] Keep both writers stopped, reconcile the database from this revision, and rerun the schema-only command." >&2
      recover_migration_checkout "migration apply/check failed" || :
      exit 1
    fi
  else
    echo "[deploy] REFUSING: live DB is not in sync with the migration manifest in the code about to deploy." >&2
    echo "[deploy] Rolling the worktree back to ${PREV:0:8} and NOT restarting." >&2
    deploy_lib_restore_reviewed_worktree "$TAG" "$DEPLOY" "$PREV" || :
    echo "[deploy] Stop/unload both writers, then re-run $0 --apply-migrations --dialectic-writers-quiesced." >&2
    exit 1
  fi
fi

verify_migration_revision

if [[ "$APPLY_MIGRATIONS" == 1 ]]; then
  echo "[deploy] migration-only operation complete; NOT compiling or restarting the lease plane"
  exit 0
fi

echo "[deploy] compiling lease_plane (surfaces compile errors before the restart)"
( cd "$DEPLOY/elixir/lease_plane" && mix deps.get && mix compile )

# kickstart when the plist is unchanged since the last deploy restart; full
# RELOAD (bootout + bootstrap) when it changed. kickstart reuses launchd's
# CACHED service definition, so a plist EnvironmentVariables edit silently
# never loads while the health probe below still passes — a green deploy over
# an unchanged process. This service is env-gated on several axes
# (UNITARES_LEASE_IDENTITY_BINDING, UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS,
# UNITARES_LEASE_TRUSTED_ISSUERS, UNITARES_LEASE_ATTESTATION_AUDIENCE, the
# UNITARES_GOVERNED_EFFECT_EXECUTE_* set), so that is a routine operation, not a
# rare one. #1933 moved deploy-mcp.sh onto this helper for the same reason;
# this script was not converted then and a plist edit had to be applied by hand.
# Called in a conditional (set -e would otherwise exit before the rollback).
echo "[deploy] restarting $LABEL (gui domain — it is a LaunchAgent, not a system daemon)"
if ! deploy_lib_restart_service "$TAG" "gui/$UID_NUM" "$LABEL" "$PLIST"; then
  echo "[deploy] FAILED — restart did not complete (see above)." >&2
  echo "[deploy] Leaving the reviewed forward worktree in place: PREV may be below the migration-078 runtime floor." >&2
  echo "[deploy] Keep the service stopped, repair forward, and rerun this command." >&2
  exit 1
fi

echo "[deploy] verifying health"
TOKEN="${LEASE_PLANE_BEARER_TOKEN:-}"

check_lease_plane_health() {
  curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/v1/health 2>/dev/null | grep -q '"ok":true'
}

if deploy_lib_poll 8 3 check_lease_plane_health; then
  echo "[deploy] OK — lease plane healthy, serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
else
  echo "[deploy] FAILED — lease plane did not return healthy. Check ~/Library/Logs/unitares-lease-plane.log" >&2
  exit 1
fi
