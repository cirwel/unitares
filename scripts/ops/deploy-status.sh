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
# name | launchd-label | repo_path | subdir | pickup | port
# label "" = no launchd service. subdir "" = repo root. port "" = no health probe.
COMPONENTS=(
"governance-mcp|com.unitares.governance-mcp|$H/projects/unitares-deploy||restart|8767"
"gateway-mcp|com.unitares.gateway-mcp|$H/projects/unitares||restart-DEV|"
"sentinel-beam|com.unitares.sentinel-beam|$H/projects/unitares|elixir/sentinel|restart-DEV|"
"wave3a-handlers|com.unitares.wave3a-handlers|$H/projects/unitares|elixir/wave3a_handlers|restart-DEV|8770"
"lease-plane|com.unitares.lease-plane|$H/projects/unitares-deploy|elixir/lease_plane|hot-reload|"
"discord-bridge|com.unitares.discord-bridge|$H/projects/unitares-discord-bridge||restart|"
"dispatch-beam|com.cirwel.dispatch-beam|$H/projects/dispatch_beam||restart|"
"dispatch-beam-codex|com.cirwel.dispatch-beam-codex|$H/projects/dispatch_beam||restart|"
"gov-plugin||$H/projects/unitares-governance-plugin||live-from-checkout|"
"host-adapter||$H/projects/unitares-host-adapter||library|"
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
behind_count() { git -C "$1" rev-list --count "HEAD..$2" 2>/dev/null || echo "?"; }
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
  curl -s -m 2 "http://127.0.0.1:$1/health" 2>/dev/null | head -c 60
}

rows=()
for c in "${COMPONENTS[@]}"; do
  IFS='|' read -r name label repo subdir pickup port <<< "$c"
  dir="$repo"; [ -n "$subdir" ] && dir="$repo/$subdir"
  [ "$FETCH" = 1 ] && git -C "$repo" fetch -q origin 2>/dev/null

  br=$(git_branch "$repo"); sha=$(git_short "$repo")
  base=$(base_ref "$repo"); behind=$(behind_count "$repo" "$base")
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
        start=$(proc_start_epoch "$pid")
        if [ -n "$start" ] && [ -n "$headep" ] && [ "$headep" -gt "$start" ]; then
          # process predates checkout HEAD — but is its OWN code actually newer?
          # Count commits to this service's code path since the process started;
          # 0 => process is old but its code is unchanged (no restart needed).
          startiso=$(date -r "$start" "+%Y-%m-%d %H:%M:%S" 2>/dev/null)
          cpath="$subdir"; [ -z "$cpath" ] && cpath="."
          delta=$(git -C "$repo" rev-list --count --since="$startiso" "$base" -- "$cpath" 2>/dev/null || echo "?")
          if [ "$delta" = "0" ]; then verdict="CURRENT*"; else verdict="STALE(Δ$delta)"; fi
        else verdict="CURRENT"; fi
      fi ;;
  esac
  [ "$ghost" = "yes" ] && verdict="GHOST-BRANCH"
  [ "$behind" != "0" ] && [ "$behind" != "?" ] && [ "$verdict" = "CURRENT" ] && verdict="BEHIND($behind)"
  devflag=""; [ "$pickup" = "restart-DEV" ] && devflag=" [DEV]"

  hz=""; [ -n "$pid" ] && hz=$(health "$port")

  rows+=("$name|$verdict$devflag|$br|$sha|behind=$behind|pid=${pid:--}|$pickup|$hz")
done

if [ "$JSON" = 1 ]; then
  printf '['
  first=1
  for r in "${rows[@]}"; do
    IFS='|' read -r name verdict br sha behindf pidf pickup hz <<< "$r"
    [ "$first" = 1 ] || printf ','; first=0
    printf '{"name":"%s","verdict":"%s","branch":"%s","commit":"%s","%s","%s","pickup":"%s","health":"%s"}' \
      "$name" "$verdict" "$br" "$sha" "$behindf" "$pidf" "$pickup" "$(echo "$hz" | tr -d '"')"
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
  printf '\n  CURRENT ok · CURRENT*=process old but its OWN code unchanged (skip restart)\n'
  printf '  STALE(Δn)=process old AND n commits to its code since (restart) · BEHIND=pull needed\n'
  printf '  GHOST-BRANCH=content already in master (discard) · DOWN · LIVE · [DEV]=loads from shared dev checkout\n\n'
fi
