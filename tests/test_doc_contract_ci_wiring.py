"""Docs that describe CI wiring must match the wiring itself.

Three prose claims about CI have drifted from the code before: the release
checklist's `git log ... --` pathspec stopped matching the Docker-quickstart
workflow's path filter (it missed `scripts/ci/plugin_file_lease_proof.py`), the
repo-scope doc listed a review-process phrase as flagged in PR bodies when only
the file check carries it, and a drift-ledger row named a `PLUGIN_SYNC_TOKEN` secret
no workflow reads. The doc-health lint is a denylist of known-bad phrasings and
cannot see this class, so this test parses the sources of truth instead
(`.github/workflows/docker-quickstart.yml` `on.*.paths`, the PR-body grep in
`.github/workflows/repo-scope.yml`, the Rule 5 greps in
`scripts/dev/check-repo-scope.sh`, and every `secrets.*`/`vars.*` reference
under `.github/`) and holds the living docs to them by exact match on the
structured claim.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"
_QUICKSTART_WF = ".github/workflows/docker-quickstart.yml"
_REPO_SCOPE_WF = ".github/workflows/repo-scope.yml"
_REPO_SCOPE_SH = "scripts/dev/check-repo-scope.sh"
_RELEASE_DOC = "docs/operations/RELEASE_PROCESS.md"
_SCOPE_DOC = "docs/REPO_SCOPE.md"


@pytest.fixture(scope="module")
def living_docs() -> list[Path]:
    spec = importlib.util.spec_from_file_location(
        "check_doc_health_ci_wiring", _DOC_HEALTH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect_md_files()


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --- (a) quickstart path filter <-> documented `git log -- <pathspec>` ------


def _normalize_pathspec(entry: str) -> str:
    """Map a workflow `paths:` glob and a git pathspec to one comparable form.

    `db/postgres/**` (workflow) and `db/postgres` (git pathspec) select the same
    tree; quotes are shell syntax. Anything else must already match verbatim.
    """
    entry = entry.strip().strip("'\"")
    if entry.endswith("/**"):
        entry = entry[: -len("/**")]
    return entry.rstrip("/")


def quickstart_path_filters() -> dict[str, set[str]]:
    data = yaml.safe_load((REPO_ROOT / _QUICKSTART_WF).read_text())
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    triggers = data.get("on", data.get(True))
    filters = {}
    for event in ("push", "pull_request"):
        raw = triggers[event]["paths"]
        normalized = {_normalize_pathspec(p) for p in raw}
        untranslatable = sorted(p for p in normalized if "**" in p)
        assert not untranslatable, (
            f"{_QUICKSTART_WF} on.{event}.paths has globs {untranslatable} that "
            "this test cannot map to a git pathspec; extend _normalize_pathspec."
        )
        filters[event] = normalized
    return filters


_GIT_LOG_SPAN = re.compile(r"`(git log [^`]*? -- ([^`]*))`")


def check_git_log_pathspecs(rel: str, text: str, workflow_paths: set[str]) -> list[str]:
    """Flag a backticked `git log ... -- <paths>` that targets the quickstart
    workflow (its pathspec names the workflow file) but whose path set differs
    from the workflow's `paths:` filter."""
    failures = []
    for match in _GIT_LOG_SPAN.finditer(text):
        documented = {_normalize_pathspec(p) for p in match.group(2).split()}
        if _QUICKSTART_WF not in documented:
            continue
        if documented == workflow_paths:
            continue
        missing = sorted(workflow_paths - documented)
        extra = sorted(documented - workflow_paths)
        failures.append(
            f"{rel}:{_line_of(text, match.start())}: the `git log -- <pathspec>` "
            f"range check claims to cover the Docker-quickstart build inputs, but "
            f"differs from {_QUICKSTART_WF} on.push/pull_request.paths "
            f"(missing from doc: {missing}; not in workflow: {extra})"
        )
    return failures


def test_quickstart_push_and_pr_filters_agree():
    filters = quickstart_path_filters()
    assert filters["push"] == filters["pull_request"], (
        f"{_QUICKSTART_WF}: on.push.paths and on.pull_request.paths differ "
        f"(push only: {sorted(filters['push'] - filters['pull_request'])}; "
        f"pull_request only: {sorted(filters['pull_request'] - filters['push'])})"
    )


def test_documented_quickstart_pathspec_matches_workflow_filter(living_docs):
    workflow_paths = quickstart_path_filters()["push"]
    failures, anchored = [], set()
    for path in living_docs:
        text = path.read_text(errors="replace")
        rel = _rel(path)
        if any(_QUICKSTART_WF in m.group(2) for m in _GIT_LOG_SPAN.finditer(text)):
            anchored.add(rel)
        failures += check_git_log_pathspecs(rel, text, workflow_paths)
    assert _RELEASE_DOC in anchored, (
        f"{_RELEASE_DOC} no longer carries a backticked `git log ... -- ... "
        f"{_QUICKSTART_WF}` range check; this test would pass vacuously."
    )
    assert not failures, "\n".join(failures)


