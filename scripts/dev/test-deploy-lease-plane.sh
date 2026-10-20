#!/usr/bin/env bash
# Behavioral migration-gate tests for deploy-lease-plane.sh. All commands that
# could touch a checkout, service, or database are replaced inside a disposable
# fixture; the production script itself is executed unchanged.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOURCE="$ROOT/scripts/ops/deploy-lease-plane.sh"
PLIST_BUDDY_FIXTURE="$ROOT/scripts/dev/plistbuddy-fixture.py"
SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
pass=0
fail=0
ok() { echo "PASS: $1"; pass=$((pass + 1)); }
bad() { echo "FAIL: $1"; fail=$((fail + 1)); }

setup_case() {
  local name="$1"
  CASE="$SB/$name"
  mkdir -p "$CASE/ops" "$CASE/deploy/scripts/dev" \
    "$CASE/deploy/elixir/lease_plane" "$CASE/home/.config/unitares" \
    "$CASE/bin"
  cp "$SOURCE" "$CASE/ops/deploy-lease-plane.sh"
  chmod +x "$CASE/ops/deploy-lease-plane.sh"
  : > "$CASE/log"
  : > "$CASE/state"
  : > "$CASE/verify-state"

  cat > "$CASE/ops/deploy-lib.sh" <<'SH'
deploy_lib_acquire_lock() { :; }
deploy_lib_require_plist_target() { :; }
deploy_lib_ff_worktree() { DEPLOY_LIB_PREV="previous-revision"; }
deploy_lib_verify_expected_revision() {
  printf 'verify\n' >> "$FAKE_LOG"
  count="$(cat "$FAKE_VERIFY_STATE" 2>/dev/null || printf '0')"
  count=$((count + 1))
  printf '%s' "$count" > "$FAKE_VERIFY_STATE"
  [[ "$count" != "${FAKE_VERIFY_FAIL_AT:-0}" ]]
}
deploy_lib_restore_reviewed_worktree() {
  printf 'restore\nrestore-target %s\n' "${3:-}" >> "$FAKE_LOG"
}
deploy_lib_restart_service() {
  printf 'restart\n' >> "$FAKE_LOG"
  printf 'restart-home %s\n' "$HOME" >> "$FAKE_LOG"
  return "${FAKE_RESTART_RC:-0}"
}
deploy_lib_poll() { return 0; }
SH

  cat > "$CASE/bin/python3" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "-" ]]; then
  while IFS= read -r _line; do :; done
  printf 'fixture-token\n'
  exit 0
fi
printf 'python3 %s\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" --apply "*) exit "${FAKE_APPLY_RC:-0}" ;;
  *" --check "*)
    count="$(cat "$FAKE_STATE" 2>/dev/null || printf '0')"
    count=$((count + 1))
    printf '%s' "$count" > "$FAKE_STATE"
    if [[ "$count" == 1 ]]; then
      exit "${FAKE_CHECK_FIRST_RC:-0}"
    fi
    exit "${FAKE_CHECK_NEXT_RC:-0}"
    ;;
esac
exit 0
SH
  chmod +x "$CASE/bin/python3"

  cat > "$CASE/bin/mix" <<'SH'
#!/usr/bin/env bash
printf 'mix %s\n' "$*" >> "$FAKE_LOG"
exit 0
SH
  chmod +x "$CASE/bin/mix"

  cat > "$CASE/bin/git" <<'SH'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$FAKE_LOG"
printf 'deadbeef\n'
SH
  chmod +x "$CASE/bin/git"

  cat > "$CASE/bash-env" <<'SH'
python3() { "$FAKE_BIN/python3" "$@"; }
mix() { "$FAKE_BIN/mix" "$@"; }
git() { "$FAKE_BIN/git" "$@"; }
launchctl() {
  printf 'launchctl %s\n' "$*" >> "$FAKE_LOG"
  if [[ "${1:-}" == print ]]; then
    return "${FAKE_LAUNCHCTL_RC:-1}"
  fi
  return 0
}
export -f python3 mix git launchctl
SH

  cat > "$CASE/home/.config/unitares/secrets.env" <<'SH'
export UNITARES_LEASE_PLANE_DATABASE_URL='postgresql://runtime/lease-plane'
export LEASE_PLANE_BEARER_TOKEN='fixture-token'
SH

  cat > "$CASE/fixture.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.unitares.lease-plane</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$CASE/deploy/elixir/lease_plane/scripts/start.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$CASE/deploy/elixir/lease_plane</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>$CASE/home</string>
    <key>UNITARES_SECRETS_ENV</key><string>$CASE/home/.config/unitares/secrets.env</string>
  </dict>
