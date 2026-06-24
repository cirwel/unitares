#!/bin/bash
# Repo scope guard — keeps the user/agent-agnostic unitares repo free of
# operator-personal material, career artifacts, and per-vendor agent config.
#
# Why this exists: unitares is meant to be user- and agent-agnostic (see
# docs/REPO_SCOPE.md). Personal/career files and tool-specific config have
# leaked in before — the FRT career demo under demos/, and PR #1039's committed
# .claude/settings.json (an attribution-stripping config force-whitelisted past
# .gitignore). Neither memory nor per-vendor instructions reliably prevent this,
# so this is a hard, vendor-neutral gate. It runs in the pre-commit hook AND in
# CI; CI is the layer that catches cloud/web sessions, which bypass local hooks.
#
# Usage:
#   check-repo-scope.sh --staged          # added/changed files in the index (pre-commit)
#   check-repo-scope.sh --base <ref>      # files changed vs <ref> (CI; e.g. origin/master)
#   check-repo-scope.sh --files <f...>    # explicit file list
#
# Escape hatch for a legitimate match: add the path (one glob per line) to
# scripts/dev/repo-scope-allow.txt, or commit locally with --no-verify.

set -uo pipefail
PROJECT_ROOT="$(git rev-parse --show-toplevel)"
cd "$PROJECT_ROOT" || exit 2

ALLOW_FILE="scripts/dev/repo-scope-allow.txt"

# Files that legitimately contain the trigger words (this guard and its docs).
is_self() {
  case "$1" in
    scripts/dev/check-repo-scope.sh|\
    scripts/dev/repo-scope-allow.txt|\
    docs/REPO_SCOPE.md|\
    .github/workflows/repo-scope.yml) return 0;;
  esac
  return 1
}

is_allowed() {
  [ -f "$ALLOW_FILE" ] || return 1
  local pat
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    case "$pat" in \#*) continue;; esac
    # shellcheck disable=SC2053
    [[ "$1" == $pat ]] && return 0
  done < "$ALLOW_FILE"
  return 1
}

mode="--staged"; base="origin/master"; files=""
case "${1:-}" in
  --staged|"") mode="--staged";;
  --base) mode="--base"; base="${2:-origin/master}";;
  --files) shift; files="$(printf '%s\n' "$@")"; mode="--files";;
  *) echo "unknown arg: ${1}"; exit 2;;
esac

collect() {
  case "$mode" in
    --files) printf '%s\n' "$files";;
    --base)  git diff --name-only --diff-filter=ACMR "${base}...HEAD" 2>/dev/null \
               || git diff --name-only --diff-filter=ACMR "$base" 2>/dev/null;;
    *)       git diff --cached --name-only --diff-filter=ACMR;;
  esac
}

violations=0
report() { echo "  ❌ $1"; echo "     → $2"; violations=$((violations + 1)); }

if [ "$mode" = "--base" ]; then echo "🔍 Repo scope guard (--base $base)..."; else echo "🔍 Repo scope guard ($mode)..."; fi

# Rule 0: .gitignore must not re-include a vendor agent-config dir (the #1039 vector).
if grep -nE '^[[:space:]]*!\.(claude|codex|cursor|aider|continue)' .gitignore 2>/dev/null; then
  report ".gitignore re-includes a vendor agent-config dir" \
    "Keep .claude/.codex/.cursor fully ignored — never whitelist tool config into the repo."
fi

FILES="$(collect | sort -u)"
while IFS= read -r f; do
  [ -z "$f" ] && continue
  is_self "$f" && continue
  is_allowed "$f" && continue
  bn="$(basename "$f")"

  # Rule 1: tracked vendor agent/tool config dirs.
  if [[ "$f" =~ ^\.(claude|codex|cursor|aider|continue)(/|$) ]]; then
    report "$f" "Vendor agent/tool config is machine-local — never commit it to the agnostic repo."
    continue
  fi

  # Rule 2: career / personal artifacts by name or path segment.
  if [[ "$bn" =~ [Rr]esume ]] || \
     [[ "$bn" =~ [Cc]over[-_]?[Ll]etter ]] || \
     [[ "/$f" =~ /[Cc]areer/ ]] || \
     [[ "/$f" =~ /[Jj]ob[-_][Aa]pplication ]] || \
     [[ "$bn" =~ ^[Ff][Rr][Tt][_-] ]] || \
     [[ "/$f" =~ /[Ff][Rr][Tt][_-] ]]; then
    report "$f" "Looks like a career/personal artifact — belongs in ~/career, not the product repo."
    continue
  fi

  # Rule 3: content checks (text files only; -I skips binaries).
  [ -f "$f" ] || continue
  if grep -nIE 'hikewa@gmail\.com' "$f" >/dev/null 2>&1; then
    report "$f" "Contains a personal job-application email — keep personal contact info out of the repo."
  fi
  if grep -nIE '"includeCoAuthoredBy"|"attribution"[[:space:]]*:' "$f" >/dev/null 2>&1; then
    report "$f" "Per-vendor attribution config belongs in local ~/.claude config, not the agnostic repo."
  fi
done <<EOF
$FILES
EOF

if [ "$violations" -gt 0 ]; then
  echo ""
  echo "❌ Repo scope guard: $violations issue(s). unitares is user/agent-agnostic — see docs/REPO_SCOPE.md."
  echo "   Move the file out (e.g. ~/career), strip the content, or allowlist it in $ALLOW_FILE."
  exit 1
fi

echo "✅ Repo scope guard: clean"
