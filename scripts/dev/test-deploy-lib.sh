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
preflight_out="$(
  set +e
  ( set -euo pipefail; . "$LIB"; deploy_lib_require_plist_target t "$P" "/needle" \
      --recipe '[t]   migrate-service.sh' --recipe-handles-reload ) 2>&1
)"
if printf '%s' "$preflight_out" | grep -q 'migrate-service.sh' \
  && ! printf '%s' "$preflight_out" | grep -q 'launchctl unload'; then
  ok "self-contained migration recipe suppresses duplicate reload command"
else
  bad "self-contained migration recipe output"
fi

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

# ── deploy-status.sh: the hot-reload row must be able to report staleness ──
# Regression guard for a blind spot measured 2026-08-28. The hot-reload branch
# was `pid ? HOT-RELOAD : DOWN` and computed no staleness whatsoever, so the
# lease plane -- the only hot-reload row -- could never report CURRENT or
# STALE. Immediately after a full restart onto brand-new code the row read
# HOT-RELOAD, and it would have read identically had the restart never
# happened. The only drift signal it ever received was the BEHIND(n) override,
# which describes the CHECKOUT, not the running process.
#
# EXTRACTED and EXECUTED, never retyped and never grepped. An earlier cut of
# this test only grep'd the branch's source text for 'build_sha "$port"' and
# 'verdict="STALE('. Every one of those greps passes against a swapped
# bsha/sha, an inverted comparison, or a reversed "$bsha..$base" -- i.e. it
# proved the code contained some words, not that it computed anything. Same
# trap the promote() test below documents. So: run the real block against a
# real git repo with the real commands stubbed only where they touch the host.
(
  set -uo pipefail
  DS="$(dirname "$LIB")/deploy-status.sh"

  # A real repo, so the delta count and the "$bsha..$base" operand order are
  # genuinely exercised rather than mocked into agreement.
  R="$SB/hotreload"; mkdir -p "$R"; cd "$R"
  git init -q -b master .; git config user.email t@t; git config user.name t
  mkdir -p svc
  echo one > svc/f.txt; git add svc; git commit -qm c1
  OLD=$(git rev-parse --short HEAD)
  echo two > svc/f.txt; git commit -qam c2
  echo three > svc/f.txt; git commit -qam c3
  HEADSHA=$(git rev-parse --short HEAD)

  body=$(sed -n '/^    hot-reload)/,/^      fi ;;$/p' "$DS")
  eval "hotverdict() {
    local pid_out=\"\$1\" sha_out=\"\$2\"
    local label=svc port=9999 repo=\"$R\" base=\"\$4\" cpath=\"\${5:-svc}\" sha=\"\$3\"
    local verdict= bsha= delta= n=
    proc_pid() { [ -n \"\$pid_out\" ] && printf '%s' \"\$pid_out\"; }
    build_sha() { [ -n \"\$sha_out\" ] && printf '%s' \"\$sha_out\"; }
    case hot-reload in
$body
    esac
    printf '%s' \"\$verdict\"
  }"

  # process sha == checkout sha -> CURRENT
  [ "$(hotverdict 123 "$HEADSHA" "$HEADSHA" master)" = "CURRENT" ] || exit 20
  # process sha behind the checkout -> STALE with the real commit delta (2).
  # A reversed "$bsha..$base" would count 0 here and this would fail.
  [ "$(hotverdict 123 "$OLD" "$HEADSHA" master)" = "STALE(Δ2)" ] || exit 21
  # Shas differ but NONE of the commits touched this service's paths -> Δ0.
  # Must be CURRENT*, not STALE(Δ0): the hot-reload row now feeds deploy-apply,
  # so STALE(Δ0) would restart the plane on every unrelated monorepo commit.
  git checkout -q -b unrelated
  echo x > other.txt; git add other.txt; git commit -qm "unrelated"
  UNREL=$(git rev-parse --short HEAD)
  [ "$(hotverdict 123 "$HEADSHA" "$UNREL" unrelated)" = "CURRENT*" ] || exit 25
  git checkout -q master
  # cpath is a pathspec LIST. elixir/unitares_sdk is compiled INTO the lease
  # plane, so an SDK-only commit changes the running binary; counting only
  # elixir/lease_plane reported Δ0 -> CURRENT* and skipped a restart that was
  # actually required. Model that here with a second directory.
  mkdir -p sdk; echo v1 > sdk/g.txt; git add sdk; git commit -qm sdk1
  SDK_BEFORE=$(git rev-parse --short HEAD)
  echo v2 > sdk/g.txt; git commit -qam sdk2
  SDK_AFTER=$(git rev-parse --short HEAD)
  # one pathspec: the SDK commit is invisible -> wrongly CURRENT*
  [ "$(hotverdict 123 "$SDK_BEFORE" "$SDK_AFTER" master svc)" = "CURRENT*" ] || exit 26
  # both pathspecs: the SDK commit counts -> STALE(Δ1)
  [ "$(hotverdict 123 "$SDK_BEFORE" "$SDK_AFTER" master "svc sdk")" = "STALE(Δ1)" ] || exit 27
  # no build_sha published -> honestly unverifiable, never a healthy verdict
  [ "$(hotverdict 123 "" "$HEADSHA" master)" = "HOT-RELOAD(?)" ] || exit 22
  # no process -> DOWN outranks everything
  [ "$(hotverdict "" "$HEADSHA" "$HEADSHA" master)" = "DOWN" ] || exit 23
  # STALE is the required spelling: deploy-apply.sh selects work with
  # startswith("STALE")/startswith("BEHIND") (deploy-apply.sh:114), so a
  # bespoke verdict would be visible to the operator and invisible to the
  # deployer -- the lease plane would silently stop auto-deploying.
  grep -q 'startswith("STALE")' "$(dirname "$DS")/deploy-apply.sh" || exit 24
  # The shipped lease-plane row must count every path that changes its binary.
  # nudge-lease-plane.sh is the existing authority on that set; drift between
  # the two silently reintroduces the skipped-restart case exercised above.
  grep -q 'elixir/lease_plane elixir/unitares_sdk|hot-reload' "$DS" || exit 28
  grep -q 'elixir/lease_plane elixir/unitares_sdk' "$(dirname "$DS")/nudge-lease-plane.sh" || exit 29
) && ok "deploy-status hot-reload row computes CURRENT/STALE/HOT-RELOAD(?)/DOWN" \
  || bad "deploy-status hot-reload staleness"

# ── lease plane must publish the boot sha deploy-status scrapes ──
# deploy-status's build_sha() greps the UNAUTHENTICATED /health. If the lease
# plane stops publishing it there, the row silently degrades to HOT-RELOAD(?)
# forever -- the same invisible-drift state, just differently spelled.
(
  set -euo pipefail
  ROOT="$(cd "$(dirname "$LIB")/../.." && pwd)"
  R="$ROOT/elixir/lease_plane/lib/unitares_lease_plane/http_router.ex"
  # Must be inside the pre-auth liveness body, not the authed /v1/health.
  awk '/defp liveness\(%Plug.Conn\{method: "GET", path_info: \["health"\]/,/^  end$/' "$R" \
    | grep -q 'build_sha:' || exit 24
  [ -f "$ROOT/elixir/lease_plane/lib/unitares_lease_plane/build_info.ex" ] || exit 25
) && ok "lease plane publishes build_sha on the pre-auth /health" \
  || bad "lease plane build_sha on /health"

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
  # EXTRACTED from deploy-status.sh, never retyped. The previous version of
  # this test carried its own hand-copied promote(), so when the real script's
  # case list changed the test kept passing against the copy — it was testing
  # itself. Extraction is what stops the two drifting apart in silence.
  DS="$(dirname "$LIB")/deploy-status.sh"
  eval "promote() { local verdict=\"\$1\" behind=\"\$2\"
    $(sed -n '/^  if \[ "\$behind" != "0" \]/,/^  fi$/p' "$DS")
    printf '%s' \"\$verdict\"; }"
  # promoted
  [ "$(promote LIVE 2)"          = "BEHIND(2)" ] || exit 20
  [ "$(promote CURRENT 5)"       = "BEHIND(5)" ] || exit 21
  [ "$(promote 'CURRENT*' 3)"    = "BEHIND(3)" ] || exit 22
  [ "$(promote HOT-RELOAD 1)"    = "BEHIND(1)" ] || exit 23
  # HOT-RELOAD(?) means "could not read the process's sha", NOT "the process is
  # behind", so it must not mask the concrete BEHIND fact (2026-08-28). A
  # hot-reload row that IS behind reports STALE(Δn) and is preserved below,
  # like every other service.
  [ "$(promote 'HOT-RELOAD(?)' 2)" = "BEHIND(2)" ] || exit 27
  # STALE moved from promoted to PRESERVED (2026-08-14). It is the sharper
  # fact — the RUNNING PROCESS is executing superseded code — and letting
  # "your checkout needs a pull" overwrite it made a STALE(12) service display
  # as BEHIND(1): the milder problem hiding the worse one.
  [ "$(promote 'STALE(4)' 7)"    = "STALE(4)" ]     || exit 24
  [ "$(promote DOWN 2)"          = "DOWN" ]         || exit 25
  [ "$(promote GHOST-BRANCH 1)"  = "GHOST-BRANCH" ] || exit 26
  [ "$(promote n/a 2)"           = "n/a" ]          || exit 27
  [ "$(promote LIVE 0)"          = "LIVE" ]         || exit 28
  [ "$(promote LIVE '?')"        = "LIVE" ]         || exit 29
  exit 0
) && ok "deploy-status BEHIND promotes CURRENT/LIVE/HOT-RELOAD(?), preserves STALE/DOWN/GHOST" \
  || bad "deploy-status BEHIND promotion rules"

# ── behind must be scoped to the service's OWN code path ──
# Repo-wide was the single worst bug in this tool. On a monorepo the shared
# worktree is nearly always >=1 commit behind, so every service read BEHIND(n)
# regardless of whether the commit touched it: measured 2026-08-13, one commit
# to governance_monitor.py put SIX services in the restart set, including
# sentinel-beam and the fail-closed lease-plane. It also made CURRENT*
# unreachable, disabling this tool's best idea with its bluntest one.
(
  set -euo pipefail
  eval "$(sed -n '/^behind_count() {/,/^}/p' "$(dirname "$LIB")/deploy-status.sh")"
  O="$SB/origin5.git"; R="$SB/repo5"
  git init -q --bare "$O"; git init -q -b master "$R"
  cd "$R" && git remote add origin "$O"
  mkdir -p svc other && echo a > svc/f && echo a > other/f
  git add svc other && git commit -qm base && git push -q origin master
  echo b >> other/f && git add other && git commit -qm "touches only other/" && git push -q origin master
  git reset -q --hard HEAD~1      # checkout now sits 1 commit behind origin
  [ "$(behind_count "$R" origin/master ".")"     = "1" ] || exit 41   # repo-wide sees it
  [ "$(behind_count "$R" origin/master "svc")"   = "0" ] || exit 42   # svc untouched -> no restart
  [ "$(behind_count "$R" origin/master "other")" = "1" ] || exit 43   # other touched -> restart
  exit 0
) && ok "behind_count is scoped to the service code path, not the whole repo" \
  || bad "behind_count path scoping"

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
  && ok "[guard] deploy-bridge still reads the shared heartbeat env var" || bad "deploy-bridge heartbeat probe"

# ── the heartbeat comparison itself ──
# Above is a name-grep. This exercises the actual decision: mtime STRICTLY
# greater than the pre-restart mark. A `-ge` there would pass on a heartbeat
# that never moved — the exact false success the pre-restart snapshot exists to
# prevent, and unreachable by any grep.
#
# Portability: the first version of this test used `stat -f` and `date -v`,
# which are BSD-only. It passed on the macOS deploy host and failed on Linux
# CI, where the fallback re-touched the file to "now" and the strict-greater
# comparison then saw two identical timestamps. Rather than touch files and
# race the clock, the mark is moved arithmetically — same comparison, no
# sleeps, no platform-specific date handling, deterministic everywhere.
#
# The second version chained `stat -f %m || stat -c %Y` and was still red on
# Linux, because an `||` chain assumes the failing branch prints nothing. GNU
# stat's `-f` means "file system status", so `stat -f %m FILE` reads `%m` as a
# second FILE: it fails on that one (firing the fallback) but still prints the
# whole multi-line filesystem block for FILE. The mtime then arrives appended
# to `File: … Block size: 4096 …`, and the comparison dies with "integer
# expression expected". Trust the OUTPUT SHAPE, not the exit status: take the
# first probe that yields a bare integer.
_mt() {
  local m
  for m in "$(stat -c %Y "$1" 2>/dev/null)" "$(stat -f %m "$1" 2>/dev/null)"; do
    case "$m" in ''|*[!0-9]*) ;; *) printf '%s\n' "$m"; return 0 ;; esac
  done
  echo 0
}
HB="$SB/hb"; : > "$HB"
hb_probe() { [ "$(_mt "$HB")" -gt "$1" ]; }   # mirrors deploy-bridge.sh
HB_NOW="$(_mt "$HB")"
hb_probe "$HB_NOW" && bad "heartbeat probe passed on an UNCHANGED file" \
  || ok "heartbeat probe rejects an unchanged heartbeat (stale-file false success)"