</dict></plist>
EOF
}

run_case() {
  local apply_flag="${1:-}"
  shift || :
  set +e
  env \
    HOME="$CASE/home" \
    BASH_ENV="$CASE/bash-env" \
    FAKE_BIN="$CASE/bin" \
    FAKE_LOG="$CASE/log" \
    FAKE_STATE="$CASE/state" \
    FAKE_VERIFY_STATE="$CASE/verify-state" \
    UNITARES_REPO="$CASE/repo" \
    UNITARES_LEASE_PLANE_DEPLOY="$CASE/deploy" \
    UNITARES_LEASE_PLANE_PLIST="$CASE/fixture.plist" \
    UNITARES_PLIST_BUDDY="$PLIST_BUDDY_FIXTURE" \
    "$@" \
    bash "$CASE/ops/deploy-lease-plane.sh" $apply_flag \
    >"$CASE/out" 2>&1
  LAST_RC=$?
  set -e
}

setup_case missing-checker
rm "$CASE/deploy/scripts/dev/apply_migrations.py" 2>/dev/null || :
run_case
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log"; then
  ok "lease-plane deploy refuses a missing migration checker before compile/restart"
else
  bad "missing migration checker refusal"
fi

setup_case check-refusal
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" FAKE_CHECK_FIRST_RC=1 UNITARES_DEPLOY_DB_URL=postgresql://runtime/lease-plane
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && grep -q -- '--check --db-url postgresql://runtime/lease-plane' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log"; then
  ok "schema gap restores the checkout and cannot compile/restart"
else
  bad "schema-gap refusal or DB forwarding"
fi

setup_case apply-success
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "--apply-migrations --dialectic-writers-quiesced" FAKE_CHECK_FIRST_RC=1 FAKE_APPLY_RC=0 FAKE_CHECK_NEXT_RC=0
if [[ "$LAST_RC" == 0 ]] && grep -q -- '--apply' "$CASE/log" \
  && grep -q -- '--dialectic-writers-quiesced' "$CASE/log" \
  && grep -q -- '--db-url postgresql://runtime/lease-plane' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log" \
  && grep -q 'migration-only operation complete' "$CASE/out"; then
  ok "explicit migration apply rechecks, then exits without compile/restart"
else
  bad "successful apply path"
fi

setup_case apply-needs-distinct-quiescence-attestation
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case --apply-migrations FAKE_CHECK_FIRST_RC=1
if [[ "$LAST_RC" != 0 ]] && ! grep -qE '^(python3|mix|restart)' "$CASE/log" \
  && grep -q 'separate --dialectic-writers-quiesced attestation' "$CASE/out"; then
  ok "migration consent cannot silently attest writer quiescence"
else
  bad "missing explicit writer-quiescence attestation"
fi

setup_case apply-verifies-writers-unloaded
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "--apply-migrations --dialectic-writers-quiesced" FAKE_LAUNCHCTL_RC=0
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log" \
  && grep -q 'still loaded' "$CASE/out"; then
  ok "quiescence attestation is verified against loaded writer services"
else
  bad "loaded writer verification"
fi

setup_case synced-deploy
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" FAKE_CHECK_FIRST_RC=0
if [[ "$LAST_RC" == 0 ]] && grep -q '^mix compile$' "$CASE/log" \
  && grep -q '^restart$' "$CASE/log"; then
  ok "schema-synced deploy compiles and restarts without applying migrations"
else
  bad "schema-synced deploy path"
fi

# deploy-apply.sh runs every service script with no arguments, so a sweep's
# migration opt-in reaches this script only as the environment variable.
setup_case sweep-optin-synced-deploy
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" UNITARES_DEPLOY_APPLY_MIGRATIONS=1 FAKE_CHECK_FIRST_RC=0
if [[ "$LAST_RC" == 0 ]] && grep -q '^mix compile$' "$CASE/log" \
  && grep -q '^restart$' "$CASE/log" \
  && ! grep -q -- '--apply' "$CASE/log"; then
  ok "a sweep's migration opt-in still deploys code when the schema is in sync"
else
  bad "sweep migration opt-in on a synced schema"
fi

setup_case sweep-optin-gap-refuses
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" UNITARES_DEPLOY_APPLY_MIGRATIONS=1 FAKE_CHECK_FIRST_RC=1
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -q -- '--apply' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log" \
  && grep -q -- '--apply-migrations --dialectic-writers-quiesced' "$CASE/out"; then
  ok "a sweep's migration opt-in never applies a schema gap without the writer attestation"
