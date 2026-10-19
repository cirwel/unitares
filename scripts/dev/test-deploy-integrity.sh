#!/usr/bin/env bash
# Isolated deployment race and result tests. No live service/DB commands run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
LIB="$ROOT/scripts/ops/deploy-lib.sh"
export UNITARES_PLIST_BUDDY="$ROOT/scripts/dev/plistbuddy-fixture.py"
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export UNITARES_DEPLOY_LOCK="$SB/deploy.lock"
PASS=0
ok() { PASS=$((PASS + 1)); printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n%s\n' "$1" "${out:-}"; exit 1; }
REPO="$SB/source"; DEP="$SB/deploy"; OTHER="$SB/other"
git init -q --bare "$SB/origin.git"
git init -q -b master "$REPO"
mkdir -p "$REPO/scripts/dev" "$REPO/db/postgres"
printf '%s\n' '-- included schema' > "$REPO/db/postgres/knowledge_schema.sql"
printf '# fixture\n' > "$REPO/scripts/dev/apply_migrations.py"
git -C "$REPO" add .; git -C "$REPO" -c commit.gpgsign=false commit -qm base
OLD=$(git -C "$REPO" rev-parse HEAD)
git -C "$REPO" remote add origin "$SB/origin.git"
git -C "$REPO" push -q origin master
git -C "$REPO" worktree add -q --detach "$DEP" HEAD
printf 'new\n' > "$REPO/change"
git -C "$REPO" add .; git -C "$REPO" -c commit.gpgsign=false commit -qm new
NEW=$(git -C "$REPO" rev-parse HEAD)
git -C "$REPO" push -q origin master
export UNITARES_DEPLOY_EXPECTED_REPO="$REPO" UNITARES_DEPLOY_EXPECTED_REVISION="$OLD"
if out=$(bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP" 2>&1); then
  fail 'a newer remote bypassed the inspected SHA'
fi
[ "$(git -C "$DEP" rev-parse HEAD)" = "$OLD" ] || fail 'mismatch moved checkout'
[[ "$out" == *'revision changed since migration review'* ]] || fail 'mismatch not explained'
ok 'new remote revision is refused before checkout moves'
export UNITARES_DEPLOY_EXPECTED_REVISION="$NEW"
# Dirty inputs must not move disk ahead of a database that has not migrated.
for path in scripts/dev/apply_migrations.py db/postgres/knowledge_schema.sql; do
  printf '\n-- modified\n' >> "$DEP/$path"
  if out=$(bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP" 2>&1); then fail "dirty $path accepted before merge"; fi
  [ "$(git -C "$DEP" rev-parse HEAD)" = "$OLD" ] || fail "dirty $path moved checkout"
  git -C "$DEP" restore "$path"
done
mkdir -p "$DEP/db/postgres/migrations"
printf 'DELETE FROM records;\n' > "$DEP/db/postgres/migrations/999_unreviewed.sql"
if out=$(bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP" 2>&1); then fail 'untracked migration accepted before merge'; fi
[ "$(git -C "$DEP" rev-parse HEAD)" = "$OLD" ] || fail 'untracked migration moved checkout'
rm "$DEP/db/postgres/migrations/999_unreviewed.sql"
ok 'dirty planner, included SQL, and untracked migrations refuse before checkout moves'
printf '%s\n' '999_unreviewed.sql' >> "$REPO/.git/info/exclude"
printf 'DELETE FROM records;\n' > "$DEP/db/postgres/migrations/999_unreviewed.sql"
git -C "$DEP" check-ignore -q db/postgres/migrations/999_unreviewed.sql || fail 'ignored SQL fixture is not ignored'
if out=$(bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP" 2>&1); then fail 'ignored migration bypassed source check'; fi
[ "$(git -C "$DEP" rev-parse HEAD)" = "$OLD" ] || fail 'ignored migration moved checkout'
rm "$DEP/db/postgres/migrations/999_unreviewed.sql"
ok 'ignored SQL still blocks deployment before checkout moves'
# An edit arriving during fast-forward must be retained during safe rollback.
export TEST_DEPLOY="$DEP"
set +e
(
  git() {
    command git "$@"; status=$?
    if [[ "${3:-}" == merge && "$status" = 0 ]]; then
      printf '# concurrent edit\n' >> "$TEST_DEPLOY/scripts/dev/apply_migrations.py"
    fi
    return "$status"
  }
  export -f git
  bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP"
) > "$SB/ff-output" 2>&1
rc=$?
set -e
out=$(cat "$SB/ff-output")
[ "$rc" != 0 ] || fail 'edit arriving during merge accepted'
[ "$(git -C "$DEP" rev-parse HEAD)" = "$OLD" ] || fail 'post-merge refusal left disk advanced'
grep -q 'concurrent edit' "$DEP/scripts/dev/apply_migrations.py" || fail 'rollback lost concurrent edit'
git -C "$DEP" restore scripts/dev/apply_migrations.py
ok 'post-merge refusal restores previous revision and preserves concurrent edits'
bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP" >/dev/null
[ "$(git -C "$DEP" rev-parse HEAD)" = "$NEW" ] || fail 'matching revision did not advance'
ok 'matching reviewed revision deploys'
printf 'conflicting local edit\n' >> "$DEP/change"
if out=$(bash -c '. "$1"; deploy_lib_restore_reviewed_worktree t "$2" "$3"' _ "$LIB" "$DEP" "$OLD" 2>&1); then fail 'rollback overwrote a conflicting local edit'; fi
[ "$(git -C "$DEP" rev-parse HEAD)" = "$NEW" ] || fail 'conflicting rollback moved HEAD'
grep -q 'conflicting local edit' "$DEP/change" || fail 'conflicting edit was lost'
[[ "$out" == *'RECOVERY REQUIRED'* ]] || fail 'conflicting rollback did not explain recovery'
git -C "$DEP" restore change
ok 'conflicting recovery preserves edits and explicitly requires manual reconciliation'
printf '# modified\n' >> "$DEP/scripts/dev/apply_migrations.py"
if bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$DEP" 2>/dev/null; then fail 'modified migration planner accepted'; fi
git -C "$DEP" restore scripts/dev/apply_migrations.py
mkdir -p "$DEP/db/postgres/migrations"
printf 'DELETE FROM records;\n' > "$DEP/db/postgres/migrations/999_unreviewed.sql"
if bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$DEP" 2>/dev/null; then fail 'untracked migration accepted'; fi
rm "$DEP/db/postgres/migrations/999_unreviewed.sql"
ok 'modified planner and untracked SQL cannot bypass a matching HEAD'
# Pin from a different worktree, spelled through a symlink, has the same scope.
ln -s "$DEP" "$SB/pinned-link"
UNITARES_DEPLOY_EXPECTED_REPO="$SB/pinned-link" bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$REPO"
ok 'revision scope follows shared Git storage through symlinks'
git init -q "$OTHER"; printf 'other\n' > "$OTHER/file"
git -C "$OTHER" add .; git -C "$OTHER" -c commit.gpgsign=false commit -qm other
bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$OTHER"
ok 'unrelated repository is not compared with the UNITARES SHA'
for bad in 'short-sha' ''; do
  if UNITARES_DEPLOY_EXPECTED_REVISION="$bad" bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$REPO" 2>/dev/null; then fail 'invalid pin accepted'; fi
done
if UNITARES_DEPLOY_EXPECTED_REPO="$SB/absent" bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$REPO" 2>/dev/null; then fail 'unreadable scope accepted'; fi
mkdir -p "$SB/not-a-repo"
if UNITARES_DEPLOY_EXPECTED_REPO="$SB/not-a-repo" bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$REPO" 2>/dev/null; then fail 'non-Git scope silently disabled pin'; fi
if bash -c '. "$1"; deploy_lib_verify_expected_revision t "$2" HEAD' _ "$LIB" "$SB/not-a-repo" 2>/dev/null; then fail 'non-Git target silently disabled pin'; fi
ok 'malformed, unreadable, and non-Git pins fail closed'
# Real deploy-mcp path: the schema check changes HEAD between ff and --apply.
# Its exported command fakes also prevent accidental host control on failure.
printf 'unreviewed\n' >> "$REPO/change"
git -C "$REPO" add .; git -C "$REPO" -c commit.gpgsign=false commit -qm unreviewed
UNREVIEWED=$(git -C "$REPO" rev-parse HEAD)
export TEST_DEPLOY="$DEP" TEST_NEW_SHA="$UNREVIEWED" TEST_TRACE="$SB/trace"
export TEST_PLIST="$SB/service.plist"
cat > "$TEST_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>ProgramArguments</key><array><string>$DEP/src/mcp_server.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>DB_POSTGRES_URL</key><string>postgresql://fixture/runtime</string>
  </dict>
</dict></plist>
EOF
mkdir -p "$SB/mcp"
# Substitute only the host-specific plist path; execute the real deploy flow.
sed 's|^PLIST=.*|PLIST="$TEST_PLIST"|' "$ROOT/scripts/ops/deploy-mcp.sh" > "$SB/mcp/deploy-mcp.sh"
cp "$LIB" "$SB/mcp/deploy-lib.sh"
cp "$ROOT/scripts/ops/apply_plist_env_overlay.py" "$SB/mcp/"
: > "$SB/reviewed.env"
export UNITARES_MCP_ENV_OVERLAY="$SB/reviewed.env"
export UNITARES_MCP_ENV_OVERLAY_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
for scenario in '0 0' '0 1' '1 0' '1 1'; do
read -r apply_status check_status <<< "$scenario"
export TEST_CHECK_STATUS="$check_status"
: > "$TEST_TRACE"
set +e
(
  export UNITARES_REPO="$REPO" UNITARES_MCP_DEPLOY="$DEP"
  export UNITARES_DEPLOY_APPLY_MIGRATIONS="$apply_status"
  python3() {
    printf 'python3 %s\n' "$*" >> "$TEST_TRACE"
    case " $* " in
      *' --validate-overlay '*) command python3 "$@"; return $? ;;
      *' --print-db-version '*) printf '76\n'; return 0 ;;
      *' --check '*)
        git -C "$TEST_DEPLOY" merge --ff-only -q "$TEST_NEW_SHA"
        printf 'preserve me\n' > "$TEST_DEPLOY/concurrent-note"
        return "$TEST_CHECK_STATUS" ;;
      *' --apply '*) return 0 ;;
      *) return 1 ;;
    esac
  }
  launchctl() { echo 'unexpected launchctl' >> "$TEST_TRACE"; return 1; }
  curl() { echo 'unexpected curl' >> "$TEST_TRACE"; return 1; }
  export -f python3 launchctl curl
  bash "$SB/mcp/deploy-mcp.sh"
) > "$SB/mcp-output" 2>&1
rc=$?
set -e
out=$(cat "$SB/mcp-output")
[ "$rc" != 0 ] || fail 'moved HEAD accepted before applying migration'
if grep -qE -- '--apply|unexpected launchctl|unexpected curl' "$TEST_TRACE"; then fail 'migration/restart ran after HEAD changed'; fi
[[ "$out" == *'revision changed since migration review'* ]] || fail 'pre-apply drift not explained'
[ "$(git -C "$DEP" rev-parse HEAD)" = "$NEW" ] || fail 'schema-check refusal left unreviewed revision on disk'
[ "$(cat "$DEP/concurrent-note")" = 'preserve me' ] || fail 'schema-check rollback lost local edits'
rm "$DEP/concurrent-note"
ok "HEAD drift during schema check (exit $check_status, apply $apply_status) restores previous code without applying or restarting"
done
unset UNITARES_DEPLOY_EXPECTED_REPO UNITARES_DEPLOY_EXPECTED_REVISION
AP="$SB/apply"; mkdir -p "$AP"
cp "$ROOT/scripts/ops/deploy-apply.sh" "$AP/"
cat > "$AP/deploy-status.sh" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$TEST_STATUS" | python3 -c '
import json, os, sys
rows=json.load(sys.stdin)
mapping={"governance-mcp":"deploy-mcp.sh","gateway-mcp":"deploy-gateway.sh","discord-bridge":"deploy-bridge.sh","lease-plane":"deploy-lease-plane.sh","dispatch-beam":"deploy-dispatch-beam.sh","dispatch-beam-codex":"deploy-dispatch-beam.sh"}
for row in rows:
    if row["name"] in mapping: row["deploy_script"]=os.path.join(sys.argv[1],mapping[row["name"]])