hb_probe "$((HB_NOW - 10))" && ok "heartbeat probe accepts an advanced heartbeat" \
  || bad "heartbeat probe rejected a genuinely advanced heartbeat"
rm -f "$HB"
hb_probe 0 && bad "heartbeat probe passed with NO heartbeat file" \
  || ok "heartbeat probe treats a missing heartbeat as not-advanced"

# ── derivation: an UNREGISTERED running service must be reported ──
# The COMPONENTS array is a hand-maintained list of what exists, and a
# hand-maintained list of what exists is what failed twice: dialectic_live
# served traffic for months while absent from it, and once that was "fixed" a
# review immediately found ipv6-loopback-proxy in the identical state. A
# missing row was indistinguishable from a healthy fleet. This asserts the
# derivation turns that silence into an UNGOVERNED row.
(
  set -euo pipefail
  UG="$SB/ugrepo"; git init -q -b master "$UG"
  ( cd "$UG" && echo x > f && git add f && git -c user.email=t@t -c user.name=t commit -qm c1 )
  fake_plist_dir="$SB/agents"; mkdir -p "$fake_plist_dir"
  # Extract the real function rather than reimplementing it.
  eval "$(sed -n '/^ungoverned_rows() {/,/^}/p' "$(dirname "$LIB")/deploy-status.sh")"
  git_branch() { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null; }
  git_short()  { git -C "$1" rev-parse --short HEAD 2>/dev/null; }
  COMPONENTS=("known|com.unitares.known|$UG||restart|")
  HOME_BAK="$HOME"
  rows=()
  # A RUNNING job, not in COMPONENTS, whose plist resolves to a git checkout.
  LAUNCHCTL_LIST_CMD="printf '4242\t0\tcom.unitares.mystery\n'"
  mkdir -p "$SB/fakehome/Library/LaunchAgents"
  printf '<string>%s</string>' "$UG" > "$SB/fakehome/Library/LaunchAgents/com.unitares.mystery.plist"
  HOME="$SB/fakehome"
  # the fixture repo lives outside $HOME/projects, so point the scan there
  sed_out=$(declare -f ungoverned_rows | sed "s|\$HOME/projects/\[A-Za-z0-9_.-\]\*|$UG|")
  eval "$sed_out"
  ungoverned_rows
  HOME="$HOME_BAK"
  [ "${#rows[@]}" -eq 1 ] || exit 51
  case "${rows[0]}" in *"|UNGOVERNED|"*) ;; *) exit 52 ;; esac
  case "${rows[0]}" in mystery*) ;; *) exit 53 ;; esac
  exit 0
) && ok "derivation reports a running, unregistered service as UNGOVERNED" \
  || bad "derivation did not flag an unregistered running service"