else
  bad "sweep migration opt-in on a schema gap"
fi

setup_case env-optin-with-attestation-stays-schema-only
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case --dialectic-writers-quiesced UNITARES_DEPLOY_APPLY_MIGRATIONS=1 \
  FAKE_CHECK_FIRST_RC=1 FAKE_APPLY_RC=0 FAKE_CHECK_NEXT_RC=0
if [[ "$LAST_RC" == 0 ]] \
  && grep -q -- '--apply --dialectic-writers-quiesced' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log" \
  && grep -q 'migration-only operation complete' "$CASE/out"; then
  ok "the environment opt-in with the writer attestation stays schema-only"
else
  bad "environment opt-in with the writer attestation"
fi

setup_case restart-failure-keeps-forward-tree
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" FAKE_CHECK_FIRST_RC=0 FAKE_RESTART_RC=1
if [[ "$LAST_RC" != 0 ]] && grep -q '^restart$' "$CASE/log" \
  && ! grep -q '^restore$' "$CASE/log" \
  && ! grep -q ' reset --hard ' "$CASE/log" \
  && grep -q 'migration-078 runtime floor' "$CASE/out"; then
  ok "restart failure never restores arbitrary pre-078 code"
else
  bad "forward-only restart failure"
fi

setup_case apply-failure
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "--apply-migrations --dialectic-writers-quiesced" FAKE_CHECK_FIRST_RC=1 FAKE_APPLY_RC=1
if [[ "$LAST_RC" != 0 ]] \
  && grep -q '^restore-target deadbeef$' "$CASE/log" \
  && ! grep -q '^restore-target previous-revision$' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log"; then
  ok "failed migration apply keeps forward code and cannot compile/restart"
else
  bad "failed apply path"
fi

setup_case post-apply-revision-drift
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "--apply-migrations --dialectic-writers-quiesced" \
  FAKE_CHECK_FIRST_RC=1 FAKE_APPLY_RC=0 FAKE_CHECK_NEXT_RC=0 \
  FAKE_VERIFY_FAIL_AT=4
if [[ "$LAST_RC" != 0 ]] \
  && grep -q '^restore-target deadbeef$' "$CASE/log" \
  && ! grep -q '^restore-target previous-revision$' "$CASE/log" \
  && grep -q 'bootout gui/.*/com.unitares.governance-mcp' "$CASE/log" \
  && grep -q 'bootout gui/.*/com.unitares.lease-plane' "$CASE/log" \
  && ! grep -qE '^(mix|restart)' "$CASE/log"; then
  ok "post-apply revision drift holds the reviewed forward tree and both writers"
else
  bad "post-apply revision-drift recovery"
fi

setup_case db-mismatch
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" UNITARES_DEPLOY_DB_URL=postgresql://different/database
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log"; then
  ok "deploy refuses a checker/runtime database mismatch"
else
  bad "database mismatch refusal"
fi

setup_case ambient-db-mismatch
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
run_case "" UNITARES_LEASE_PLANE_DATABASE_URL=postgresql://ambient/wrong
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log"; then
  ok "ambient shell database cannot redirect the service migration gate"
else
  bad "ambient database parity refusal"
fi

setup_case ambient-secrets-mismatch
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
: > "$CASE/alternate-secrets.env"
run_case "" UNITARES_SECRETS_ENV="$CASE/alternate-secrets.env"
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log"; then
  ok "ambient shell secrets path cannot redirect the service migration gate"
else
  bad "ambient secrets parity refusal"
fi

setup_case plist-secrets-db
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
cat > "$CASE/plist-secrets.env" <<'SH'
export UNITARES_LEASE_PLANE_DATABASE_URL='postgresql://plist/secrets-db'
export LEASE_PLANE_BEARER_TOKEN='fixture-token'
SH
"$PLIST_BUDDY_FIXTURE" -c \
  "Set :EnvironmentVariables:UNITARES_SECRETS_ENV $CASE/plist-secrets.env" \
  "$CASE/fixture.plist"
"$PLIST_BUDDY_FIXTURE" -c \
  'Add :EnvironmentVariables:UNITARES_LEASE_PLANE_DATABASE_URL string postgresql://plist/pre-source-db' \
  "$CASE/fixture.plist"
run_case "" UNITARES_DEPLOY_DB_URL=postgresql://plist/secrets-db
if [[ "$LAST_RC" == 0 ]] \
  && grep -q -- '--check --db-url postgresql://plist/secrets-db' "$CASE/log" \
  && grep -q '^restart$' "$CASE/log"; then
  ok "plist secrets override wins after the direct plist DB, matching start.sh"