print(json.dumps(rows))
' "$(dirname "$0")"
EOS
chmod +x "$AP/deploy-status.sh"
out=$(bash "$AP/deploy-apply.sh" --capabilities)
[[ "$out" == *'expected-revision-v1'* ]] || fail 'handshake missing'
[[ " $out " == *' requested-service-v1 '* ]] || fail 'requested-service handshake missing'
ok 'capability handshake runs without status or deploy'
for pair in 'unknown STALE(1)' 'governance-mcp BEHIND(1)' 'governance-mcp DOWN'; do
  read -r name verdict <<< "$pair"
  export TEST_STATUS="[{\"name\":\"$name\",\"verdict\":\"$verdict\",\"checkout\":\"/fixture/unused\",\"pickup\":\"restart\"}]"
  for mode in '' '--dry-run'; do
    set +e
    out=$(bash "$AP/deploy-apply.sh" --no-fetch $mode 2>&1); rc=$?
    set -e
    [ "$rc" = 3 ] || fail "incomplete $pair $mode returned $rc, expected 3"
  done
  ok "$pair returns incomplete for normal and dry-run updates"
done
# DOWN is still incomplete when a deploy script exists; it is never restarted.
printf '#!/usr/bin/env bash\necho unexpected-deploy\n' > "$AP/deploy-mcp.sh"
chmod +x "$AP/deploy-mcp.sh"
set +e
out=$(bash "$AP/deploy-apply.sh" --no-fetch 2>&1); rc=$?
set -e
[ "$rc" = 3 ] && [[ "$out" != *'unexpected-deploy'* ]] || fail 'DOWN service was restarted'
ok 'DOWN service stays stopped even with deployment automation'
# A service earlier in the table must not wake a later DOWN lease plane.
# Exercise the real shared nudge helper; fake all host control commands.
cp "$LIB" "$ROOT/scripts/ops/nudge-lease-plane.sh" "$AP/"
cat > "$AP/deploy-mcp.sh" <<'EOS'
#!/usr/bin/env bash
. "$(dirname "$0")/deploy-lib.sh"
DEPLOY_LIB_FRESH=1
deploy_lib_nudge_lease_plane t fixture /fixture/shared
EOS
export TEST_STATUS='[{"name":"governance-mcp","verdict":"STALE(1)","checkout":"/fixture/shared"},{"name":"lease-plane","verdict":"DOWN","checkout":"/fixture/shared"}]'
set +e
out=$(
  launchctl() { echo "launchctl $*" >> "$TEST_TRACE"; return 0; }
  curl() { printf 200; }
  sleep() { :; }
  export -f launchctl curl sleep
  bash "$AP/deploy-apply.sh" --no-fetch 2>&1
); rc=$?
set -e
[ "$rc" = 3 ] && [[ "$out" == *'leaving it stopped'* ]] || fail 'sweep did not suppress indirect DOWN restart'
if grep -q launchctl "$TEST_TRACE"; then fail 'indirect nudge reached launchctl for DOWN service'; fi
ok 'DOWN snapshot prevents indirect lease-plane restarts regardless of table order'
# The same nudge still repairs a running plane after a shared-tree change.
export TEST_STATUS='[{"name":"governance-mcp","verdict":"STALE(1)","checkout":"/fixture/shared"},{"name":"lease-plane","verdict":"CURRENT","checkout":"/fixture/shared"}]'
out=$(
  launchctl() { echo "launchctl $*" >> "$TEST_TRACE"; return 0; }
  curl() { printf 200; }
  sleep() { :; }
  export -f launchctl curl sleep
  bash "$AP/deploy-apply.sh" --no-fetch 2>&1
)
grep -q 'launchctl kickstart' "$TEST_TRACE" || fail 'running plane no longer receives its recovery nudge'
ok 'running lease plane still gets its shared-worktree recovery nudge'
# Paired bot rows use one script that restarts both; hold that whole script
# when either instance is DOWN instead of waking its stopped sibling.
printf '#!/usr/bin/env bash\necho unexpected-paired-deploy\n' > "$AP/deploy-dispatch-beam.sh"
chmod +x "$AP/deploy-dispatch-beam.sh"
export TEST_STATUS='[{"name":"dispatch-beam","verdict":"STALE(1)","checkout":"/fixture/dispatch"},{"name":"dispatch-beam-codex","verdict":"DOWN","checkout":"/fixture/dispatch"}]'
set +e
out=$(bash "$AP/deploy-apply.sh" --no-fetch 2>&1); rc=$?
set -e
[ "$rc" = 3 ] && [[ "$out" == *'also controls a DOWN service'* ]] || fail 'paired deploy was not held'
[[ "$out" != *'unexpected-paired-deploy'* ]] || fail 'paired script ran despite DOWN sibling'
ok 'a paired deploy cannot restart a DOWN sibling'
export TEST_STATUS='[{"name":"governance-mcp","verdict":"CURRENT"}]'
bash "$AP/deploy-apply.sh" --no-fetch >/dev/null
ok 'fully current fleet still returns success'
export TEST_STATUS='[{"name":"governance-mcp","verdict":"STALE(1)","checkout":"/fixture/shared"},{"name":"unknown","verdict":"STALE(1)","checkout":"/fixture/other"}]'
printf '#!/usr/bin/env bash\nexit 1\n' > "$AP/deploy-mcp.sh"
set +e
out=$(bash "$AP/deploy-apply.sh" --no-fetch 2>&1); rc=$?
set -e
[ "$rc" = 1 ] || fail 'incomplete result hid a deployment failure'
ok 'actual deployment failure takes precedence over incomplete result'

