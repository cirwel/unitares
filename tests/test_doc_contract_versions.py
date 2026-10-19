"""Doc claims about the current release and interface version match the code.

A living doc that names "the latest release", heads a section as a release
candidate, or states the negotiated interface-contract version is restating a
value the repository already records: `PUBLISHED_VERSION`, the `Status:` line
of `docs/releases/<v>.md`, and `INTERFACE_CONTRACT_VERSION` in
`src/interface_contract.py`. Those values move with every release, and the doc
lint's phrase denylist cannot know the new number, so the restatements went
stale (a "latest `v2.x`" release after 3.x shipped, a tagged v2.22.1 still
headed as a candidate, a `version: 1.27.0` twenty contract releases late).
This test reads the sources by parsing, never by import, and compares only
narrow, structured claims: a `v`-prefixed version tied to "latest" and
"release", a heading that pairs "candidate" with a version, and the contract
guide's version statements outside its dated release entries and fenced
command examples.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"
_PUBLISHED = "PUBLISHED_VERSION"
_CONTRACT_SOURCE = "src/interface_contract.py"
_CONTRACT_DOC = "docs/INTERFACE_CONTRACT.md"
_RELEASES_DIR = "docs/releases"

# Point-in-time records: what they say was true when written. Matches the
# audit's living-doc exclusions on top of check_doc_health's own skip list.
_HISTORICAL_PREFIXES = ("docs/proposals/", "docs/releases/", "papers/")

_FENCE = re.compile(r"^\s*(```|~~~)")
_VERSION = r"v(\d+)\.(\d+|x)(?:\.(\d+))?"
# "latest ... v3.3.0 ... release". `latest` must be a word, not an image tag
# (`gemma4:latest`, `` `latest` ``); the gap may wrap one line, never a
# paragraph or a sentence.
_LATEST_FORWARD = re.compile(
    r"(?<![:`\w-])latest\b(?!`)(?:[^.\n]|\n(?!\s*\n)){0,40}?" + _VERSION
)
# "v3.3.0 is the latest".
_LATEST_BACKWARD = re.compile(r"\bv(\d+)\.(\d+)\.(\d+)[`*]*\s+is\s+(?:the\s+)?latest\b")
# A claim about another release line (plugin, SDK, paper, the interface
# contract) or a dependency is not this server's. Searched over the claim's
# clause on both sides of "latest", so "the plugin's latest release v0.5.0" and
# "the latest plugin release v0.5.0" are both exempt. `mcp` counts only as the
# package (not `governance-mcp`, not "MCP server"); `AGE` only in capitals.
_OTHER_PRODUCT = re.compile(
    r"(?:\b(?i:plugins?|sdks?|paper|contract|interface|compose|postgres(?:ql)?|pgvector"
    r"|python|redis|ollama|node)\b|(?<![-\w])(?i:mcp)\b(?!\s+server)|\bAGE\b)"
)
# The start of the clause a match sits in: a sentence end, a semicolon, a
# paragraph break, or a line that opens a new list item or table row.
_CLAUSE_BREAK = re.compile(r"(?:[.;!?](?=\s)|\n\s*\n|\n[ \t]*(?:[-*+]|\d+\.|\|)[ \t])")
_PAST_TENSE = re.compile(r"(?:\bwas\b|\bthen-)[^.\n]{0,15}$", re.IGNORECASE)

_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_CANDIDATE = re.compile(r"\bcandidates?\b", re.IGNORECASE)
_HEADING_VERSION = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)\b")

# `version: 1.27.0`, version `1.49.0`, version 1.49.0 -- but not `--version`.
_CONTRACT_VERSION_CLAIM = re.compile(r"(?<![-\w])version[:\s]+`?(\d+\.\d+\.\d+)")
# ...unless the version is qualified as some other thing's, a few words back in
# the same clause ("server version 3.3.0", "the SDK package version 1.2.0").
_NON_CONTRACT_SUBJECT = re.compile(
    r"\b(?:server|package|python|sdk|plugin|postgres(?:ql)?|redis|mcp|docker|node|paper)\b"
    r"(?:[^.;\n]{0,25})$",
    re.IGNORECASE,
)
_CONTRACT_HEADER = re.compile(r"^\*\*Current contract:\*\*.*version `(\d+\.\d+\.\d+)`")
_CONTRACT_ENTRY = re.compile(r"^## (\d+\.\d+\.\d+) \(\d{4}-\d{2}-\d{2}\)\s*$")


def _vtuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _without_fences(text: str) -> str:
    """Blank fenced-code lines, keeping line numbers stable."""
    out, fenced = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            out.append("")
        else:
            out.append("" if fenced else line)
    return "\n".join(out)


def _lineno(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _clause_before(text: str, offset: int, span: int = 40) -> str:
    """Up to `span` chars before `offset`, cut at the clause's start."""
    window = text[max(0, offset - span): offset]
    breaks = list(_CLAUSE_BREAK.finditer(window))
    return window[breaks[-1].end():] if breaks else window