# A job that IS registered must NOT be reported twice.
(
  set -euo pipefail
  UG="$SB/ugrepo"
  eval "$(sed -n '/^ungoverned_rows() {/,/^}/p' "$(dirname "$LIB")/deploy-status.sh")"
  COMPONENTS=("known|com.unitares.mystery|$UG||restart|")
  rows=()
  LAUNCHCTL_LIST_CMD="printf '4242\t0\tcom.unitares.mystery\n'"
  HOME="$SB/fakehome" ungoverned_rows
  [ "${#rows[@]}" -eq 0 ] || exit 54
  exit 0
) && ok "derivation stays quiet for a service that IS registered" \
  || bad "derivation double-reported a registered service"

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

# ── deploy-apply dispatches gov-plugin ──
# The inverse silence: gov-plugin HAD a status row for months and no dispatch
# entry, so every sweep printed "SKIP ... no deploy script" to stderr and then a
# reassuring summary. Measured 2026-08-17 at behind=3. Row-without-dispatch is
# the failure mode, so assert the pair.
(
  set -euo pipefail
  grep -q 'gov-plugin)      echo "$OPS_DIR/deploy-gov-plugin.sh"' \
    "$(dirname "$LIB")/deploy-apply.sh"
  [ -x "$(dirname "$LIB")/deploy-gov-plugin.sh" ]
) && ok "deploy-apply dispatches gov-plugin to an executable script" \
  || bad "deploy-apply gov-plugin dispatch"