# Explicit config-only requests enter the same dispatcher as code drift. All
# deploy scripts here are stubs; only the revision verifier uses fixture Git.
export TEST_APPLY_TRACE="$SB/request-trace" TEST_APPLY_LIB="$LIB"
export TEST_APPLY_REPO="$REPO" TEST_FAIL_SCRIPT="" TEST_VERIFY_PIN=0
for script in deploy-mcp.sh deploy-gateway.sh deploy-bridge.sh deploy-dispatch-beam.sh; do
  cat > "$AP/$script" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
if [ "$TEST_VERIFY_PIN" = 1 ]; then
  . "$TEST_APPLY_LIB"
  deploy_lib_verify_expected_revision fixture "$TEST_APPLY_REPO" HEAD
fi
name="$(basename "$0")"
printf '%s\n' "$name" >> "$TEST_APPLY_TRACE"
[ "$name" != "$TEST_FAIL_SCRIPT" ]
EOS
  chmod +x "$AP/$script"
done
assert_apply_exit() {
  expected="$1"; shift
  rc=0
  out=$(bash "$AP/deploy-apply.sh" --no-fetch "$@" 2>&1) || rc=$?
  [ "$rc" = "$expected" ] || fail "request $* returned $rc, expected $expected"
}
export TEST_STATUS='[{"name":"governance-mcp","verdict":"CURRENT","checkout":"/fixture/shared"}]'
: > "$TEST_APPLY_TRACE"
assert_apply_exit 0
[ ! -s "$TEST_APPLY_TRACE" ] || fail 'CURRENT service deployed without a request'
assert_apply_exit 0 --request-service=governance-mcp --request-service=governance-mcp
[ "$(cat "$TEST_APPLY_TRACE")" = deploy-mcp.sh ] || fail 'duplicate CURRENT request did not dispatch exactly once'
ok 'CURRENT services remain opt-in and repeated requests dispatch once'

