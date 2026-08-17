#!/usr/bin/env bash
# test-deploy-gov-plugin.sh — sandbox tests for scripts/ops/deploy-gov-plugin.sh.
#
# The refusal guards ARE this script's substance: its target is a checkout the
# operator works in, and the failure mode it exists to prevent is an unattended
# `cirwel update` discarding in-flight plugin work to make a hook current. So
# the cases below are mostly "does it refuse, and does it leave the checkout
# where it found it" rather than "does the happy path work".
#
# Everything runs against throwaway fixtures under mktemp: a bare origin and a
# clone standing in for the plugin checkout. No network, no real checkout, and
# GOV_PLUGIN_CODEX_CACHE is pointed at a temp dir so the operator's real cache
# is never read. Invoked by scripts/dev/check-deploy-lib.sh (CI smoke job).
set -u
SCRIPT="$(cd "$(dirname "$0")/../ops" && pwd)/deploy-gov-plugin.sh"
SB="$(mktemp -d)"
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GOV_PLUGIN_CODEX_CACHE="$SB/no-such-cache"

ORIGIN="$SB/origin.git"
git init -q --bare "$ORIGIN"

# Build a fresh fixture: origin at c2, clone left at c1 (behind=1) on master.
# Each case gets its own clone so state cannot leak between them.
fixture() {
  local repo="$1"
  rm -rf "$repo"
  git clone -q "$ORIGIN" "$repo"
  git -C "$repo" reset -q --hard HEAD~1
}

seed="$SB/seed"
git init -q -b master "$seed"
( cd "$seed" && echo a > f && git add f && git commit -qm c1 \
    && git remote add origin "$ORIGIN" && git push -q origin master \
    && echo b > f && git commit -qam c2 && git push -q origin master )
TARGET="$(git -C "$seed" rev-parse --short HEAD)"
PREV="$(git -C "$seed" rev-parse --short HEAD~1)"

run() { GOV_PLUGIN_REPO="$1" bash "$SCRIPT" "${@:2}"; }
head_of() { git -C "$1" rev-parse --short HEAD; }
branch_of() { git -C "$1" rev-parse --abbrev-ref HEAD; }

# ── happy path: behind on trunk -> fast-forwarded ──
R="$SB/r-happy"; fixture "$R"
if run "$R" >/dev/null 2>&1 && [[ "$(head_of "$R")" == "$TARGET" ]]; then
  ok "behind on trunk fast-forwards to origin/master"
else
  bad "behind on trunk fast-forwards (head=$(head_of "$R") want=$TARGET)"
fi

# ── already current: exits 0, no-op ──
if run "$R" 2>&1 | grep -q "already current"; then ok "already-current is a clean no-op"
else bad "already-current is a clean no-op"; fi

# ── stale feature branch with NO unique commits: the 2026-08-17 real case ──
# A merged/abandoned codex branch left pointing at an old master. Nothing is at
# risk, so this must deploy rather than refuse — refusing here would make the
# guard useless in the exact situation it was written for.
R="$SB/r-stalebranch"; fixture "$R"
git -C "$R" checkout -qb codex/stale
if run "$R" >/dev/null 2>&1 \
   && [[ "$(head_of "$R")" == "$TARGET" && "$(branch_of "$R")" == "master" ]]; then
  ok "stale branch with no unique commits deploys and lands on master"
else
  bad "stale branch with no unique commits (head=$(head_of "$R") branch=$(branch_of "$R"))"
fi

# ── dirty tree: refuse, and do not move ──
R="$SB/r-dirty"; fixture "$R"
echo edited > "$R/f"
if run "$R" >/dev/null 2>&1; then bad "dirty tree refuses"
elif [[ "$(head_of "$R")" == "$PREV" ]]; then ok "dirty tree refuses and leaves checkout untouched"
else bad "dirty tree refused but moved the checkout"; fi

# ── untracked file also counts as dirty (it can be a live hook) ──
R="$SB/r-untracked"; fixture "$R"
echo new > "$R/untracked-hook.py"
if run "$R" >/dev/null 2>&1; then bad "untracked file refuses"
else ok "untracked file refuses"; fi

