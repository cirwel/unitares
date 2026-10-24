#!/usr/bin/env bash
# deploy-status.sh — one-glance view of what's RUNNING vs what's ON DISK across
# the UNITARES ecosystem. Codifies the manual audit (launchctl + git + ps + ports)
# that otherwise has to be re-derived by hand every time someone asks "what's live?".
#
# Stakeholders: an operator reads the table; an agent reads `--json` and verifies
# against it instead of assuming. An explicitly configured inventory supplies
# the services and paths; an unconfigured installation selects no fleet.
#
# Verdicts:
#   CURRENT      running, and its code is not older than the checkout HEAD
#   STALE        running, but checkout HEAD is NEWER than the process start
#                (process is on older — usually merged — code; restart to refresh)
#   BEHIND       checkout itself is behind origin (needs a pull before any restart)
#   GHOST-BRANCH on a branch whose commits are squash-merged into master already
#                (content == master; safe to `checkout master && branch -D`)
#   DOWN         a launchd service that is not currently running
#   LIVE         live-from-checkout (no restart needed; tree is live)
#   n/a          library / Pi-deployed (no local long-running process)
#   PI-CURRENT   (pi-deploy) the Pi itself reports the origin/main tip
#   PI-STALE(Δn) (pi-deploy) the Pi runs n commits behind origin/main
#   PI-DIRTY     (pi-deploy) the Pi's marker says its tree is dirty
#   PI-UNVERIFIED       Pi answered but gave nothing this Mac can verify
#   n/a(Pi:unreachable) ssh probe failed; the row degrades to the LOCAL
#                       checkout, labeled so it cannot read as Pi truth
#
# Footgun flag: ⚠DEV = the service loads from the SHARED dev checkout
# configured by the operator; a restart deploys its checked-out branch.
#
# Flags: --json (machine-readable), --fetch (refresh remotes first; slower).
set -uo pipefail

if [[ "$#" = 1 && "$1" = --capabilities ]]; then
  echo inventory-v1
  exit 0
fi

JSON=0; FETCH=0; LIST_CHECKOUTS=0
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    --list-checkouts) LIST_CHECKOUTS=1 ;;
    --fetch) FETCH=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

OPS_DIR="$(cd "$(dirname "$0")" && pwd)"
INVENTORY="$OPS_DIR/deployment_inventory.py"
if [ "$LIST_CHECKOUTS" = 1 ]; then
  exec python3 "$INVENTORY" --list-checkouts
fi
inventory="$(python3 "$INVENTORY")" || exit 2
COMPONENTS=()
while IFS= read -r component; do
  [ -z "$component" ] || COMPONENTS+=("$component")
done <<< "$inventory"

# --- git helpers (operate on the repo containing repo_path; worktrees ok) -----
base_ref() { # echo origin/master or origin/main
  local d="$1" b
  for b in master main; do
    git -C "$d" rev-parse -q --verify "origin/$b" >/dev/null 2>&1 && { echo "origin/$b"; return; }
  done
  echo "origin/HEAD"
}
git_branch() { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null; }
git_short()  { git -C "$1" rev-parse --short HEAD 2>/dev/null; }
git_head_epoch() { git -C "$1" log -1 --format=%ct 2>/dev/null; }
# behind_count REPO BASE [CPATH]
#
# Scoped to the service's own code path, not the whole repo. Repo-wide was the
# single worst bug in this tool: on a monorepo the shared worktree is almost
# always >=1 commit behind, so EVERY service read BEHIND(n) whether or not the
# commit touched it. Measured 2026-08-13: one commit (#1651, governance_monitor
# only) made six services report BEHIND(1), and `deploy-apply.sh` would have
# restarted all six — including sentinel-beam, which the topology notes say
# never to restart casually, and the fail-closed lease-plane.
#
# It also made CURRENT* — this tool's best idea, "old process but its OWN code
# is unchanged, skip the restart" — structurally unreachable, because the
# override below rewrote it to BEHIND before anyone could read it.
#
# CPATH="." degenerates to repo-wide, which is correct for the services that
# genuinely have no subdir (the Python servers, and live-from-checkout where
# the whole checkout IS the artifact).
behind_count() {
  # Word-split on purpose: cpath is a pathspec LIST (see COMPONENTS). Every
  # value is a single token with no spaces or globs.
  local cpath="${3:-.}"
  git -C "$1" rev-list --count --full-history "HEAD..$2" -- $cpath 2>/dev/null || echo "?"
}
# ghost = HEAD has commits not in base BY SHA, but the trees are identical
is_ghost() {
  local d="$1" base="$2"
  [ -n "$(git -C "$d" log --oneline "$base..HEAD" 2>/dev/null)" ] || return 1
  [ -z "$(git -C "$d" diff --stat "$base..HEAD" 2>/dev/null)" ]
}