# --- sources of truth -------------------------------------------------------


def published_version(root: Path = REPO_ROOT) -> str:
    value = (root / _PUBLISHED).read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", value), f"{_PUBLISHED} is not X.Y.Z: {value!r}"
    return value


def contract_version(root: Path = REPO_ROOT) -> str:
    tree = ast.parse((root / _CONTRACT_SOURCE).read_text())
    values = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "INTERFACE_CONTRACT_VERSION" for t in node.targets)
    ]
    assert len(values) == 1, f"{_CONTRACT_SOURCE}: expected one INTERFACE_CONTRACT_VERSION, found {values}"
    return values[0]


def tagged_releases(root: Path = REPO_ROOT) -> set[str]:
    """Versions whose `docs/releases/<v>.md` opens with `Status: tagged`."""
    tagged = set()
    for path in (root / _RELEASES_DIR).glob("*.md"):
        if not re.fullmatch(r"\d+\.\d+\.\d+", path.stem):
            continue
        head = "\n".join(path.read_text().splitlines()[:12])
        if re.search(r"^Status:\s*tagged\b", head, re.MULTILINE | re.IGNORECASE):
            tagged.add(path.stem)
    return tagged


# --- checks (pure: doc text in, failure messages out) ------------------------


def latest_release_failures(rel: str, text: str, published: str) -> list[str]:
    body = _without_fences(text)
    failures = []
    pub_major = _vtuple(published)[0]

    def judge(match: re.Match, major: str, minor: str, patch: str | None) -> None:
        if minor == "x":
            ok, claim = int(major) == pub_major, f"v{major}.x"
        elif patch is None:
            ok, claim = (int(major), int(minor)) == _vtuple(published)[:2], f"v{major}.{minor}"
        else:
            ok, claim = f"{major}.{minor}.{patch}" == published, f"v{major}.{minor}.{patch}"
        if not ok:
            failures.append(
                f"{rel}:{_lineno(body, match.start())}: names {claim} as the latest "
                f"release ({' '.join(match.group(0).split())!r}), but {_PUBLISHED} is {published}"
            )

    for match in _LATEST_FORWARD.finditer(body):
        after = body[match.end(): match.end() + 25]
        after = after[: m.start()] if (m := _CLAUSE_BREAK.search(after)) else after
        tail = body[match.start(): match.end()] + after
        if not re.search(r"\breleases?\b", tail):
            continue
        if _OTHER_PRODUCT.search(_clause_before(body, match.start()) + tail):
            continue
        if _PAST_TENSE.search(body[max(0, match.start() - 20): match.start()]):
            continue
        judge(match, *match.group(1, 2, 3))
    for match in _LATEST_BACKWARD.finditer(body):
        after = body[match.end(): match.end() + 25]
        after = after[: m.start()] if (m := _CLAUSE_BREAK.search(after)) else after
        if _OTHER_PRODUCT.search(_clause_before(body, match.start()) + match.group(0) + after):
            continue
        judge(match, *match.group(1, 2, 3))
    return failures


def candidate_heading_failures(
    rel: str, text: str, published: str, tagged: set[str]
) -> list[str]:
    failures = []
    for lineno, line in enumerate(_without_fences(text).split("\n"), 1):
        heading = _HEADING.match(line)
        if not heading or not _CANDIDATE.search(heading.group(1)):
            continue
        if _OTHER_PRODUCT.search(heading.group(1)):
            continue  # a plugin, SDK or contract candidate has its own numbering
        for match in _HEADING_VERSION.finditer(heading.group(1)):
            version = ".".join(match.groups())
            if version in tagged:
                why = f"{_RELEASES_DIR}/{version}.md says Status: tagged"
            elif _vtuple(version) <= _vtuple(published):
                why = f"{_PUBLISHED} is {published}"
            else:
                continue
            failures.append(
                f"{rel}:{lineno}: heading presents {version} as a candidate "
                f"({line.strip()!r}), but {why}"
            )
    return failures


