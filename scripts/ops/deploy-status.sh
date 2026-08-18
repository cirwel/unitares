#!/usr/bin/env bash
# deploy-status.sh — one-glance view of what's RUNNING vs what's ON DISK across
# the UNITARES ecosystem. Codifies the manual audit (launchctl + git + ps + ports)
# that otherwise has to be re-derived by hand every time someone asks "what's live?".
#
# Stakeholders: an operator reads the table; an agent reads `--json` and verifies
# against it instead of assuming. Driven by the topology documented in
# ~/.claude .../memory/project_deploy-topology.md — keep the two in sync.
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
#
# Footgun flag: ⚠DEV = the service loads from the SHARED dev checkout
# (~/projects/unitares); a restart deploys whatever branch is checked out there.
#
# Flags: --json (machine-readable), --fetch (refresh remotes first; slower).
set -uo pipefail

JSON=0; FETCH=0
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    --fetch) FETCH=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

H="$HOME"

# See the dispatch-beam rows below. Deliberately a fallback rather than a hard
# path: this table is read by `cirwel status` on every invocation, and a row
# pointing at a worktree that has not been created yet would report a confident
# wrong verdict rather than an honest one.
DISPATCH_TREE="$H/projects/dispatch_beam-deploy"
[ -d "$DISPATCH_TREE" ] || DISPATCH_TREE="$H/projects/dispatch_beam"

# name | launchd-label | repo_path | subdir | pickup | port
# label "" = no launchd service. subdir "" = repo root. port "" = no health probe.
COMPONENTS=(
"governance-mcp|com.unitares.governance-mcp|$H/projects/unitares-deploy||restart|8767"
"gateway-mcp|com.unitares.gateway-mcp|$H/projects/unitares-deploy||restart|8768"
"sentinel-beam|com.unitares.sentinel-beam|$H/projects/unitares-deploy|elixir/sentinel|restart|"
"wave3a-handlers|com.unitares.wave3a-handlers|$H/projects/unitares-deploy|elixir/wave3a_handlers|restart|8770"
"lease-plane|com.unitares.lease-plane|$H/projects/unitares-deploy|elixir/lease_plane|hot-reload|8788"
# Phoenix on :8790, loading from the SHARED deploy worktree. Was absent from
# this table entirely until 2026-08-13, so every ff moved its source under a
# running BEAM with no verdict row to report it. Scoped to its own subdir for
# the same reason sentinel/wave3a are.
"dialectic-live|com.unitares.dialectic-live|$H/projects/unitares-deploy|elixir/dialectic_live|restart|8790"
# Its OWN dedicated worktree, deliberately NOT unitares-deploy: pinning it there
# would couple every orchestrator restart to gov-mcp's migration cadence. Scoped
# to elixir/agent_orchestrator so the verdict tracks ITS code, not the 200+
# unrelated commits the shared tree accumulates — that subdir has not changed
# since 2026-06-29, so this reads CURRENT*, which is the honest answer.
"agent-orchestrator|com.unitares.agent-orchestrator|$H/projects/unitares-orchestrator|elixir/agent_orchestrator|restart|8789"
# Its own repo (cirwel/unitares-discord-bridge) on `main`, and its own deploy
# worktree — base_ref() below already resolves main-vs-master, so the row needs
# no special casing. Pointed here rather than at the dev checkout so the verdict
# describes what deploy-bridge.sh actually deploys.
"discord-bridge|com.unitares.discord-bridge|$H/projects/unitares-discord-bridge-deploy||restart|"
# Both dispatch bots are the same code under two tokens. They serve from a
# pinned deploy worktree once migrate-dispatch-beam-deploy.sh has run, and from
# the dev checkout before that — resolved below so this row never describes a
# path that does not exist on the machine reading it.
"dispatch-beam|com.cirwel.dispatch-beam|$DISPATCH_TREE||restart|"
"dispatch-beam-codex|com.cirwel.dispatch-beam-codex|$DISPATCH_TREE||restart|"
"gov-plugin||$H/projects/unitares-governance-plugin||live-from-checkout|"
# Was "library|" — which renders n/a, documented as "no local long-running
# process". That was FALSE: com.unitares.openai-governance-proxy has run this
# tree for weeks on the :8767 path. A blank row invites a look; a confident
# n/a closes the question, so this was worse than an omission.
"openai-gov-proxy|com.unitares.openai-governance-proxy|$H/projects/unitares-host-adapter|src|restart|"
# Serves from the SHARED deploy worktree that every deploy fast-forwards —
# the same hazard dialectic_live had, found by an adversarial review AFTER
# that one was "fixed". Scoped to the single file it runs. No port: it proxies
# to :8767, and probing that would report the governance MCP's health under
# this proxy's name.
"ipv6-loopback-proxy|com.unitares.ipv6-loopback-proxy|$H/projects/unitares-deploy|scripts/ops/ipv6_loopback_proxy.py|restart|"
"anima-noauth-proxy|com.unitares.anima-noauth-proxy|$H/projects/anima-mcp|scripts/mcp_noauth_proxy.py|restart|"
"anima-proxy|com.unitares.anima-proxy|$H/projects/anima-mcp|scripts/tcp_proxy.py|restart|"
"pi-plugin||$H/projects/unitares-pi-plugin||pi-deploy|"
"anima-mcp||$H/projects/anima-mcp||pi-deploy|"
)

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
  local cpath="${3:-.}"
  git -C "$1" rev-list --count --full-history "HEAD..$2" -- "$cpath" 2>/dev/null || echo "?"
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
for c in "${COMPONENTS[@]}"; do
  IFS='|' read -r name label repo subdir pickup port <<< "$c"
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

  pid=""; start=""; verdict=""
  case "$pickup" in
    live-from-checkout) verdict="LIVE" ;;
    library)            verdict="n/a" ;;
    pi-deploy)          verdict="n/a(Pi)" ;;
    hot-reload)         pid=$(proc_pid "$label"); verdict=$([ -n "$pid" ] && echo "HOT-RELOAD" || echo "DOWN") ;;
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
            delta=$(git -C "$repo" rev-list --count --full-history "$bsha..$base" -- "$cpath" 2>/dev/null || echo "?")
            [ -z "$delta" ] && delta="?"
            verdict="STALE(Δ$delta)"
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
            delta=$(git -C "$repo" rev-list --count --full-history --since="$startiso" "$base" -- "$cpath" 2>/dev/null || echo "?")
            if [ "$delta" = "0" ]; then verdict="CURRENT*"; else verdict="STALE(Δ$delta)"; fi
          else verdict="CURRENT"; fi
        fi
      fi ;;
  esac
  [ "$ghost" = "yes" ] && verdict="GHOST-BRANCH"
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
      CURRENT|CURRENT\*|LIVE|HOT-RELOAD) verdict="BEHIND($behind)" ;;
    esac
  fi
  devflag=""; [ "$pickup" = "restart-DEV" ] && devflag=" [DEV]"

  hz=""; [ -n "$pid" ] && hz=$(health "$port")

  rows+=("$name|$verdict$devflag|$br|$sha|behind=$behind|pid=${pid:--}|$pickup|$hz")