export TEST_STATUS='[{"name":"gateway-mcp","verdict":"CURRENT","checkout":"/fixture/shared"},{"name":"governance-mcp","verdict":"CURRENT","checkout":"/fixture/shared"},{"name":"discord-bridge","verdict":"STALE(1)","checkout":"/fixture/bridge"}]'
: > "$TEST_APPLY_TRACE"
assert_apply_exit 0 --request-service=governance-mcp --request-service=gateway-mcp --request-service=governance-mcp
[ "$(cat "$TEST_APPLY_TRACE")" = $'deploy-gateway.sh\ndeploy-mcp.sh\ndeploy-bridge.sh' ] || fail 'requests changed status-table order or omitted drift'
ok 'multiple requests preserve status-table order alongside ordinary drift'
: > "$TEST_APPLY_TRACE"
assert_apply_exit 0 --dry-run --request-service=governance-mcp --request-service=gateway-mcp
[ ! -s "$TEST_APPLY_TRACE" ] && [[ "$out" == *'DRY   would deploy governance-mcp (CURRENT)'* ]] || fail 'dry-run executed or omitted requested CURRENT service'
ok 'dry-run reports requested CURRENT services without dispatch'

for verdict in 'STALE(1)' 'BEHIND(1)'; do
  export TEST_STATUS="[{\"name\":\"governance-mcp\",\"verdict\":\"$verdict\",\"checkout\":\"/fixture/shared\"}]"
  : > "$TEST_APPLY_TRACE"
  assert_apply_exit 0 --request-service=governance-mcp --request-service=governance-mcp
  [ "$(cat "$TEST_APPLY_TRACE")" = deploy-mcp.sh ] || fail "requested $verdict dispatched more than once"