def contract_version_failures(rel: str, text: str, current: str) -> list[str]:
    """Version statements in the contract guide's prose, before its release entries."""
    lines = _without_fences(text).split("\n")
    failures, headers = [], []
    for lineno, line in enumerate(lines, 1):
        if _CONTRACT_ENTRY.match(line):
            break  # dated release entries are history; checked separately
        header = _CONTRACT_HEADER.match(line)
        if header:
            headers.append(lineno)
        for match in _CONTRACT_VERSION_CLAIM.finditer(line):
            if _NON_CONTRACT_SUBJECT.search(line[: match.start()]):
                continue
            if match.group(1) != current:
                failures.append(
                    f"{rel}:{lineno}: states contract version {match.group(1)} "
                    f"({match.group(0)!r}), but {_CONTRACT_SOURCE} "
                    f"INTERFACE_CONTRACT_VERSION is {current}"
                )
    if len(headers) != 1:
        failures.append(
            f"{rel}: expected exactly one '**Current contract:** ... version `X.Y.Z`' "
            f"line (scripts/dev/prepare_interface_release.py rewrites it), found "
            f"{len(headers)} at lines {headers}"
        )
    entries = [(n, m.group(1)) for n, l in enumerate(lines, 1) if (m := _CONTRACT_ENTRY.match(l))]
    if entries and entries[-1][1] != current:
        lineno, version = entries[-1]
        failures.append(
            f"{rel}:{lineno}: newest release entry is '## {version}', but "
            f"{_CONTRACT_SOURCE} INTERFACE_CONTRACT_VERSION is {current} "
            f"(prepare_interface_release.py appends the entry for each bump)"
        )
    for (_, older), (lineno, newer) in zip(entries, entries[1:]):
        if _vtuple(newer) <= _vtuple(older):
            failures.append(
                f"{rel}:{lineno}: release entry '## {newer}' follows '## {older}'; "
                f"entries must ascend so the last is the current version"
            )
    return failures


# --- the tree ----------------------------------------------------------------


@pytest.fixture(scope="module")
def living_docs() -> list[tuple[str, str]]:
    spec = importlib.util.spec_from_file_location("check_doc_health_versions", _DOC_HEALTH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # collect_md_files walks the filesystem; a local checkout also holds
    # gitignored point-in-time notes (handoffs, dogfood reports) CI never sees.
    # Keep tracked files and untracked-but-not-ignored ones (docs about to land).
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    in_git = set(filter(None, listed.split("\0")))
    docs = []
    for path in module.collect_md_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(_HISTORICAL_PREFIXES) or rel not in in_git:
            continue
        docs.append((rel, path.read_text(errors="replace")))
    return docs


def test_sources_of_truth_parse():
    assert _vtuple(published_version()) >= (3, 0, 0)
    assert re.fullmatch(r"\d+\.\d+\.\d+", contract_version())
    tagged = tagged_releases()
    assert tagged, f"no {_RELEASES_DIR}/X.Y.Z.md opens with 'Status: tagged'; the parser or the notes moved"
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in tagged), tagged


def test_doc_release_and_contract_versions_current(living_docs):
    rels = {rel for rel, _ in living_docs}
    assert _CONTRACT_DOC in rels and "docs/REVIEWER_GUIDE.md" in rels, "living-doc set is empty or wrong"
    published, tagged, current = published_version(), tagged_releases(), contract_version()
    failures = []
    for rel, text in living_docs:
        failures += latest_release_failures(rel, text, published)
        failures += candidate_heading_failures(rel, text, published, tagged)
    failures += contract_version_failures(
        _CONTRACT_DOC, (REPO_ROOT / _CONTRACT_DOC).read_text(), current
    )
    assert not failures, "Docs restate a release or contract version the code has moved past:\n" + "\n".join(failures)


# --- regressions: the stale text the 2026-10-17 audit found -------------------


@pytest.mark.parametrize(
    "snippet, claim",
    [
        ("- Public release history: latest `v2.x` release in this repository.\n", "v2.x"),
        ("and never shipped in a release; v2.22.1 is the latest release and defaults to\n", "v2.22.1"),
        ("The latest verified\nrelease is v3.2.0.\n", "v3.2.0"),
        ("The server's latest release is v3.2.0.\n", "v3.2.0"),
        # A product noun in the previous sentence does not exempt this one.
        ("See the plugin notes. The latest release is v3.2.0.\n", "v3.2.0"),
        ("The governance-mcp latest release is v3.2.0.\n", "v3.2.0"),
    ],
)
def test_flags_stale_latest_release(snippet, claim):
    failures = latest_release_failures("docs/x.md", snippet, "3.3.0")
    assert len(failures) == 1 and claim in failures[0] and "PUBLISHED_VERSION is 3.3.0" in failures[0]