proc_pid() { [ -z "$1" ] && return; launchctl list 2>/dev/null | awk -v l="$1" '$3==l && $1!="-"{print $1}'; }
proc_start_epoch() {
  local pid="$1" ls
  ls=$(ps -o lstart= -p "$pid" 2>/dev/null | xargs) || return
  [ -z "$ls" ] && return
  date -j -f "%a %b %d %T %Y" "$ls" +%s 2>/dev/null
}
health() { # port -> short ok/string or ""
  [ -z "$1" ] && return
  local out code body
  out="$(curl -s -m 2 -w '\n%{http_code}' "http://127.0.0.1:$1/health" 2>/dev/null)" || return
  code="${out##*$'\n'}"
  body="${out%$'\n'*}"
  # A bearer-gated /health (agent-orchestrator :8789, lease-plane :8788)
  # answers 401 to this unauthenticated probe. That is PROOF OF LIFE — a dead
  # service refuses the connection, it does not refuse the credential — so say
  # so, instead of pasting `{"error":"permission_denied"...}` into the table
  # where it reads as a failure. The probe stays unauthenticated on purpose:
  # deploy-status must never need a secret to tell you what is running.
  case "$code" in
    401|403) printf 'up (bearer-gated %s)' "$code"; return ;;
  esac
  printf '%s' "$body" | head -c 60
}
# build_sha -> the commit the RUNNING process was started from, as it reports
# itself on /health. Authoritative and timestamp-free, so it is preferred over
# the commit-date heuristic below. Empty when the service exposes no build_sha
# (only some do) — callers must fall back.
build_sha() { # port -> sha or ""
  [ -z "$1" ] && return
  curl -s -m 2 "http://127.0.0.1:$1/health" 2>/dev/null \
    | sed -n 's/.*"build_sha"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]\{7,40\}\)".*/\1/p' \
    | head -1
}

