#!/usr/bin/env bash
# Isolated deployment race and result tests. No live service/DB commands run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
LIB="$ROOT/scripts/ops/deploy-lib.sh"
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export UNITARES_DEPLOY_LOCK="$SB/deploy.lock"
PASS=0
ok() { PASS=$((PASS + 1)); printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n%s\n' "$1" "${out:-}"; exit 1; }
REPO="$SB/source"; DEP="$SB/deploy"; OTHER="$SB/other"
git init -q --bare "$SB/origin.git"
git init -q -b master "$REPO"
mkdir -p "$REPO/scripts/dev"
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
bash -c '. "$1"; deploy_lib_ff_worktree t "$2" "$3"' _ "$LIB" "$REPO" "$DEP" >/dev/null
[ "$(git -C "$DEP" rev-parse HEAD)" = "$NEW" ] || fail 'matching revision did not advance'
ok 'matching reviewed revision deploys'
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
ok 'malformed and unreadable pins fail closed'
# Real deploy-mcp path: the schema check changes HEAD between ff and --apply.
# Its exported command fakes also prevent accidental host control on failure.
printf 'unreviewed\n' >> "$REPO/change"
git -C "$REPO" add .; git -C "$REPO" -c commit.gpgsign=false commit -qm unreviewed
UNREVIEWED=$(git -C "$REPO" rev-parse HEAD)
export TEST_DEPLOY="$DEP" TEST_NEW_SHA="$UNREVIEWED" TEST_TRACE="$SB/trace"
export TEST_PLIST="$SB/service.plist"
printf '<string>%s/src/mcp_server.py</string>\n' "$DEP" > "$TEST_PLIST"
mkdir -p "$SB/mcp"
# Substitute only the host-specific plist path; execute the real deploy flow.
sed 's|^PLIST=.*|PLIST="$TEST_PLIST"|' "$ROOT/scripts/ops/deploy-mcp.sh" > "$SB/mcp/deploy-mcp.sh"
cp "$LIB" "$SB/mcp/deploy-lib.sh"
set +e
(
  export UNITARES_REPO="$REPO" UNITARES_MCP_DEPLOY="$DEP"
  export UNITARES_DEPLOY_APPLY_MIGRATIONS=1
  python3() {
    printf 'python3 %s\n' "$*" >> "$TEST_TRACE"
    case " $* " in
      *' --check '*) git -C "$TEST_DEPLOY" merge --ff-only -q "$TEST_NEW_SHA"; return 1 ;;
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
ok 'HEAD change during schema check blocks migration application and restart'
unset UNITARES_DEPLOY_EXPECTED_REPO UNITARES_DEPLOY_EXPECTED_REVISION
AP="$SB/apply"; mkdir -p "$AP"
cp "$ROOT/scripts/ops/deploy-apply.sh" "$AP/"
cat > "$AP/deploy-status.sh" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$TEST_STATUS"
EOS
chmod +x "$AP/deploy-status.sh"
out=$(bash "$AP/deploy-apply.sh" --capabilities)
[[ "$out" == *'expected-revision-v1'* ]] || fail 'handshake missing'
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
printf '%s passed, 0 failed\n' "$PASS"