done
ok 'requests do not duplicate STALE or BEHIND deployments'

export TEST_STATUS='[{"name":"governance-mcp","verdict":"STALE(1)","checkout":"/fixture/shared"}]'
for request in missing-service '' 'bad/name' $'governance-mcp\nmissing-service'; do
  : > "$TEST_APPLY_TRACE"
  assert_apply_exit 1 "--request-service=$request"
  [ ! -s "$TEST_APPLY_TRACE" ] || fail 'invalid request allowed partial deployment'
done
export TEST_STATUS='[{"name":"governance-mcp","verdict":"CURRENT"},{"name":"governance-mcp","verdict":"DOWN"}]'
assert_apply_exit 1 --request-service=governance-mcp
[ ! -s "$TEST_APPLY_TRACE" ] || fail 'ambiguous requested status allowed deployment'
ok 'missing, malformed and ambiguous requests fail before any deployment'

for verdict in DOWN GHOST 'HOT-RELOAD(?)' UNKNOWN; do
  export TEST_STATUS="[{\"name\":\"governance-mcp\",\"verdict\":\"$verdict\",\"checkout\":\"/fixture/shared\"}]"
  for mode in '' '--dry-run'; do
    assert_apply_exit 3 --request-service=governance-mcp $mode
    [ ! -s "$TEST_APPLY_TRACE" ] || fail "request restarted $verdict service"
  done
