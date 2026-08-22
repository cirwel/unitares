#!/usr/bin/env bash
# Functional sandbox for deploy-openai-gov-proxy.sh. No live plist, launchd
# job, port, or operator checkout is touched: git repos and process tools are
# synthetic, while the real deploy script and deploy-lib execute end to end.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/ops/deploy-openai-gov-proxy.sh"
SB="$(mktemp -d)"
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass + 1)); }
bad() { echo "FAIL: $1"; fail=$((fail + 1)); }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
ORIGIN="$SB/origin.git"
REPO="$SB/repo"
DEPLOY="$SB/deploy"
PLIST="$SB/proxy.plist"
FAKE_LAUNCHCTL="$SB/launchctl"
FAKE_LSOF="$SB/lsof"

git init -q --bare "$ORIGIN"
git init -q -b master "$REPO"
(
  cd "$REPO" || exit
  mkdir -p src/unitares_host_adapter
  printf 'old\n' > src/unitares_host_adapter/version.py
  printf '[project]\nname="fixture"\nversion="1"\n' > pyproject.toml
  git add .
  git commit -qm c1
  git remote add origin "$ORIGIN"
  git push -q origin master
  git checkout -qb dev
)
git -C "$REPO" worktree add -q --detach "$DEPLOY" origin/master
mkdir -p "$DEPLOY/.venv/bin"
printf '#!/usr/bin/env bash\nexit 0\n' > "$DEPLOY/.venv/bin/python3"
chmod +x "$DEPLOY/.venv/bin/python3"

cat > "$PLIST" <<EOF
<plist><string>$DEPLOY/.venv/bin/python3</string><string>$DEPLOY/src</string></plist>
EOF

cat > "$FAKE_LAUNCHCTL" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  print) printf 'program = %s/.venv/bin/python3\nPYTHONPATH => %s/src\n' "$HOST_ADAPTER_DEPLOY" "$HOST_ADAPTER_DEPLOY" ;;
  list)  printf '4242\t0\tcom.unitares.openai-governance-proxy\n' ;;
  kickstart) exit 0 ;;
  *) exit 1 ;;
esac
EOF
cat > "$FAKE_LSOF" <<'EOF'
#!/usr/bin/env bash
printf 'COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n'
printf 'Python 4242 test 6u IPv4 0t0 TCP 127.0.0.1:11435 (LISTEN)\n'
EOF
chmod +x "$FAKE_LAUNCHCTL" "$FAKE_LSOF"

# Advance origin/master while the deploy tree remains on c1.
(
  cd "$REPO" || exit
  printf 'new\n' > src/unitares_host_adapter/version.py
  git add .
  git commit -qm c2
  git push -q origin HEAD:master
  # Keep the operator/source checkout on c1 while its fetched origin/master is
  # c2 — the real BEHIND shape that `cirwel update` reports.
  git checkout -q --detach HEAD~1
)
TARGET="$(git -C "$REPO" rev-parse origin/master)"

run_deploy() {
  HOST_ADAPTER_REPO="$REPO" \
  HOST_ADAPTER_DEPLOY="$DEPLOY" \
  OPENAI_GOV_PROXY_PLIST="$PLIST" \
  OPENAI_GOV_PROXY_LAUNCHCTL="$FAKE_LAUNCHCTL" \
  OPENAI_GOV_PROXY_LSOF="$FAKE_LSOF" \
  OPENAI_GOV_PROXY_VERIFY_ATTEMPTS=1 \
  OPENAI_GOV_PROXY_VERIFY_INTERVAL=0 \
  UNITARES_DEPLOY_LOCK="$SB/deploy.lock" \
  bash "$SCRIPT"
}

if output="$(run_deploy 2>&1)" \
  && [[ "$(git -C "$DEPLOY" rev-parse HEAD)" == "$TARGET" ]] \
  && printf '%s' "$output" | grep -q 'pending source: .* c2' \
  && printf '%s' "$output" | grep -q 'OK — OpenAI governance proxy listening'; then
  ok "BEHIND deploy fast-forwards, restarts, and verifies launchd socket ownership"
else
  bad "BEHIND deploy end to end"
  printf '%s\n' "$output" >&2
fi

# A matching plist on disk is insufficient: kickstart reuses launchd's loaded
# definition. Make `print` report the development checkout and ensure the real
# script refuses before claiming success.
cat > "$FAKE_LAUNCHCTL" <<EOF
#!/usr/bin/env bash
case "\$1" in
  print) printf 'program = /usr/bin/python3\nPYTHONPATH => %s/src\n' "$REPO" ;;
  list)  printf '4242\t0\tcom.unitares.openai-governance-proxy\n' ;;
  kickstart) exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$FAKE_LAUNCHCTL"

if refusal="$(run_deploy 2>&1)"; then
  bad "loaded development definition refuses"
elif printf '%s' "$refusal" | grep -q 'plist file is migrated, but launchd is not loaded from'; then
  ok "loaded development definition refuses despite a migrated plist file"
else
  bad "loaded-definition refusal explanation"
  printf '%s\n' "$refusal" >&2
fi

rm -rf "$SB"
echo "test-deploy-openai-gov-proxy: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