def test_pathspec_check_flags_the_missing_lease_proof_entry():
    # origin/master RELEASE_PROCESS.md:83 before the audit correction.
    stale = (
        "   `git log --oneline vLAST..origin/master -- Dockerfile 'requirements*.txt' "
        "constraints.txt docker-compose.yml db/postgres elixir/lease_plane "
        "elixir/unitares_sdk scripts/demo/quick_demo.py scripts/demo/coordination_demo.py "
        "scripts/ci/check_mcp_tool_surface.py .github/workflows/docker-quickstart.yml`.\n"
    )
    workflow_paths = quickstart_path_filters()["push"]
    failures = check_git_log_pathspecs(_RELEASE_DOC, stale, workflow_paths)
    assert len(failures) == 1
    assert "scripts/ci/plugin_file_lease_proof.py" in failures[0]
    assert failures[0].startswith(f"{_RELEASE_DOC}:1:")


# --- (b) repo-scope register phrases: doc <-> PR-body regex <-> file regex ---


def _alternatives(patterns: list[str]) -> set[str]:
    return {alt for pattern in patterns for alt in pattern.split("|")}


def pr_body_register_alternatives() -> set[str]:
    """Every `grep -niE` alternative the PR-body step applies (path + register)."""
    data = yaml.safe_load((REPO_ROOT / _REPO_SCOPE_WF).read_text())
    runs = [
        step.get("run", "")
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if "bad_register=" in step.get("run", "")
    ]
    assert len(runs) == 1, f"{_REPO_SCOPE_WF}: expected one PR-body register step"
    patterns = re.findall(r"grep -niE '([^']*)'", runs[0])
    assert patterns, f"{_REPO_SCOPE_WF}: no `grep -niE` patterns in the PR-body step"
    return _alternatives(patterns)


def file_register_alternatives() -> set[str]:
    """Every `grep -nIE` alternative in check-repo-scope.sh Rule 5."""
    text = (REPO_ROOT / _REPO_SCOPE_SH).read_text()
    block = re.search(r"# Rule 5:.*?# Rule 6:", text, re.S)
    assert block, f"{_REPO_SCOPE_SH}: Rule 5 block not found"
    patterns = re.findall(r"grep -nIE '([^']*)'", block.group(0))
    assert patterns, f"{_REPO_SCOPE_SH}: no `grep -nIE` patterns in Rule 5"
    return _alternatives(patterns)


_REGISTER_BULLET = re.compile(
    r"^- \*\*Chat-session / AI-deliberation register\*\*.*?(?=^\S|^- |\Z)",
    re.S | re.M,
)
_SCOPE_QUALIFIERS = {"in files only": "files", "in PR bodies only": "pr"}


def documented_register_phrases(text: str) -> list[tuple[str, str, int]]:
    """(phrase, claimed scope, line) for each backticked phrase the doc says the
    guard flags. Scope is `both` unless the phrase is introduced by an
    "in files only" / "in PR bodies only" qualifier."""
    bullet = _REGISTER_BULLET.search(text)
    if not bullet:
        return []
    body_start = text.find("The guard flags", bullet.start(), bullet.end())
    body_end = text.find("It deliberately", body_start, bullet.end())
    if body_start < 0 or body_end < 0:
        return []
    phrases, cursor = [], body_start
    for match in re.finditer(r"`([^`]+)`", text[body_start:body_end]):
        start = body_start + match.start()
        between = text[cursor:start]
        scope = next(
            (s for q, s in _SCOPE_QUALIFIERS.items() if q in " ".join(between.split())),
            "both",
        )
        phrases.append((match.group(1), scope, _line_of(text, start)))
        cursor = body_start + match.end()
    return phrases


def check_register_phrases(
    rel: str, text: str, file_alts: set[str], pr_alts: set[str]
) -> list[str]:
    phrases = documented_register_phrases(text)
    if not phrases:
        return [f"{rel}: the 'Chat-session / AI-deliberation register' phrase list was not found"]
    sources = f"{_REPO_SCOPE_SH} Rule 5 and {_REPO_SCOPE_WF} PR-body step"
    failures, covered = [], set()
    for phrase, claimed, line in phrases:
        in_files = {a for a in file_alts if re.fullmatch(a, phrase, re.I)}
        in_pr = {a for a in pr_alts if re.fullmatch(a, phrase, re.I)}
        covered |= in_files | in_pr
        actual = (
            "both" if in_files and in_pr
            else "files" if in_files
            else "pr" if in_pr
            else None
        )
        if actual is None:
            failures.append(
                f"{rel}:{line}: documents `{phrase}` as a flagged register phrase, "
                f"but no pattern in {sources} matches it"
            )
        elif actual != claimed:
            failures.append(
                f"{rel}:{line}: documents `{phrase}` as flagged in "
                f"{claimed!r}, but it is flagged in {actual!r} "
                f"(source: {sources}; file alternatives match: {sorted(in_files)}, "
                f"PR-body alternatives match: {sorted(in_pr)})"
            )
    for alt in sorted((file_alts | pr_alts) - covered):
        failures.append(
            f"{rel}: the register phrase list omits the guard pattern {alt!r} "
            f"(source: {sources})"
        )
    return failures