done
ok 'requests leave DOWN and unconfirmed services pending in real and dry-run modes'

export TEST_STATUS='[{"name":"mystery","verdict":"CURRENT","checkout":"/fixture/other"}]'
assert_apply_exit 3 --request-service=mystery
export TEST_STATUS='[{"name":"lease-plane","verdict":"CURRENT","checkout":"/fixture/shared"}]'
assert_apply_exit 3 --request-service=lease-plane
export TEST_STATUS='[{"name":"gateway-mcp","verdict":"CURRENT","checkout":"/fixture/shared"}]'
chmod -x "$AP/deploy-gateway.sh"
assert_apply_exit 3 --request-service=gateway-mcp
chmod +x "$AP/deploy-gateway.sh"
[ ! -s "$TEST_APPLY_TRACE" ] || fail 'unavailable deployment automation executed'
ok 'requested services without registered, present or executable automation remain pending'

export TEST_STATUS='[{"name":"dispatch-beam","verdict":"CURRENT","checkout":"/fixture/dispatch"},{"name":"dispatch-beam-codex","verdict":"DOWN","checkout":"/fixture/dispatch"}]'
assert_apply_exit 3 --request-service=dispatch-beam
[ ! -s "$TEST_APPLY_TRACE" ] && [[ "$out" == *'also controls a DOWN service'* ]] || fail 'request restarted a DOWN sibling'
ok 'requested CURRENT paired service respects its DOWN sibling'

export TEST_STATUS='[{"name":"governance-mcp","verdict":"STALE(1)","checkout":"/fixture/shared"},{"name":"gateway-mcp","verdict":"CURRENT","checkout":"/fixture/shared"},{"name":"discord-bridge","verdict":"CURRENT","checkout":"/fixture/bridge"}]'
export TEST_FAIL_SCRIPT=deploy-mcp.sh
assert_apply_exit 1 --request-service=gateway-mcp --request-service=discord-bridge
[ "$(cat "$TEST_APPLY_TRACE")" = $'deploy-mcp.sh\ndeploy-bridge.sh' ] && [[ "$out" == *'HOLD  gateway-mcp'* ]] || fail 'requested service bypassed shared-checkout hold'
ok 'failed deployment holds requested siblings while unrelated requested services continue'

export TEST_STATUS='[{"name":"governance-mcp","verdict":"CURRENT","checkout":"/fixture/shared"}]'
export TEST_FAIL_SCRIPT="" TEST_VERIFY_PIN=1
export UNITARES_DEPLOY_EXPECTED_REPO="$REPO" UNITARES_DEPLOY_EXPECTED_REVISION="$NEW"
: > "$TEST_APPLY_TRACE"
assert_apply_exit 1 --request-service=governance-mcp
[ ! -s "$TEST_APPLY_TRACE" ] && [[ "$out" == *'revision changed since migration review'* ]] || fail 'CURRENT request bypassed reviewed revision'
export UNITARES_DEPLOY_EXPECTED_REVISION="$UNREVIEWED"
assert_apply_exit 0 --request-service=governance-mcp
[ "$(cat "$TEST_APPLY_TRACE")" = deploy-mcp.sh ] || fail 'matching reviewed CURRENT request did not deploy'
ok 'requested CURRENT deployment inherits and enforces the reviewed revision pin'
printf '%s passed, 0 failed\n' "$PASS"