@pytest.mark.parametrize(
    "snippet",
    [
        "- Public release history: latest `v3.x` release in this repository.\n",
        "v3.3.0 is the latest release.\n",
        "and never shipped in a release; v2.22.1 is the last 2.x release and defaults to\n",
        "| `UNITARES_MODEL_ID` | `gemma4:latest`, a default a later release removes v2.1 |\n",
        "Promotion moves `latest` to the verified v2.22.1 release digest.\n",
        "the latest plugin release v0.4.17 matches the bundle.\n",
        # Another product named before "latest", in the usual English order.
        "The plugin's latest release is v0.5.0.\n",
        "The SDK's latest v1.2.0 release adds X.\n",
        "Paper v6's latest release, v6.9.3, is on Zenodo.\n",
        "The governance plugin ships its latest\nrelease v0.6.0 with the bundle.\n",
        "v0.5.0 is the latest plugin release.\n",
        # Dependencies have their own release lines.
        "Upgrading to the latest mcp v2.1.1 release fixed it.\n",
        "Docker Compose latest v2.29.0 release required.\n",
        "The latest AGE release v1.7.0 supports PG18.\n",
        "On 2026-09-08 it was the latest v2.22.1 release.\n",
        "```\nlatest release v1.0.0\n```\n",
    ],
)
def test_allows_current_historical_and_unrelated_latest(snippet):
    assert latest_release_failures("docs/x.md", snippet, "3.3.0") == []


def test_flags_tagged_release_still_headed_as_candidate():
    failures = candidate_heading_failures(
        "docs/operations/RELEASE_PROCESS.md",
        "intro\n\n## v2.22.1 maintenance candidate\n\nSee the notes.\n",
        "3.3.0",
        {"2.22.1"},
    )
    assert failures == [
        "docs/operations/RELEASE_PROCESS.md:3: heading presents 2.22.1 as a candidate "
        "('## v2.22.1 maintenance candidate'), but docs/releases/2.22.1.md says Status: tagged"
    ]
    # Tagged but not yet promoted to PUBLISHED_VERSION is still not a candidate.
    assert candidate_heading_failures("d.md", "## v3.4.0 candidate\n", "3.3.0", {"3.4.0"})


def test_allows_future_candidate_and_versionless_candidate_headings():
    text = "## v3.4.0 release candidate\n\n## Candidate corpora\n\n```\n# 2.0.0 candidate\n```\n"
    assert candidate_heading_failures("d.md", text, "3.3.0", {"2.22.1"}) == []


def test_allows_other_product_candidate_headings():
    text = "## Interface contract 1.50.0 candidate\n\n## Plugin v0.5.0 release candidate\n"
    assert candidate_heading_failures("d.md", text, "3.3.0", set()) == []


_CONTRACT_STALE = """\
**Current contract:** `unitares.interface-contract.v1`, version `1.49.0`

- `version: 1.27.0` is the negotiated interface release. Compatible additions

```bash
python3 scripts/dev/prepare_interface_release.py --version 1.34.0 \\
```

Run it with --version 1.50.0 to preview.

## 1.48.0 (2026-10-17)

Since version 1.47.0 nothing.

## 1.49.0 (2026-10-17)
"""


def test_flags_stale_contract_version_in_prose():
    failures = contract_version_failures("docs/INTERFACE_CONTRACT.md", _CONTRACT_STALE, "1.49.0")
    assert failures == [
        "docs/INTERFACE_CONTRACT.md:3: states contract version 1.27.0 ('version: 1.27.0'), "
        "but src/interface_contract.py INTERFACE_CONTRACT_VERSION is 1.49.0"
    ]


def test_allows_non_contract_versions_in_contract_prose():
    text = _CONTRACT_STALE.replace(
        "- `version: 1.27.0` is",
        "Clients built against server version 3.3.0 receive this contract.\n"
        "The Python SDK package version 1.2.0 negotiates it.\n- `version: 1.49.0` is",
    )
    assert contract_version_failures("docs/INTERFACE_CONTRACT.md", text, "1.49.0") == []


def test_flags_contract_header_and_entries_behind_source():
    failures = contract_version_failures("docs/INTERFACE_CONTRACT.md", _CONTRACT_STALE, "1.50.0")
    assert any(":1: states contract version 1.49.0" in f for f in failures)
    assert any("newest release entry is '## 1.49.0'" in f for f in failures)
    missing = contract_version_failures("d.md", "no header here\n", "1.49.0")
    assert any("expected exactly one '**Current contract:**" in f for f in missing)


def test_neighbouring_bullet_does_not_exempt_a_stale_claim():
    # A product word in the previous bullet is a different clause.
    text = "- Redis 7\n- Public release history: latest `v2.x` release\n"
    assert latest_release_failures("d.md", text, "3.3.0")


def test_next_sentence_does_not_exempt_a_stale_claim():
    text = "The latest release is v2.22.1.\nPython 3.14 required.\n"
    assert latest_release_failures("d.md", text, "3.3.0")