# ── branch carrying unique commits: refuse ──
R="$SB/r-work"; fixture "$R"
git -C "$R" checkout -qb wip
echo wip > "$R/w"; git -C "$R" add w; git -C "$R" commit -qm wip
WIP="$(head_of "$R")"
if run "$R" >/dev/null 2>&1; then bad "in-flight branch refuses"
elif [[ "$(head_of "$R")" == "$WIP" ]]; then ok "in-flight branch refuses and preserves the work"
else bad "in-flight branch refused but moved off the work"; fi

# ── local trunk AHEAD of origin: refuse ──
# The case `git pull --ff-only` does NOT catch: it reports "Already up to date"
# and succeeds, so without this guard an unpushed commit would deploy itself.
R="$SB/r-ahead"; fixture "$R"
git -C "$R" merge -q --ff-only origin/master
echo local > "$R/l"; git -C "$R" add l; git -C "$R" commit -qm unpushed
AHEAD="$(head_of "$R")"
if run "$R" >/dev/null 2>&1; then bad "unpushed trunk commit refuses"
elif [[ "$(head_of "$R")" == "$AHEAD" ]]; then ok "unpushed trunk commit refuses (pull --ff-only would not)"
else bad "unpushed trunk commit refused but moved"; fi

# ── unreachable origin: refuse rather than report a false 'already current' ──
R="$SB/r-offline"; fixture "$R"
git -C "$R" remote set-url origin "$SB/does-not-exist.git"
if run "$R" >/dev/null 2>&1; then bad "failed fetch refuses"
elif [[ "$(head_of "$R")" == "$PREV" ]]; then ok "failed fetch refuses instead of claiming freshness"
else bad "failed fetch refused but moved"; fi

# ── --dry-run changes nothing ──
R="$SB/r-dry"; fixture "$R"
if run "$R" --dry-run >/dev/null 2>&1 && [[ "$(head_of "$R")" == "$PREV" ]]; then
  ok "--dry-run leaves the checkout where it was"
else
  bad "--dry-run leaves the checkout where it was"
fi

# ── not a git checkout: refuse ──
mkdir -p "$SB/r-nogit"
if run "$SB/r-nogit" >/dev/null 2>&1; then bad "non-checkout refuses"; else ok "non-checkout refuses"; fi

# ── end to end: a BEHIND verdict actually flows through the sweep ──
# The original bug was not in either script alone — deploy-status.sh reported
# the drift correctly and deploy-gov-plugin.sh did not exist, so the sweep
# printed "SKIP" and a clean summary. Testing the two halves separately would
# not have caught it. Real deploy-apply.sh + real deploy-gov-plugin.sh, with
# only deploy-status.sh stubbed (it hardcodes the operator's real paths).
OPSSB="$SB/ops"; mkdir -p "$OPSSB"
OPSDIR="$(dirname "$SCRIPT")"
cp "$OPSDIR/deploy-apply.sh" "$OPSDIR/deploy-gov-plugin.sh" "$OPSDIR/deploy-lib.sh" "$OPSSB/"
chmod +x "$OPSSB/deploy-apply.sh" "$OPSSB/deploy-gov-plugin.sh"
cat > "$OPSSB/deploy-status.sh" <<'STUB'
#!/usr/bin/env bash
printf '[{"name":"gov-plugin","verdict":"BEHIND(1)","branch":"master","commit":"0000000","behind":"1","pid":"-","pickup":"live-from-checkout","health":""}]\n'
STUB
chmod +x "$OPSSB/deploy-status.sh"
R="$SB/r-sweep"; fixture "$R"
if GOV_PLUGIN_REPO="$R" bash "$OPSSB/deploy-apply.sh" --no-fetch >/dev/null 2>&1 \
   && [[ "$(head_of "$R")" == "$TARGET" ]]; then
  ok "deploy-apply sweep deploys a BEHIND gov-plugin end to end"
else
  bad "deploy-apply sweep deploys a BEHIND gov-plugin (head=$(head_of "$R") want=$TARGET)"
fi

rm -rf "$SB"
echo "test-deploy-gov-plugin: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