done

# --- derivation: an unregistered service must be LOUD, not absent -----------
# COMPONENTS above encodes INTENT. launchd knows what is actually RUNNING.
# Joining them is the only way "nobody registered this service" becomes a
# finding instead of a silence.
#
# The whole COMPONENTS array is a hand-maintained list of what EXISTS, and a
# hand-maintained list of what exists is exactly the thing that was wrong:
# dialectic_live served traffic for months while absent from this table, and
# after that was "fixed" an adversarial review immediately found
# ipv6-loopback-proxy in the identical state — running from the SHARED deploy
# worktree, unlisted. Two instances of one class, found one at a time, because
# a missing row is indistinguishable from a healthy fleet.
#
# So this pass asserts absence. A live launchd job whose code resolves into a
# git checkout, with no COMPONENTS row, prints as UNGOVERNED. Registering it
# is then a deliberate act, and forgetting is no longer quiet.
#
# Scope is deliberately narrow to stay signal: RUNNING only (a job with no PID
# is periodic/cron, not a service), and only when the plist actually resolves
# to a git work tree (Homebrew binaries and system paths resolve to nothing and
# are skipped rather than becoming permanent noise you learn to ignore).
# Stubbable so the derivation can be tested without launchd, the same way
# bridge_liveness_watchdog.sh stubs its restart and PID probes. A guard nobody
# can test is a guard nobody can trust — and this one exists precisely because
# an untested absence went unnoticed for months.
LAUNCHCTL_LIST_CMD="${LAUNCHCTL_LIST_CMD:-launchctl list}"

ungoverned_rows() {
  local known label pid st path
  known="$(for c in "${COMPONENTS[@]}"; do IFS='|' read -r _ l _ <<< "$c"; [ -n "$l" ] && printf '%s\n' "$l"; done)"
  while IFS=$'\t' read -r pid st label; do
    case "$label" in com.unitares.*|com.cirwel.*) ;; *) continue ;; esac
    [ "$pid" = "-" ] && continue
    printf '%s\n' "$known" | grep -qxF "$label" && continue
    [ -f "$HOME/Library/LaunchAgents/$label.plist" ] || continue
    path=$(grep -o "$HOME/projects/[A-Za-z0-9_.-]*" "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null | head -1)
    [ -n "$path" ] || continue
    git -C "$path" rev-parse --git-dir >/dev/null 2>&1 || continue
    rows+=("${label#com.*.}|UNGOVERNED|$(git_branch "$path")|$(git_short "$path")|behind=?|pid=$pid|unregistered|no deploy script")
  done < <(eval "$LAUNCHCTL_LIST_CMD" 2>/dev/null)
}
ungoverned_rows


if [ "$JSON" = 1 ]; then
  printf '['
  first=1
  for r in "${rows[@]}"; do
    IFS='|' read -r name verdict br sha behindf pidf pickup hz <<< "$r"
    [ "$first" = 1 ] || printf ','; first=0
    # behindf/pidf carry "behind=N"/"pid=X"; strip the prefixes so the JSON has
    # real keys (the previous form emitted keyless values → invalid JSON, which
    # broke any agent trying to parse --json as the header promises).
    printf '{"name":"%s","verdict":"%s","branch":"%s","commit":"%s","behind":"%s","pid":"%s","pickup":"%s","health":"%s"}' \
      "$name" "$verdict" "$br" "$sha" "${behindf#behind=}" "${pidf#pid=}" "$pickup" "$(echo "$hz" | tr -d '"\\')"
  done
  printf ']\n'
else
  printf '\n  UNITARES deploy status  (%s)\n' "$([ "$FETCH" = 1 ] && echo 'remotes fetched' || echo 'cached remotes — use --fetch to refresh')"
  printf '  %-20s %-16s %-34s %-9s %s\n' "SERVICE" "VERDICT" "BRANCH@COMMIT" "PID" "PICKUP / health"
  printf '  %s\n' "$(printf '%.0s-' {1..96})"
  for r in "${rows[@]}"; do
    IFS='|' read -r name verdict br sha behindf pidf pickup hz <<< "$r"
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
