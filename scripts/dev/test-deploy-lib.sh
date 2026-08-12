#!/usr/bin/env bash
# test-deploy-lib.sh — functional sandbox tests for scripts/ops/deploy-lib.sh.
#
# Everything runs against throwaway fixtures under mktemp: a bare "origin", a
# dev checkout on a feature branch (matching the real operator layout — the
# deploy worktree can only be created when the dev checkout is NOT on master),
# and a synthetic plist. No launchctl, no network, no live services touched.
# Each case runs in a subshell so exits inside lib functions don't kill the
# harness. Invoked by scripts/dev/check-deploy-lib.sh (CI smoke job).
set -u
LIB="$(cd "$(dirname "$0")/../ops" && pwd)/deploy-lib.sh"
SB="$(mktemp -d)"
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

# ── executing the lib directly must refuse ──
if bash "$LIB" >/dev/null 2>&1; then bad "lib refuses direct execution"; else ok "lib refuses direct execution"; fi

# ── lock: acquire, contend, reclaim-stale ──
export UNITARES_DEPLOY_LOCK="$SB/test.lock"
(
  set -euo pipefail; . "$LIB"
  deploy_lib_acquire_lock t "$SB/deploy"
  [[ -d "$UNITARES_DEPLOY_LOCK" ]] || exit 9
) && [[ ! -d "$SB/test.lock" ]] && ok "lock acquired and released on EXIT" || bad "lock acquire/release"

mkdir "$SB/test.lock"; printf '%s' "$$" > "$SB/test.lock/pid"   # live holder (this shell)
( set -euo pipefail; . "$LIB"; deploy_lib_acquire_lock t "$SB/deploy" ) >/dev/null 2>&1 \
  && bad "lock refuses when live holder present" || ok "lock refuses when live holder present"
rm -rf "$SB/test.lock"

mkdir "$SB/test.lock"; printf '%s' "999999" > "$SB/test.lock/pid"  # dead holder
( set -euo pipefail; . "$LIB"; deploy_lib_acquire_lock t "$SB/deploy" ) >/dev/null 2>&1 \
  && ok "lock reclaims stale (dead-holder) lock" || bad "lock reclaims stale lock"
rm -rf "$SB/test.lock"
unset UNITARES_DEPLOY_LOCK

# ── lock key derivation matches historical per-script derivation ──
D="/Users/x/projects/unitares-deploy"
hist="${TMPDIR:-/tmp}/unitares-deploy$(printf '%s' "$D" | tr -c 'A-Za-z0-9' '_').lock"
libk="$( set -euo pipefail; . "$LIB"; deploy_lib_acquire_lock t "$D" >/dev/null 2>&1; printf '%s' "$DEPLOY_LIB_LOCK_DIR" )"
[[ "$libk" == "$hist" ]] && ok "lock key byte-identical to historical derivation" || bad "lock key drift: lib=$libk hist=$hist"