else
  bad "plist secrets database parity"
fi

setup_case plist-direct-db
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
cat > "$CASE/home/.config/unitares/secrets.env" <<'SH'
export LEASE_PLANE_BEARER_TOKEN='fixture-token'
SH
"$PLIST_BUDDY_FIXTURE" -c \
  'Add :EnvironmentVariables:UNITARES_LEASE_PLANE_DATABASE_URL string postgresql://plist/direct-db' \
  "$CASE/fixture.plist"
run_case "" UNITARES_DEPLOY_DB_URL=postgresql://plist/direct-db
if [[ "$LAST_RC" == 0 ]] \
  && grep -q -- '--check --db-url postgresql://plist/direct-db' "$CASE/log" \
  && grep -q '^restart$' "$CASE/log"; then
  ok "plist direct database selects the migration preflight target"
else
  bad "plist direct database parity"
fi

setup_case plist-home
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
mkdir -p "$CASE/runtime-home/.config/unitares"
cat > "$CASE/runtime-home/.config/unitares/secrets.env" <<'SH'
export UNITARES_LEASE_PLANE_DATABASE_URL="postgresql://plist/home-db?runtime_home=$HOME"
export LEASE_PLANE_BEARER_TOKEN='fixture-token'
SH
"$PLIST_BUDDY_FIXTURE" -c \
  "Set :EnvironmentVariables:HOME $CASE/runtime-home" \
  "$CASE/fixture.plist"
"$PLIST_BUDDY_FIXTURE" -c \
  "Set :EnvironmentVariables:UNITARES_SECRETS_ENV $CASE/runtime-home/.config/unitares/secrets.env" \
  "$CASE/fixture.plist"
run_case "" \
  "UNITARES_DEPLOY_DB_URL=postgresql://plist/home-db?runtime_home=$CASE/runtime-home"
if [[ "$LAST_RC" == 0 ]] \
  && grep -F -q -- "--check --db-url postgresql://plist/home-db?runtime_home=$CASE/runtime-home" "$CASE/log" \
  && grep -F -q -- "restart-home $CASE/home" "$CASE/log" \
  && grep -q '^restart$' "$CASE/log"; then
  ok "plist HOME controls secrets expansion without moving deploy sidecar state"
else
  bad "plist HOME database parity"
fi

setup_case plist-missing-home
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
"$PLIST_BUDDY_FIXTURE" -c 'Delete :EnvironmentVariables:HOME' \
  "$CASE/fixture.plist"
run_case ""
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log" \
  && grep -q 'must provide an absolute EnvironmentVariables/HOME' "$CASE/out"; then
  ok "missing plist HOME fails closed before database preflight"
else
  bad "missing plist HOME refusal"
fi

setup_case plist-empty-direct-db
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
"$PLIST_BUDDY_FIXTURE" -c \
  'Add :EnvironmentVariables:UNITARES_LEASE_PLANE_DATABASE_URL string ""' \
  "$CASE/fixture.plist"
run_case ""
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log" \
  && grep -q 'present but empty' "$CASE/out"; then
  ok "empty plist database fails closed instead of substituting a default"
else
  bad "empty plist database refusal"
fi

setup_case unexported-service-db
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
cat > "$CASE/home/.config/unitares/secrets.env" <<'SH'
UNITARES_LEASE_PLANE_DATABASE_URL='postgresql://runtime/lease-plane'
export LEASE_PLANE_BEARER_TOKEN='fixture-token'
SH
run_case ""
if [[ "$LAST_RC" != 0 ]] && grep -q '^restore$' "$CASE/log" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log"; then
  ok "unexported service database is refused before migration or restart"
else
  bad "unexported database refusal"
fi

setup_case no-persisted-source
: > "$CASE/deploy/scripts/dev/apply_migrations.py"
"$PLIST_BUDDY_FIXTURE" -c 'Delete :EnvironmentVariables:UNITARES_SECRETS_ENV' "$CASE/fixture.plist"
run_case ""
if [[ "$LAST_RC" != 0 ]] && grep -q 'persisted UNITARES_SECRETS_ENV' "$CASE/out" \
  && ! grep -qE '^(python3|mix|restart)' "$CASE/log"; then
  ok "absent persisted source refuses instead of discovering a home file"
else
  bad "missing persisted secrets source refusal"
fi

if [[ "$fail" != 0 ]]; then
  echo "test-deploy-lease-plane: $pass passed, $fail failed" >&2
  exit 1
fi
echo "test-deploy-lease-plane: $pass passed"