def test_repo_scope_register_phrases_match_both_guards():
    text = (REPO_ROOT / _SCOPE_DOC).read_text()
    failures = check_register_phrases(
        _SCOPE_DOC, text, file_register_alternatives(), pr_body_register_alternatives()
    )
    assert not failures, "\n".join(failures)


def test_register_check_flags_three_lane_council_claimed_for_pr_bodies():
    # origin/master REPO_SCOPE.md:28-34 before the audit correction.
    stale = (
        "- **Chat-session / AI-deliberation register** — committed docs, code comments,\n"
        "  and PR descriptions should read as product engineering. The guard flags, in changed\n"
        # The guard's own phrases are split across literals so this file does
        # not trip the guard it is checking.
        "  files and in PR bodies: operator-local paths (`/Users/" "cirwel`), second-person\n"
        "  address of the operator (`per " "your guidance`, `your " "overlay`, `you " "flagged`,\n"
        "  `questions " "for Kenny`), and exposed AI-review process (`council " "pass`,\n"
        "  `council " "fold`, `live" "-verifier`, `three-lane " "council`). It deliberately does\n"
        "  **not** flag the real product vocabulary `operator`, `council`, or `dialectic`.\n"
    )
    failures = check_register_phrases(
        _SCOPE_DOC, stale, file_register_alternatives(), pr_body_register_alternatives()
    )
    assert len(failures) == 1, failures
    assert "`three-lane " "council`" in failures[0]
    assert f"{_SCOPE_DOC}:6:" in failures[0]


# --- (d) secrets / vars named in docs must be read by some workflow ---------

_SECRET_MENTION = re.compile(
    r"`(?P<a>[A-Z][A-Z0-9_]{2,})`\s+(?:repository\s+|repo\s+|Actions\s+|GitHub\s+)?secret\b"
    r"|\bsecret\s+`(?P<b>[A-Z][A-Z0-9_]{2,})`"
    r"|\b(?:secrets|vars)\.(?P<c>[A-Z][A-Z0-9_]{2,})\b"
)

# (doc, name) pairs that name a secret no workflow reads, on purpose. Each must
# still occur in its doc, or the test fails so the entry is removed.
_ABSENT_BY_DESIGN = {
    # Describes a deliberately unbuilt fix loop that *would* need this secret.
    ("docs/operations/ci-issue-surfacing.md", "ANTHROPIC_API_KEY"),
}


def workflow_secret_and_var_names() -> set[str]:
    names = set()
    for path in sorted((REPO_ROOT / ".github").rglob("*.y*ml")):
        names |= set(re.findall(r"\$\{\{\s*(?:secrets|vars)\.([A-Za-z0-9_]+)", path.read_text()))
    assert "GITHUB_TOKEN" in names, "workflow secret scan found no secrets.GITHUB_TOKEN"
    return names


def check_secret_mentions(rel: str, text: str, known: set[str]) -> tuple[list[str], set[tuple[str, str]]]:
    failures, mentioned = [], set()
    for match in _SECRET_MENTION.finditer(text):
        name = match.group("a") or match.group("b") or match.group("c")
        mentioned.add((rel, name))
        if name in known or (rel, name) in _ABSENT_BY_DESIGN:
            continue
        failures.append(
            f"{rel}:{_line_of(text, match.start())}: names the `{name}` Actions "
            f"secret/var, but no `${{{{ secrets.{name} }}}}` or `${{{{ vars.{name} }}}}` "
            f"occurs under .github/ (source of truth: .github/workflows/*.yml)"
        )
    return failures, mentioned


def test_documented_secrets_are_read_by_a_workflow(living_docs):
    known = workflow_secret_and_var_names()
    failures, mentioned = [], set()
    for path in living_docs:
        doc_failures, doc_mentions = check_secret_mentions(
            _rel(path), path.read_text(errors="replace"), known
        )
        failures += doc_failures
        mentioned |= doc_mentions
    for rel, name in sorted(_ABSENT_BY_DESIGN - mentioned):
        failures.append(
            f"_ABSENT_BY_DESIGN entry ({rel}, {name}) no longer matches a doc mention; remove it"
        )
    for rel, name in sorted(_ABSENT_BY_DESIGN & mentioned):
        if name in known:
            failures.append(
                f"_ABSENT_BY_DESIGN entry ({rel}, {name}) is now read by a workflow; remove it"
            )
    assert not failures, "\n".join(failures)


def test_secret_check_flags_the_retired_plugin_sync_token():
    # origin/master DRIFT_LEDGER.md:59 before the audit correction.
    stale = (
        "| mirror | divergence | sync | ⚠️ opens the sync PR through "
        "`scripts/dev/open-plugin-skill-sync-pr.sh`. That needs the "
        "`PLUGIN_SYNC_TOKEN` secret; without it the workflow only warns |\n"
    )
    failures, _ = check_secret_mentions(
        "docs/dev/DRIFT_LEDGER.md", stale, workflow_secret_and_var_names()
    )
    assert len(failures) == 1
    assert "`PLUGIN_SYNC_TOKEN`" in failures[0]
    assert failures[0].startswith("docs/dev/DRIFT_LEDGER.md:1:")