# ── plist preflight ──
P="$SB/svc.plist"
( set -euo pipefail; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" ) >/dev/null 2>&1 \
  && ok "missing plist passes by default" || bad "missing plist passes by default"
( set -euo pipefail; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" --require-exists ) >/dev/null 2>&1 \
  && bad "missing plist + --require-exists refuses" || ok "missing plist + --require-exists refuses"
echo "<string>/deploy/needle/prog.py</string>" > "$P"
( set -euo pipefail; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" ) >/dev/null 2>&1 \
  && ok "matching plist passes" || bad "matching plist passes"
echo "<string>/dev/checkout/prog.py</string>" > "$P"
( set -euo pipefail; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" ) >/dev/null 2>&1 \
  && bad "mismatched plist hard-refuses (no escape hatch)" || ok "mismatched plist hard-refuses (no escape hatch)"
( set -euo pipefail; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" --allow-env MY_ALLOW ) >/dev/null 2>&1 \
  && bad "mismatched plist refuses when allow-env unset" || ok "mismatched plist refuses when allow-env unset"
( set -euo pipefail; export MY_ALLOW=1; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" --allow-env MY_ALLOW ) >/dev/null 2>&1 \
  && ok "mismatched plist passes with allow-env=1" || bad "mismatched plist passes with allow-env=1"

# ── ff_worktree: create-if-missing, then ff on an advanced origin ──
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
ORIGIN="$SB/origin.git"; REPO="$SB/repo"; DEP="$SB/dep"
git init -q --bare "$ORIGIN"
git init -q -b master "$REPO"; ( cd "$REPO" && echo a > f && git add f && git commit -qm c1 && git remote add origin "$ORIGIN" && git push -q origin master && git checkout -qb dev )
out="$( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO" "$DEP" >/dev/null 2>&1; echo "$DEPLOY_LIB_FRESH" )"
[[ "$out" == "1" && -d "$DEP" ]] && ok "ff_worktree creates missing worktree (FRESH=1)" || bad "ff_worktree create: FRESH=$out"
( cd "$REPO" && echo b >> f && git add f && git commit -qm c2 && git push -q origin HEAD:master )
res="$( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO" "$DEP" >/dev/null 2>&1; echo "$DEPLOY_LIB_FRESH:$DEPLOY_LIB_PREV:$(git -C "$DEP" rev-parse HEAD)" )"
fresh="${res%%:*}"; rest="${res#*:}"; prev="${rest%%:*}"; head="${rest#*:}"
exp_head="$(git -C "$REPO" rev-parse origin/master)"
[[ "$fresh" == "0" && "$head" == "$exp_head" && "$prev" != "$head" ]] \
  && ok "ff_worktree ffs to origin/master (FRESH=0, PREV=old)" || bad "ff_worktree ff: $res"

# ── poll ──
( set -euo pipefail; . "$LIB"
  n=0; probe() { n=$((n+1)); [[ $n -ge 3 ]]; }
  deploy_lib_poll 5 0 probe ) && ok "poll succeeds when probe eventually passes" || bad "poll eventual success"
( set -euo pipefail; . "$LIB"; deploy_lib_poll 3 0 false ) && bad "poll fails after attempts exhausted" || ok "poll fails after attempts exhausted"

# ── deploy-status.sh staleness counting must survive merge commits ──
# Regression guard for the false-CURRENT* bug (measured 2026-08-12: the
# governance MCP ran build_sha 1ac9912a against a 56728eba checkout and was
# reported CURRENT*, i.e. "skip restart"). With a pathspec, git applies history
# simplification and prunes the merge commit; the branch commits underneath
# carry pre-merge dates that fall outside --since, so the count comes back 0.
# --full-history keeps the merge visible. This repo lands work as PR merges, so
# without it every post-restart merge is invisible to the staleness check.
(
  set -euo pipefail
  R="$SB/mergecount"; mkdir -p "$R"; cd "$R"
  git init -q -b master .; git config user.email t@t; git config user.name t
  echo a > f.txt; git add f.txt; git commit -qm init
  git checkout -qb feat
  echo b > f.txt
  # branch commit dated well BEFORE the cutoff; the merge lands after it
  GIT_COMMITTER_DATE="2020-01-01T00:00:00" git commit -qam "work" --date="2020-01-01T00:00:00"
  git checkout -q master
  git merge -q --no-ff feat -m "Merge pull request #1 from feat"
  S="2021-01-01 00:00:00"
  simplified=$(git rev-list --count --since="$S" master -- .)
  full=$(git rev-list --count --full-history --since="$S" master -- .)
  [ "$simplified" = "0" ] || exit 9        # the trap this guards against
  [ "$full" -ge 1 ] || exit 10             # --full-history must see the merge
  grep -q -- '--full-history' "$(dirname "$LIB")/deploy-status.sh" || exit 11
) && ok "deploy-status counts merge commits (--full-history)" \
  || bad "deploy-status merge-commit staleness counting"

echo; echo "passed=$pass failed=$fail"
rm -rf "$SB"
exit "$((fail > 0))"