rows=()
for c in ${COMPONENTS+"${COMPONENTS[@]}"}; do
  IFS='|' read -r name label repo subdir pickup port deploy_script status_adapter <<< "$c"
  dir="$repo"; [ -n "$subdir" ] && dir="$repo/$subdir"
  [ "$FETCH" = 1 ] && git -C "$repo" fetch -q origin 2>/dev/null

  br=$(git_branch "$repo"); sha=$(git_short "$repo")
  # One definition of "this service's code", used by BOTH the behind count and
  # the staleness delta below. They disagreed before: delta was path-scoped and
  # behind was repo-wide, so the tool computed the right answer and then threw
  # it away.
  cpath="$subdir"; [ -z "$cpath" ] && cpath="."
  base=$(base_ref "$repo"); behind=$(behind_count "$repo" "$base" "$cpath")
  headep=$(git_head_epoch "$repo")
  ghost="no"; is_ghost "$repo" "$base" && ghost="yes"

  pid=""; start=""; verdict=""; hz=""
  case "$pickup" in
    live-from-checkout) verdict="LIVE" ;;
    library)            verdict="n/a" ;;
    external)
      external="$(python3 "$INVENTORY" --adapter-status "$status_adapter" "$repo" "$base")" || {
        echo "[status] external status unavailable for $name" >&2
        exit 2
      }
      IFS='|' read -r verdict br sha behind hz <<< "$external"
      ;;
    unregistered)
      pid=$(proc_pid "$label")
      verdict="UNGOVERNED"
      hz="no deploy script"
      ;;
    hot-reload)
      # ⛔This branch used to be `up => HOT-RELOAD, else DOWN` and computed no
      # staleness at all, so the lease plane — the ONLY hot-reload row — could
      # never report CURRENT or STALE. Its running-code drift was structurally
      # invisible: the sole staleness signal it ever got was the BEHIND(n)
      # override below, which describes the CHECKOUT being behind origin, not
      # the process being behind the checkout. Measured 2026-08-28: immediately
      # after a full restart onto new code the row still read HOT-RELOAD, and it
      # would have read exactly the same had the restart never happened.
      #
      # Now compares the boot sha the plane publishes on /health, the same way
      # the restart branch does. HOT-RELOAD is kept for the case where the
      # process predates the checkout: on this service that is a legitimate
      # state (modules swapped in place), but it is NOT equivalent to current —
      # `hot-reload.sh` cannot add a child to an already-started supervisor, so
      # any change touching application.ex needs a real restart. The verdict
      # therefore names the delta and the operator decides.
      pid=$(proc_pid "$label")
      if [ -z "$pid" ]; then verdict="DOWN"
      else
        bsha=$(build_sha "$port")
        if [ -n "$bsha" ] && [ -n "$sha" ]; then
          n=${#bsha}; [ "${#sha}" -lt "$n" ] && n=${#sha}
          if [ "${bsha:0:$n}" = "${sha:0:$n}" ]; then verdict="CURRENT"
          else
            delta=$(git -C "$repo" rev-list --count --full-history "$bsha..$base" -- $cpath 2>/dev/null || echo "?")
            [ -z "$delta" ] && delta="?"
            # ⛔STALE(Δn), NOT a HOT-RELOAD(Δn) of its own. deploy-apply.sh
            # selects work with `v.startswith("STALE") or v.startswith("BEHIND")`
            # (deploy-apply.sh:114), so a novel verdict string would be SEEN by
            # the operator and silently ignored by the deployer — the lease
            # plane would stop auto-deploying entirely. Reusing STALE also
            # inherits the existing, tested exclusion from the BEHIND promotion
            # below, which is right for the same reason: "the running process is
            # behind" outranks "your checkout needs a pull". The PICKUP column
            # already says hot-reload, so naming the verdict STALE loses nothing.
            # Δ0 = the shas differ only because the shared monorepo advanced
            # on code this service does not compile. Restarting for that is
            # pointless churn, and now that the hot-reload row feeds
            # deploy-apply it would be pointless churn ON EVERY UNRELATED
            # COMMIT. CURRENT* is the existing spelling for "process is old but
            # its own code is unchanged", used identically by the restart branch.
            if [ "$delta" = "0" ]; then verdict="CURRENT*"; else verdict="STALE(Δ$delta)"; fi
          fi
        else
          # No build_sha on /health: an older lease plane, or a deployment with
          # no .git to resolve one. Say so rather than printing a confident
          # verdict derived from nothing — an unverifiable row must not read as
          # healthy. Deliberately NOT a STALE form: we do not know that it is
          # stale, and a permanently unreadable sha would then re-deploy on
          # every single run. It stays promotable to BEHIND (see below), which
          # preserves exactly the behaviour bare HOT-RELOAD had.
          verdict="HOT-RELOAD(?)"
        fi
      fi ;;
    restart|restart-DEV)
      pid=$(proc_pid "$label")
      if [ -z "$pid" ]; then verdict="DOWN"
      else
        bsha=$(build_sha "$port")
        if [ -n "$bsha" ] && [ -n "$sha" ]; then
          # SHA path: compare what the process says it is running against the
          # checkout. Exact, and immune to the merge-commit trap below.
          n=${#bsha}; [ "${#sha}" -lt "$n" ] && n=${#sha}
          if [ "${bsha:0:$n}" = "${sha:0:$n}" ]; then verdict="CURRENT"
          else
            delta=$(git -C "$repo" rev-list --count --full-history "$bsha..$base" -- $cpath 2>/dev/null || echo "?")
            [ -z "$delta" ] && delta="?"
            # Δ0 = the shas differ only because the shared monorepo advanced
            # on code this service does not compile. Restarting for that is
            # pointless churn, and now that the hot-reload row feeds
            # deploy-apply it would be pointless churn ON EVERY UNRELATED
            # COMMIT. CURRENT* is the existing spelling for "process is old but
            # its own code is unchanged", used identically by the restart branch.
            if [ "$delta" = "0" ]; then verdict="CURRENT*"; else verdict="STALE(Δ$delta)"; fi
          fi
        else
          start=$(proc_start_epoch "$pid")
          if [ -n "$start" ] && [ -n "$headep" ] && [ "$headep" -gt "$start" ]; then
            # No build_sha to compare, so fall back to commit dates: count
            # commits to this service's code path since the process started.
            # 0 => process is old but its code is unchanged (no restart needed).
            #
            # --full-history is REQUIRED. Without it, pathspec history
            # simplification prunes merge commits, and the branch commits
            # underneath carry pre-merge dates that fall outside --since — so a
            # PR merged after the process started counts 0 and reports CURRENT*
            # ("skip restart") while the process runs older code. Measured
            # 2026-08-12: the governance MCP ran build_sha 1ac9912a against a
            # 56728eba checkout and was labelled CURRENT*.
            startiso=$(date -r "$start" "+%Y-%m-%d %H:%M:%S" 2>/dev/null)
            delta=$(git -C "$repo" rev-list --count --full-history --since="$startiso" "$base" -- $cpath 2>/dev/null || echo "?")
            if [ "$delta" = "0" ]; then verdict="CURRENT*"; else verdict="STALE(Δ$delta)"; fi
          else verdict="CURRENT"; fi
        fi
      fi ;;
  esac
  # pi-deploy rows exempt: ghost describes the LOCAL checkout's branch, and
  # letting it overwrite a PI-* verdict would re-introduce exactly the lie
  # the Pi probe exists to fix (local-checkout state rendered as Pi state).
  [ "$ghost" = "yes" ] && [ "$pickup" != "external" ] && verdict="GHOST-BRANCH"
  # BEHIND means "the checkout itself is behind origin — pull before anything
  # else", so it applies to every RUNNING/LIVE verdict, not just CURRENT.
  #
  # Gating this on CURRENT made drift structurally invisible for anything that
  # never reports CURRENT. live-from-checkout is set to LIVE unconditionally, so
  # gov-plugin could sit any number of commits behind origin and still display a
  # reassuring LIVE — measured 2026-08-12 at behind=2, including a release bump.
  # For that service the checkout IS the deployed artifact, so "behind" is the
  # ONLY drift signal that exists; there is no process to be STALE.
  #
  # DOWN and GHOST-BRANCH are left alone: both name a more urgent, different
  # action (start it / discard the branch) than "pull". n/a rows have no repo
  # relationship worth reporting.
  if [ "$behind" != "0" ] && [ "$behind" != "?" ]; then
    case "$verdict" in
      # STALE deliberately NOT in this list. It is the more urgent and more
      # specific fact -- the RUNNING PROCESS is executing superseded code --
      # and letting "your checkout needs a pull" overwrite it meant a
      # STALE(12) service displayed as BEHIND(1). The milder problem hid the
      # sharper one.
      #
      # HOT-RELOAD(?) IS promoted: it means "could not verify", not "the process
      # is behind", so it must not mask the one concrete, actionable fact we do
      # have. This also preserves exactly what bare HOT-RELOAD did before the
      # hot-reload branch learned to compare shas. A hot-reload row that is
      # genuinely behind its checkout reports STALE(Δn) and is preserved by the
      # STALE rule above, like every other service.
      CURRENT|CURRENT\*|LIVE|HOT-RELOAD|HOT-RELOAD\(\?\)) verdict="BEHIND($behind)" ;;
    esac
  fi
  devflag=""; [ "$pickup" = "restart-DEV" ] && devflag=" [DEV]"

  # hz is initialized before the pickup case so a pi-deploy row can preset
  # its note; process rows still overwrite it with the live health probe.
  [ -n "$pid" ] && hz=$(health "$port")

  # $repo, not $dir: this field names the git worktree a deploy fast-forwards.
  # Several services share ONE tree, and a consumer that cannot see the
  # sharing will advance a tree that another service just refused to advance.
  # Kept BEFORE $hz so health remains the greedy tail of every `read` below —
  # a health body containing "|" must not shift a real field.
  rows+=("$name|$verdict$devflag|$br|$sha|behind=$behind|pid=${pid:--}|$pickup|$repo|$deploy_script|$hz")