# ── deploy-apply dispatches the OpenAI governance proxy ──
# This service had a status row but no dispatch entry: every `cirwel update`
# reported BEHIND, printed a speculative restart-DEV question, and left the
# live checkout unchanged. Guard both the mapping and the executable target.
(
  set -euo pipefail
  grep -q 'openai-gov-proxy) echo "$OPS_DIR/deploy-openai-gov-proxy.sh"' \
    "$(dirname "$LIB")/deploy-apply.sh"
  [ -x "$(dirname "$LIB")/deploy-openai-gov-proxy.sh" ]
) && ok "deploy-apply dispatches openai-gov-proxy to an executable script" \
  || bad "deploy-apply openai-gov-proxy dispatch"

# Before the one-time plist migration, status must name the development
# checkout honestly. After migration it must follow the deploy worktree. A
# directory-exists heuristic is insufficient because preparing a worktree does
# not change what the already-loaded LaunchAgent serves.
(
  set -euo pipefail
  ds="$(dirname "$LIB")/deploy-status.sh"
  grep -q 'HOST_ADAPTER_PICKUP="restart-DEV"' "$ds"
  grep -q 'OPENAI_PROXY_LOADED=.*launchctl print' "$ds"
  grep -q '\[ -z "$OPENAI_PROXY_LOADED" \].*\[ -f "$OPENAI_PROXY_PLIST" \]' "$ds"
  grep -q 'openai-gov-proxy|com.unitares.openai-governance-proxy|$HOST_ADAPTER_TREE|src|$HOST_ADAPTER_PICKUP|' "$ds"
) && ok "deploy-status follows the proxy plist and labels pre-migration DEV" \
  || bad "deploy-status OpenAI proxy topology selection"

