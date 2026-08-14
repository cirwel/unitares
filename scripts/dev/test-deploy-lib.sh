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

# ── ff_worktree --branch: a repo whose trunk is `main`, not `master` ──
# The fleet is not single-repo — unitares-discord-bridge is on `main`. Before
# --branch the ref was hardcoded three ways (fetch, worktree add, merge), so
# this repo shape could not use the lib at all. A create AND an ff are both
# exercised: the create path is where a hardcoded `master` fails loudest
# ("invalid reference: master"), the ff path is where it would silently do
# nothing on a repo that happens to have both branches.
ORIGIN2="$SB/origin2.git"; REPO2="$SB/repo2"; DEP2="$SB/dep2"
git init -q --bare "$ORIGIN2"
git init -q -b main "$REPO2"
( cd "$REPO2" && echo a > f && git add f && git commit -qm c1 && git remote add origin "$ORIGIN2" && git push -q origin main && git checkout -qb dev )
out2="$( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO2" "$DEP2" --branch main >/dev/null 2>&1; echo "$DEPLOY_LIB_FRESH" )"
[[ "$out2" == "1" && -d "$DEP2" ]] && ok "ff_worktree --branch main creates worktree" || bad "ff_worktree --branch create: FRESH=$out2"
( cd "$REPO2" && echo b >> f && git add f && git commit -qm c2 && git push -q origin HEAD:main )
( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO2" "$DEP2" --branch main ) >/dev/null 2>&1
[[ "$(git -C "$DEP2" rev-parse HEAD)" == "$(git -C "$REPO2" rev-parse origin/main)" ]] \
  && ok "ff_worktree --branch main ffs to origin/main" || bad "ff_worktree --branch ff did not advance"

# A typo'd flag must fail loudly, not silently deploy the wrong trunk.
( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO2" "$DEP2" --branch ) >/dev/null 2>&1 \
  && bad "ff_worktree --branch with no value refuses" || ok "ff_worktree --branch with no value refuses"

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

# ── deploy-status.sh: BEHIND must apply to every running/live verdict ──
# Gating the BEHIND promotion on CURRENT made checkout drift structurally
# invisible for anything that never reports CURRENT. live-from-checkout is set
# to LIVE unconditionally, so gov-plugin could sit any number of commits behind
# origin and still display LIVE — measured 2026-08-12 at behind=2, including a
# release bump. For that service the checkout IS the deployed artifact, so
# "behind" is the only drift signal that exists. DOWN and GHOST-BRANCH must NOT
# be overridden: each names a more urgent, different action than "pull".
(
  set -uo pipefail
  promote() { # verdict behind -> verdict
    local verdict="$1" behind="$2"
    if [ "$behind" != "0" ] && [ "$behind" != "?" ]; then
      case "$verdict" in
        CURRENT|CURRENT\*|LIVE|HOT-RELOAD|STALE*) verdict="BEHIND($behind)" ;;
      esac
    fi
    printf '%s' "$verdict"
  }
  # promoted
  [ "$(promote LIVE 2)"          = "BEHIND(2)" ] || exit 20
  [ "$(promote CURRENT 5)"       = "BEHIND(5)" ] || exit 21
  [ "$(promote 'CURRENT*' 3)"    = "BEHIND(3)" ] || exit 22
  [ "$(promote HOT-RELOAD 1)"    = "BEHIND(1)" ] || exit 23
  [ "$(promote 'STALE(4)' 7)"    = "BEHIND(7)" ] || exit 24
  # preserved
  [ "$(promote DOWN 2)"          = "DOWN" ]         || exit 25
  [ "$(promote GHOST-BRANCH 1)"  = "GHOST-BRANCH" ] || exit 26
  [ "$(promote n/a 2)"           = "n/a" ]          || exit 27
  [ "$(promote LIVE 0)"          = "LIVE" ]         || exit 28
  [ "$(promote LIVE '?')"        = "LIVE" ]         || exit 29
  # and the real script must not have re-gated it on CURRENT alone
  grep -q '\[ "\$verdict" = "CURRENT" \] && verdict="BEHIND' \
    "$(dirname "$LIB")/deploy-status.sh" && exit 30
  exit 0
) && ok "deploy-status BEHIND applies to LIVE/HOT-RELOAD/STALE, not DOWN/GHOST" \
  || bad "deploy-status BEHIND promotion rules"

# NOTE on the symlink class: `git worktree list --porcelain` prints FULLY
# resolved paths (macOS /var -> /private/var, and any intermediate symlink), so
# comparing them to the caller's literal argument reported "missing" for an
# existing worktree and then died on `worktree add ... already exists`. The
# regression test for that is the "ff_worktree ffs to origin/master" case above
# — it runs under mktemp (/var/folders/...), which IS a symlinked path, and it
# was RED until deploy-lib resolved both sides. A separate symlink case was
# tried here and removed: it could not be made to fail against the old lib, so
# it asserted nothing.

# ── ff_worktree --detach: a SECOND dedicated worktree cannot use the branch ──
# git allows one checkout of `master` per repo, so branch-mode add fails once
# unitares-deploy holds it. The orchestrator's tree is detached for this reason.
ORIGIN3="$SB/o3.git"; REPO3="$SB/r3"; DEP3A="$SB/dep3a"; DEP3B="$SB/dep3b"
git init -q --bare "$ORIGIN3"
git init -q -b master "$REPO3"
( cd "$REPO3" && echo a > f && git add f && git commit -qm c1 && git remote add origin "$ORIGIN3" \
  && git push -q origin master && git checkout -qb dev ) >/dev/null 2>&1
( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO3" "$DEP3A" >/dev/null 2>&1 ) || true
if ( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO3" "$DEP3B" --detach ) >/dev/null 2>&1 \
   && [ "$(git -C "$DEP3B" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "HEAD" ]; then
  ok "ff_worktree --detach creates a second worktree detached at origin/master"
else
  bad "ff_worktree --detach second worktree"
fi

# ── deploy-status health(): bearer-gated 401 is proof of life, not a failure ──
# agent-orchestrator :8789 and lease-plane :8788 refuse an unauthenticated
# /health. Pasting the permission_denied blob into the table reads as broken.
(
  set -euo pipefail
  ds="$(dirname "$LIB")/deploy-status.sh"
  grep -q '401|403) printf .up (bearer-gated' "$ds" || exit 1
  # and the orchestrator must actually be in the component table with its port
  grep -q '^"agent-orchestrator|com.unitares.agent-orchestrator|' "$ds" || exit 2
  grep -q 'elixir/agent_orchestrator|restart|8789"' "$ds" || exit 3
) && ok "deploy-status treats a gated 401 as up and lists agent-orchestrator" \
  || bad "deploy-status gated-health / orchestrator row"

# ── deploy-apply dispatches the orchestrator ──
(
  set -euo pipefail
  grep -q 'agent-orchestrator) echo "$OPS_DIR/deploy-orchestrator.sh"' \
    "$(dirname "$LIB")/deploy-apply.sh"
) && ok "deploy-apply dispatches agent-orchestrator" \
  || bad "deploy-apply orchestrator dispatch"

# ── deploy-apply dispatches the discord bridge ──
# It was the last SKIP in the sweep ("no deploy script"), which mattered more
# than it looked: the bridge is the alert delivery path, so a stale bridge is a
# stale alarm. Guard both halves of the wiring — a dispatch entry pointing at a
# script that does not exist would re-open the same silent gap.
(
  set -euo pipefail
  grep -q 'discord-bridge)  echo "$OPS_DIR/deploy-bridge.sh"' \
    "$(dirname "$LIB")/deploy-apply.sh"
  [ -x "$(dirname "$LIB")/deploy-bridge.sh" ]
) && ok "deploy-apply dispatches discord-bridge to an executable script" \
  || bad "deploy-apply discord-bridge dispatch"

# The bridge repo's trunk is `main`; deploying it off `master` would be a
# silent no-op forever. Pin that the script actually passes --branch main.
# ── ff_worktree --detach --branch TOGETHER ──
# The two flags were each covered alone and NEVER in combination, which is the
# only way deploy-bridge.sh actually calls them: the bridge repo's trunk is
# `main` AND its dev checkout already occupies that branch, so the real call
# needs both at once. A source-grep for the flag string cannot catch arg
# parsing that breaks only for the pair, so exercise it for real.
ORIGIN4="$SB/origin4.git"; REPO4="$SB/repo4"; DEP4A="$SB/dep4a"; DEP4B="$SB/dep4b"
git init -q --bare "$ORIGIN4"
git init -q -b main "$REPO4"
( cd "$REPO4" && echo a > f && git add f && git commit -qm c1 && git remote add origin "$ORIGIN4" && git push -q origin main )
# REPO4 stays ON main, reproducing the bridge's actual layout: branch-mode
# `worktree add` must fail here, which is precisely why --detach is required.
( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO4" "$DEP4A" --detach --branch main ) >/dev/null 2>&1 \
  && ok "ff_worktree --detach --branch main works when the trunk is checked out" \
  || bad "ff_worktree --detach --branch main (the bridge's actual call) failed"
[[ "$(git -C "$DEP4A" rev-parse HEAD 2>/dev/null)" == "$(git -C "$REPO4" rev-parse origin/main)" ]] \
  && ok "ff_worktree --detach --branch main lands on origin/main" || bad "--detach --branch landed on the wrong ref"
# Flag order must not matter.
( set -euo pipefail; . "$LIB"; deploy_lib_ff_worktree t "$REPO4" "$DEP4B" --branch main --detach ) >/dev/null 2>&1 \
  && ok "ff_worktree accepts --branch before --detach" || bad "ff_worktree flag order dependence"

# The source-grep below is a deliberate regression guard, NOT a behavioral test:
# it only catches someone deleting the flags from the call site. The behavior
# itself is covered above.
grep -q -- '--detach --branch main' "$(dirname "$LIB")/deploy-bridge.sh" \
  && ok "[guard] deploy-bridge call site still passes --detach --branch main" \
  || bad "deploy-bridge missing --detach --branch main"

# The bridge has no HTTP health endpoint, so the verify probe MUST be the
# heartbeat the liveness watchdog also trusts — a probe that only checked the
# process was alive would pass against a wedged event loop, which is the exact
# 2026-06-19 hang the watchdog exists for.
grep -q 'BRIDGE_HEARTBEAT_PATH' "$(dirname "$LIB")/deploy-bridge.sh" \
  && ok "deploy-bridge verifies via the shared heartbeat signal" || bad "deploy-bridge heartbeat probe"

# ── deploy-apply dispatches dialectic-live ──
# It ran for months absent from deploy-status.sh entirely — no row, no dispatch
# — while loading from the SHARED worktree every other deploy fast-forwards.
# The failure mode was silence, so the guard is that it stays wired at all.
(
  set -euo pipefail
  grep -q 'dialectic-live)  echo "$OPS_DIR/deploy-dialectic-live.sh"' \
    "$(dirname "$LIB")/deploy-apply.sh"
  [ -x "$(dirname "$LIB")/deploy-dialectic-live.sh" ]
) && ok "deploy-apply dispatches dialectic-live to an executable script" \
  || bad "deploy-apply dialectic-live dispatch"

# Every service that loads from the shared worktree needs a status row, or a ff
# moves its code with nothing reporting the drift. Assert the row exists AND is
# subdir-scoped — an unscoped row would read BEHIND(hundreds) forever and be
# learned-to-ignore, which is the same silence in a louder costume.
grep -q '"dialectic-live|com.unitares.dialectic-live|.*|elixir/dialectic_live|restart|8790"' \
  "$(dirname "$LIB")/deploy-status.sh" \
  && ok "deploy-status has a subdir-scoped dialectic-live row" || bad "dialectic-live status row"

echo; echo "passed=$pass failed=$fail"
rm -rf "$SB"
exit "$((fail > 0))"