done

if [ "$JSON" = 1 ]; then
  printf '%s\n' ${rows+"${rows[@]}"} | python3 -c '
import json, sys
keys = ("name", "verdict", "branch", "commit", "behind", "pid", "pickup", "checkout", "deploy_script", "health")
rows=[]
for line in sys.stdin:
    if not line.strip(): continue
    row=dict(zip(keys,line.rstrip("\n").split("|",9)))
    row["behind"]=row["behind"].removeprefix("behind=")
    row["pid"]=row["pid"].removeprefix("pid=")
    rows.append(row)
print(json.dumps(rows))
'
else
  printf '\n  UNITARES deploy status  (%s)\n' "$([ "$FETCH" = 1 ] && echo 'remotes fetched' || echo 'cached remotes — use --fetch to refresh')"
  printf '  %-20s %-16s %-34s %-9s %s\n' "SERVICE" "VERDICT" "BRANCH@COMMIT" "PID" "PICKUP / health"
  printf '  %s\n' "$(printf '%.0s-' {1..96})"
  for r in ${rows+"${rows[@]}"}; do
    IFS='|' read -r name verdict br sha behindf pidf pickup checkout deploy_script hz <<< "$r"
    printf '  %-20s %-16s %-34s %-9s %s %s\n' \
      "$name" "$verdict" "$(echo "$br@$sha" | cut -c1-34)" "${pidf#pid=}" "$pickup" "$hz"
  done
  printf '\n  UNGOVERNED=running, code in a git checkout, NO row here — register it\n'
  printf '  CURRENT ok · CURRENT*=process old but its OWN code unchanged (skip restart)\n'
  printf '  STALE(Δn)=process old AND n commits to its code since (restart) · BEHIND=pull needed\n'
  # [DEV] dropped from the legend: no COMPONENTS row uses restart-DEV, so the
  # verdict cannot print. It survived the condition it was built to describe and
  # was advertising a state the operator can never see — while the actual
  # "loads from a tree deploys rewrite" hazard it once named went unflagged on
  # ipv6-loopback-proxy. The pickup value still works if a row ever sets it.
  printf '  GHOST-BRANCH=content already in master (discard) · DOWN · LIVE\n\n'
fi