# Unknown automation stays non-fatal for compatibility, but the operator must
# be told exactly what was not changed. The old "still restart-DEV?" text was
# speculative and omitted the checkout even though JSON already carried both.
(
  set -euo pipefail
  AP="$SB/apply-skip"; mkdir -p "$AP"
  cp "$(dirname "$LIB")/deploy-apply.sh" "$AP/"
  cat > "$AP/deploy-status.sh" <<'EOS'
#!/usr/bin/env bash
printf '[{"name":"mystery","verdict":"BEHIND(2)","checkout":"/tmp/mystery","pickup":"restart-DEV"}]\n'
EOS
  chmod +x "$AP/deploy-status.sh"
  out="$(bash "$AP/deploy-apply.sh" --no-fetch 2>&1)"
  printf '%s' "$out" | grep -q 'no deploy automation registered (pickup=restart-DEV; checkout=/tmp/mystery)'
  printf '%s' "$out" | grep -q 'live process and checkout were left unchanged'
  printf '%s' "$out" | grep -q 'skipped = drift remains; no deploy automation ran'
) && ok "deploy-apply explains an unautomated skip without speculation" \
  || bad "deploy-apply unautomated skip explanation"

# ── deploy-apply holds a shared checkout after a refusal ──
# Seven services deploy from ONE worktree. deploy-mcp.sh refuses on a migration
# gap and rolls that tree back so disk does not sit ahead of a running process --
# and before this guard the very next service in the sweep fast-forwarded the
# same tree again, undoing the rollback. Observed in the deploy tree's reflog on
# 2026-08-20: rollback 02:38:03, undone in the SAME second, and again 02:09:24/25.
# Runs the real deploy-apply.sh against a sandboxed ops dir: fake status, one
# failing service, two more on its tree, one on a tree of its own.
(
  set -euo pipefail
  AP="$SB/apply-ops"; mkdir -p "$AP"
  cp "$(dirname "$LIB")/deploy-apply.sh" "$AP/"
  cat > "$AP/deploy-status.sh" <<'EOS'
#!/usr/bin/env bash
cat <<'J'
[{"name":"governance-mcp","verdict":"BEHIND(3)","branch":"master","commit":"a","behind":"3","pid":"1","pickup":"restart","checkout":"/tmp/shared","health":""},
 {"name":"gateway-mcp","verdict":"BEHIND(3)","branch":"master","commit":"a","behind":"3","pid":"2","pickup":"restart","checkout":"/tmp/shared","health":""},
 {"name":"sentinel-beam","verdict":"STALE(1)","branch":"master","commit":"a","behind":"1","pid":"3","pickup":"restart","checkout":"/tmp/shared","health":""},
 {"name":"discord-bridge","verdict":"BEHIND(1)","branch":"main","commit":"b","behind":"1","pid":"4","pickup":"restart","checkout":"/tmp/own","health":""}]
J
EOS
  printf '#!/usr/bin/env bash\nexit 1\n' > "$AP/deploy-mcp.sh"
  for f in deploy-gateway.sh deploy-sentinel.sh deploy-bridge.sh; do
    printf '#!/usr/bin/env bash\necho ran-%s\n' "$f" > "$AP/$f"
  done
  chmod +x "$AP"/*.sh
  out="$(bash "$AP/deploy-apply.sh" --no-fetch 2>&1)" || true

  # the two siblings on the failed tree must NOT have run. `if`, not
  # `grep && exit`: under set -e a non-matching grep at the end of an && list
  # exits the subshell itself, which reads as a failed assertion.
  if printf '%s' "$out" | grep -q 'ran-deploy-gateway.sh';  then exit 21; fi
  if printf '%s' "$out" | grep -q 'ran-deploy-sentinel.sh'; then exit 22; fi
  # ... and must be reported as held, not silently dropped ...
  printf '%s' "$out" | grep -q 'HOLD  gateway-mcp'  || exit 23
  printf '%s' "$out" | grep -q 'HOLD  sentinel-beam' || exit 24
  # ... while an unrelated tree still deploys (a hold, not a stop-the-world) ...
  printf '%s' "$out" | grep -q 'ran-deploy-bridge.sh' || exit 25
  # ... and the summary still names the real failure (spacing is cosmetic).
  printf '%s' "$out" | grep -qE 'failed: +governance-mcp' || exit 26
) && ok "deploy-apply holds a shared checkout after a refusal (rollback survives)" \
  || bad "deploy-apply shared-checkout hold"

# ── deploy-status --json publishes the checkout deploy-apply keys the hold on ──
# The hold is only as good as the field it reads: if --json stops carrying
# `checkout`, every service reads "" and the hold silently never fires.
(
  set -euo pipefail
  DS="$(dirname "$LIB")/deploy-status.sh"
  grep -q '"checkout":"%s"' "$DS"
  grep -q 'read -r name verdict br sha behindf pidf pickup checkout hz' "$DS"
  grep -q 'svc.get("checkout", "")' "$(dirname "$LIB")/deploy-apply.sh"
  grep -q 'svc.get("pickup", "unknown")' "$(dirname "$LIB")/deploy-apply.sh"
) && ok "[guard] deploy-status --json carries checkout; deploy-apply reads it" \
  || bad "deploy-status/deploy-apply checkout field contract"

# ── restart_service: kickstart-vs-reload by plist content-hash sidecar ──
# launchctl and sleep are shadowed by shell functions inside each subshell:
# no live services touched, and the poll/retry sleeps cost nothing. The fake
# logs every call so assertions read intent, not side effects. Bootstrap's
# attempt counter lives in a FILE because the production code invokes
# `launchctl bootstrap` inside a command substitution (to capture its
# diagnostics), and a variable counter would reset in that subshell.
RS_PLIST="$SB/rs.plist"; printf 'v1\n' > "$RS_PLIST"
_rs_sha() { (command -v shasum >/dev/null 2>&1 && shasum -a 256 "$1" || sha256sum "$1") | awk '{print $1}'; }

# helper: run deploy_lib_restart_service under a scripted fake launchctl.
#   $1 state_dir  $2 log  $3 bootout_rc  $4 bootstrap_fail_count (before
#   success; 99 = never succeeds)  $5 print_rc  $6 kickstart_rc (default 0)
# print_rc models the post-bootout loaded-state poll: nonzero = label gone
# (reload proceeds to bootstrap), 0 = still loaded (refusal after the wait).
_rs_run() {
  local state="$1" log="$2" bo_rc="$3" bs_fail="$4" pr_rc="$5" kick_rc="${6:-0}"
  (
    set -euo pipefail; . "$LIB"
    export UNITARES_DEPLOY_STATE_DIR="$state"
    sleep() { :; }
    launchctl() {
      echo "launchctl $*" >> "$log"
      case "$1" in
        kickstart) return "$kick_rc" ;;
        bootout)   return "$bo_rc" ;;
        bootstrap)
          local n; n="$(cat "$log.bs" 2>/dev/null || echo 0)"; n=$((n+1)); echo "$n" > "$log.bs"
          [ "$n" -gt "$bs_fail" ] && return 0 || return 1 ;;
        print)     return "$pr_rc" ;;
      esac
    }
    deploy_lib_restart_service t gui/501 test.svc "$RS_PLIST"
  )
}

L="$SB/rs1.log"; : > "$L"
if _rs_run "$SB/rs1" "$L" 0 0 1 >/dev/null 2>&1 \
  && grep -q 'launchctl kickstart' "$L" && ! grep -q 'launchctl bootout' "$L" \
  && [[ "$(cat "$SB/rs1/test.svc.plist.sha256")" == "$(_rs_sha "$RS_PLIST")" ]]; then
  ok "restart_service: no baseline -> ADOPT hash + kickstart (absence is not drift)"
else bad "restart_service: adopt-on-missing-baseline path"; fi

L="$SB/rs2.log"; : > "$L"
if _rs_run "$SB/rs1" "$L" 0 0 1 >/dev/null 2>&1 \
  && grep -q 'launchctl kickstart' "$L" && ! grep -q 'launchctl bootout' "$L"; then
  ok "restart_service: sidecar matches -> kickstart, no reload"
else bad "restart_service: unchanged-plist kickstart path"; fi

printf 'v2-edited\n' > "$RS_PLIST"
L="$SB/rs3.log"; : > "$L"
if _rs_run "$SB/rs1" "$L" 0 0 1 >/dev/null 2>&1 \
  && grep -q 'launchctl bootout' "$L" && grep -q 'launchctl bootstrap' "$L" \
  && ! grep -q 'launchctl kickstart' "$L" \
  && [[ "$(cat "$SB/rs1/test.svc.plist.sha256")" == "$(_rs_sha "$RS_PLIST")" ]]; then
  ok "restart_service: edited plist -> RELOAD, sidecar updated"
else bad "restart_service: edited-plist reload path"; fi

mkdir -p "$SB/rs4"; printf 'deadbeef' > "$SB/rs4/test.svc.plist.sha256"
L="$SB/rs4.log"; : > "$L"
if _rs_run "$SB/rs4" "$L" 0 2 1 >/dev/null 2>&1 \
  && [[ "$(grep -c 'launchctl bootstrap' "$L")" == 3 ]]; then
  ok "restart_service: bootstrap retried past the I/O race (2 fails then ok)"
else bad "restart_service: bootstrap retry"; fi

mkdir -p "$SB/rs5"; printf 'deadbeef' > "$SB/rs5/test.svc.plist.sha256"
L="$SB/rs5.log"; : > "$L"
out5="$(_rs_run "$SB/rs5" "$L" 1 99 0 2>&1)"; rc5=$?
if [[ "$rc5" != 0 ]] && ! grep -q 'launchctl kickstart' "$L" \
  && ! grep -q 'launchctl bootstrap' "$L" \
  && printf '%s' "$out5" | grep -q 'did NOT take effect' \
  && [[ "$(cat "$SB/rs5/test.svc.plist.sha256")" == "deadbeef" ]]; then
  ok "restart_service: still loaded after bootout wait -> REFUSE (no kickstart, no false success)"
else bad "restart_service: still-loaded refusal (rc=$rc5)"; fi

mkdir -p "$SB/rs6"; printf 'deadbeef' > "$SB/rs6/test.svc.plist.sha256"
L="$SB/rs6.log"; : > "$L"
out6="$(_rs_run "$SB/rs6" "$L" 0 99 1 2>&1)"; rc6=$?
if [[ "$rc6" != 0 ]] && printf '%s' "$out6" | grep -q 'DOWN'; then
  ok "restart_service: bootstrap dead + not loaded -> hard error (outage is loud)"
else bad "restart_service: outage hard-error (rc=$rc6)"; fi

L="$SB/rs7.log"; : > "$L"
if _rs_run "$SB/rs1" "$L" 0 0 1 7 >/dev/null 2>&1; then
  bad "restart_service: kickstart failure must propagate"
else ok "restart_service: kickstart failure propagates (no swallowed rc)"; fi

touch "$SB/rs8-blocker"
L="$SB/rs8.log"; : > "$L"
out8="$(_rs_run "$SB/rs8-blocker/state" "$L" 0 0 1 2>&1)"; rc8=$?
if [[ "$rc8" == 0 ]] && grep -q 'launchctl kickstart' "$L" \
  && printf '%s' "$out8" | grep -q 'could not record the plist-hash sidecar'; then
  ok "restart_service: unwritable sidecar demotes to WARN, never aborts a working restart"
else bad "restart_service: sidecar-write failure tolerance (rc=$rc8)"; fi

# ── [guard] deploy-mcp.sh actually calls the primitive, in a conditional ──
# The sandbox cases above exercise the library directly; without this pin,
# reverting deploy-mcp.sh to a bare `launchctl kickstart -k` would leave every
# restart_service test green while production silently lost the behavior.
(
  set -euo pipefail
  DM="$(dirname "$LIB")/deploy-mcp.sh"
  grep -q 'if ! deploy_lib_restart_service "\$TAG"' "$DM"
) && ok "[guard] deploy-mcp.sh routes its restart through deploy_lib_restart_service (conditional call)" \
  || bad "deploy-mcp.sh restart wiring guard"

# ── [guard] deploy-lease-plane.sh routes its restart through the primitive ──
# It used a bare `launchctl kickstart -k` until #1945. kickstart reuses
# launchd's cached service definition, so a plist env edit never loaded while
# the script's own health probe still passed — a green deploy over an unchanged
# process. This service is env-gated on several axes, so that is routine.
(
  set -euo pipefail
  DLP="$(dirname "$LIB")/deploy-lease-plane.sh"
  grep -q 'if ! deploy_lib_restart_service "\$TAG"' "$DLP"
  ! grep -q '^launchctl kickstart' "$DLP"
) && ok "[guard] deploy-lease-plane.sh routes its restart through deploy_lib_restart_service (conditional call)" \
  || bad "deploy-lease-plane.sh restart wiring guard"

# ── [guard] deploy-orchestrator.sh gates on the migration manifest ──
# It deploys from its OWN worktree, deliberately unpinned from unitares-deploy —
# which also removed it from deploy-mcp.sh's migration gate without removing its
# dependency on the governance DB schema. #1961: a sweep restarted it on code
# expecting migration 068's table while that table did not exist.
(
  set -euo pipefail
  DO="$(dirname "$LIB")/deploy-orchestrator.sh"
  grep -q 'apply_migrations.py' "$DO"
  grep -q -- '--check' "$DO"
) && ok "[guard] deploy-orchestrator.sh runs a migration preflight before restarting" \
  || bad "deploy-orchestrator.sh migration preflight guard"

echo; echo "passed=$pass failed=$fail"
rm -rf "$SB"
exit "$((fail > 0))"
