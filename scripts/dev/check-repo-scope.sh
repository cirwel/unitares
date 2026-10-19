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

# Detection patterns are GENERAL, never one contributor's identity: a guard that
# names the person it protects publishes exactly what it exists to keep out.
#
# Personal webmail address (Rule 3). Provider list, not a person.
WEBMAIL_RE='[A-Za-z0-9._%+-]+@(gmail|googlemail|yahoo|hotmail|outlook|live|icloud|proton|protonmail)\.(com|me)([^A-Za-z0-9_-]|$)'
# Machine-absolute macOS home path (Rule 8): /Users/<name>, ending on a
# non-dot so a sentence-final "/Users/you." still reads as the name "you".
# .github/workflows/repo-scope.yml carries the same two patterns for the PR
# body, and tests/test_doc_contract_ci_wiring.py holds them identical.
HOME_PATH_RE='/Users/[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9_-])?'
# Names that are illustrative, not anybody's account: docs placeholders, test
# fixtures (one letter, testuser, op), the GitHub macOS runner, and macOS's own
# /Users/Shared. A templated /Users/<name> or /Users/$USER never matches above.
HOME_PLACEHOLDER_RE='^/Users/([A-Za-z0-9]|you|me|user|username|name|alice|bob|example|test|testuser|foo|runner|operator|op|someone|Shared)$'

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
  # "resume"/"cv" only count as a CV artifact when they're the trailing word of a
  # *document* name (resume.pdf, jane-resume.docx, cv.md) — NOT the verb "resume"
  # buried in technical names (lifecycle/resume.py, identity-resume-v0.md), which
  # were false-flagged before.
  cv_artifact=0
  case "${bn##*.}" in
    py|js|ts|tsx|jsx|mjs|cjs|ex|exs|go|rs|rb|java|kt|c|cc|cpp|h|hpp|sh|bash|zsh|sql|yaml|yml|toml|json|lock|cfg|ini|env|mod|tf) ;;  # code/config — never a résumé
    *) [[ "$bn" =~ (^|[-_])([Rr][eé]sum[eé]|[Cc][Vv])(\.[A-Za-z0-9]+)?$ ]] && cv_artifact=1 ;;
  esac
  if [[ "$cv_artifact" == 1 ]] || \
     [[ "$bn" =~ [Cc]over[-_]?[Ll]etter ]] || \
     [[ "/$f" =~ /[Cc]areer/ ]] || \
     [[ "/$f" =~ /[Jj]ob[-_][Aa]pplication ]] || \
     [[ "$bn" =~ ^[Ff][Rr][Tt][_-] ]] || \
     [[ "/$f" =~ /[Ff][Rr][Tt][_-] ]]; then
    report "$f" "Looks like a career/personal artifact — belongs outside the product repo."
    continue
  fi

  # Rule 3: content checks (text files only; -I skips binaries).
  [ -f "$f" ] || continue
  if grep -niIE "$WEBMAIL_RE" "$f" >/dev/null 2>&1; then
    report "$f" "Contains a personal webmail address — keep personal contact info out of the repo; use the project's public contact surfaces, or user@example.com in an example."
  fi
  if grep -nIE '"includeCoAuthoredBy"|"attribution"[[:space:]]*:' "$f" >/dev/null 2>&1; then
    report "$f" "Per-vendor attribution config belongs in local ~/.claude config, not the agnostic repo."
  fi

  # Rule 5: Claude session/deliberation register leaking into committed content.
  # Public docs/code should read as product engineering, not as a continued chat
  # session or an AI-review scratchpad. (Codex-authored content does not carry
  # this register; this catches the Claude-specific pattern.) Tight, near-zero-
  # false-positive markers ONLY — never bare "operator"/"council", which are real
  # product vocab (src/mcp_handlers/identity/operator.py, the dialectic system).
  # Stored dialectic session data under src/data/ is exempt.
  case "$f" in
    src/data/*) ;;
    *)
      if grep -nIE 'per your guidance|your overlay|you flagged|questions for [Kk]enny' "$f" >/dev/null 2>&1; then
        report "$f" "Addresses the operator in second person (chat-session register) — product docs/PRs are third-person/imperative, not a reply to one operator."
      fi
      # Leading capitals and hyphen-or-space, so "Council pass", "Live-verifier"
      # and "council-folded" match too. Character classes rather than -i keep
      # these alternatives in the form tests/test_doc_contract_ci_wiring.py parses.
      if grep -nIE '[Ll]ive[- ]verifier|[Cc]ouncil[- ]pass|[Cc]ouncil[- ]fold|three-lane council' "$f" >/dev/null 2>&1; then
        report "$f" "Exposes the AI-review process (council/live-verifier) — internal deliberation, not product content. Keep working notes outside the product checkout and ship clean docs."
      fi
      ;;
  esac

  # Rule 6: a metered model-API on the REQUIRED default path. unitares is
  # user/agent-agnostic and must run free/self-hosted with no paid key — so a
  # metered API (Anthropic / OpenAI / ...) must never be FORCED on every
  # installer. Metered models are welcome as an OPT-IN, off-by-default backend:
  # a config-driven `base_url` (env override) is NOT flagged, and the existing
  # `openai` client (drives LOCAL Ollama) and orchestrator-spawned `claude` CLI
  # pass. We flag only the "forces it on everyone" signals — and an intentional
  # opt-in metered backend can register in repo-scope-allow.txt.
  if [[ "$f" == .github/workflows/*.yml || "$f" == .github/workflows/*.yaml ]]; then
    if grep -nIE 'anthropics/claude-code-action' "$f" >/dev/null 2>&1; then
      report "$f" "CI uses anthropics/claude-code-action — a metered model API forced on everyone who runs CI. Keep CI free-runnable (GITHUB_TOKEN-only / local Ollama); a metered CI step must be opt-in, not the default."
    fi
  fi
  case "$bn" in
    *.py)
      if grep -nIE '^[[:space:]]*(import|from)[[:space:]]+anthropic([[:space:].]|$)' "$f" >/dev/null 2>&1; then
        report "$f" "Imports the anthropic SDK (no local fallback) — a metered dependency on the default path. Core must run free/self-hosted; expose metered models as an opt-in, off-by-default backend (config-gated), then allowlist this file in scripts/dev/repo-scope-allow.txt."
      fi
      ;;
  esac
  case "$bn" in
    *.py|*.sh|*.bash|*.yml|*.yaml|*.toml)
      if grep -nIE 'api\.(openai|anthropic)\.com' "$f" >/dev/null 2>&1; then
        report "$f" "Hardcodes a metered model cloud endpoint (api.openai.com / api.anthropic.com) — that forces a paid API on every installer. Make base_url config-driven (env) so metered stays opt-in, or allowlist a deliberate opt-in backend."
      fi
      ;;
  esac

  # Rule 7: harness-memory file names (feedback_*.md / project_*.md) cited as
  # authority. That memory is machine-local; cite the repo artifact or the KG
  # entry instead. Same exemption as Rule 5.
  case "$f" in
    src/data/*) ;;
    *)
      # A bare name, or any path through a memory/ directory. Other paths are
      # not flagged, so a repo file such as docs/project_layout.md passes.
      if grep -nIE '(^|[^A-Za-z0-9/_])(feedback|project)_[a-z0-9_-]+\.md|memory/(feedback|project)_[a-z0-9_-]+\.md' "$f" >/dev/null 2>&1; then
        report "$f" "Cites a harness-memory file (feedback_*/project_*.md) — that memory is machine-local. Cite the repo artifact or the knowledge-graph entry instead."
      fi
      ;;
  esac

  # Rule 8: a machine-absolute home path (/Users/<name>/...) for any account
  # other than a placeholder. Same exemption as Rule 5. Its own rule, not a
  # Rule 5 alternative, because the placeholder exclusion is a second filter.
  case "$f" in
    src/data/*) ;;
    *)
      leaked_homes="$(grep -ohIE "$HOME_PATH_RE" "$f" 2>/dev/null | grep -viE "$HOME_PLACEHOLDER_RE" | sort -u | tr '\n' ' ')"
      if [ -n "$leaked_homes" ]; then
        report "$f" "Leaks a machine-absolute home path (${leaked_homes% }) — use ~, \$HOME, an env var, or a placeholder such as /Users/you."
      fi
      ;;
  esac
done <<EOF
$FILES
EOF

# Rule 4: the per-class config dicts must use only generic behavior classes —
# never named residents. The 2026-06-27 anchor recalibration baked one
# deployment's residents (Lumen/Vigil/Sentinel/...) with their measured EISV
# operating points into this user-agnostic repo; named residents belong in the
# deployment-local UNITARES_CLASS_CALIBRATION overlay. Allowlist-based (AST, no
# import) so a NEW resident name is caught too, not just the known set.
CFG="config/governance_config.py"
if printf '%s\n' "$FILES" | grep -qx "$CFG" && [ -f "$CFG" ] && ! is_allowed "$CFG" && command -v python3 >/dev/null 2>&1; then
  leaked="$(python3 - "$CFG" <<'PY'
import ast, sys
ALLOWED = {"embodied", "resident_persistent", "engaged_ephemeral", "ephemeral", "default"}
TARGETS = {"DELTA_NORM_MAX_BY_CLASS", "HEALTHY_OPERATING_POINT_BY_CLASS", "VOID_THRESHOLD_BY_CLASS"}
bad = set()
def keys_of(node):
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value not in ALLOWED:
                bad.add(k.value)
try:
    tree = ast.parse(open(sys.argv[1]).read())
except Exception:
    sys.exit(0)  # parse failure is handled elsewhere, not here
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in TARGETS:
                keys_of(node.value)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
            and node.target.id in TARGETS and node.value is not None:
        keys_of(node.value)
print(",".join(sorted(bad)))
PY
)"
  if [ -n "$leaked" ]; then
    report "$CFG" "Named-resident keys in class-conditional dicts: ${leaked}. unitares is user-agnostic — use generic behavior classes (embodied/resident_persistent/engaged_ephemeral/ephemeral/default) and put named residents in the deployment-local UNITARES_CLASS_CALIBRATION overlay."
  fi
fi

if [ "$violations" -gt 0 ]; then
  echo ""
  echo "❌ Repo scope guard: $violations issue(s). unitares is user/agent-agnostic — see docs/REPO_SCOPE.md."
  echo "   Move the file out of the repo, strip the content, or allowlist it in $ALLOW_FILE."
  exit 1
fi

echo "✅ Repo scope guard: clean"
