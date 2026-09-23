#!/usr/bin/env python3
"""Inventory a committed proposal tree without assigning policy or moving files.

Phase 0 of #2347. References are conservative filename mentions, not a safe
rewrite list: the same basename can identify a stub and its resolved document.
Git dates report content changes, never whether a change was meaningful.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import posixpath
import subprocess


PROPOSALS = "docs/proposals/"
INDEX = PROPOSALS + "README.md"
DISPOSITIONS = ("Built", "Registered", "Active", "Parked", "Closed")
ROW = re.compile(r"^\|\s*\[[^]]+\]\(([^)]+)\)\s*\|\s*(.*?)\s*\|$", re.M)
STATUS = re.compile(r"(?:^\s*[-*>]?\s*|[·|]\s*)\**(?:status|disposition)\**\s*[:：]", re.I)


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(root), *args])


def tracked_text(root: Path, revision: str) -> tuple[str, dict[str, str], list[str]]:
    """Read one immutable Git tree, including assets but skipping binary bodies."""
    commit = git(root, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    entries = git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0")
    objects = []
    for entry in entries:
        if entry:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if kind == b"blob" and mode != b"120000":
                objects.append((raw_path.decode(), oid))
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), "cat-file", "--batch"],
        input=b"\n".join(oid for _, oid in objects) + b"\n",
        stdout=subprocess.PIPE, check=True,
    )
    texts = {}
    offset = 0
    for path, _ in objects:
        header_end = proc.stdout.index(b"\n", offset)
        size = int(proc.stdout[offset:header_end].rsplit(b" ", 1)[1])
        body = proc.stdout[header_end + 1:header_end + 1 + size]
        offset = header_end + 1 + size + 1
        if b"\0" not in body:
            try:
                texts[path] = body.decode("utf-8")
            except UnicodeDecodeError:
                pass
    return commit, texts, [path for path, _ in objects]


def status_block(text: str) -> str | None:
    lines = text.splitlines()
    for number, line in enumerate(lines[:15]):
        if STATUS.search(line):
            block = [line]
            for continuation in lines[number + 1:]:
                if not continuation.strip() or re.match(r"^\s*(?:#|\*\*\w[^*]*:|[-*] )", continuation):
                    break
                block.append(continuation)
            return "\n".join(block)
    return None


def last_content_change(root: Path, commit: str, path: str) -> dict:
    """Use a disclosed textual proxy for 'meaningful', not semantic inference."""
    history = git(root, "log", "--follow", "-w", "--ignore-blank-lines", "--numstat",
                  "--format=%x1e%H%n%cI%n%s", commit, "--", path).decode()
    for record in history.split("\x1e"):
        lines = record.strip().splitlines()
        if len(lines) >= 4 and any(re.match(r"(?:[1-9]\d*\t|\d+\t[1-9]|-\t-\t)", line)
                                   for line in lines[3:]):
            return {"commit": lines[0], "date": lines[1], "subject": lines[2],
                    "basis": "latest non-whitespace content diff, following renames; semantic significance not inferred"}
    return {"basis": "no non-whitespace content diff found"}


def consumer(path: str) -> str:
    if path.startswith(("tests/", "agents/sdk/tests/")):
        return "tests"
    if path.startswith(("src/", "governance_core/", "config/", "elixir/", "agents/")):
        return "source"
    if path.startswith(("skills/", "commands/")) or path in ("AGENTS.md", "CLAUDE.md"):
        return "skills_and_agent_contracts"
    if path.startswith(("scripts/", ".github/")):
        return "tooling_and_ci"
    if path.startswith(("site/", "website/")):
        return "public_site"
    if path.startswith(("paper/", "papers/")):
        return "paper"
    return "documentation_or_other"


def inventory(root: Path, revision: str = "HEAD") -> dict:
    commit, texts, paths = tracked_text(root, revision)
    proposal_paths = sorted(p for p in paths if p.startswith(PROPOSALS))
    rows: dict[str, list[dict]] = defaultdict(list)
    indexes = sorted(p for p in proposal_paths if p.endswith("/README.md"))
    for index in indexes:
        for link, description in ROW.findall(texts.get(index, "")):
            target = posixpath.normpath(str(PurePosixPath(index).parent / link))
            tag = re.match(r"\*\*(" + "|".join(DISPOSITIONS) + r")\b", description)
            rows[target].append({"index": index, "disposition": tag[1] if tag else None,
                                 "description": description})

    by_name: dict[str, list[str]] = defaultdict(list)
    for path in proposal_paths:
        by_name[PurePosixPath(path).name].append(path)
    mentions: dict[str, list[dict]] = defaultdict(list)
    # README.md alone is too ambiguous; require a proposals-qualified path.
    names = sorted(set(by_name) - {"README.md"}, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w.-])") if names else None
    for source, body in sorted(texts.items()):
        for line_number, line in enumerate(body.splitlines(), 1):
            targets = set()
            if pattern:
                for match in pattern.finditer(line):
                    targets.update(by_name[match[1]])
            for index in indexes:
                if index in line:
                    targets.add(index)
            for target in sorted(targets - {source}):
                mentions[target].append({"path": source, "line": line_number,
                                         "consumer": consumer(source)})

    records = []
    for path in proposal_paths:
        body = texts.get(path, "")
        row = rows[path]
        disposition = next((r["disposition"] for r in row if r["disposition"]), None)
        if PurePosixPath(path).name == "README.md":
            audience = "navigation"
        elif disposition == "Registered":
            audience = "registered_protocol"
        elif disposition == "Active":
            audience = "active_research"
        elif disposition or "/resolved/" in path:
            audience = "historical_provenance"
        else:
            audience = "needs_review"
        log = git(root, "log", "-1", "--format=%H%n%cI%n%s", commit, "--", path).decode().splitlines()
        refs = mentions[path]
        records.append({
            "path": path,
            "sha256": hashlib.sha256(git(root, "show", f"{commit}:{path}")).hexdigest(),
            "owning_index": row[0]["index"] if row else (INDEX if path != INDEX else "docs/README.md"),
            "index_rows": row,
            "declared_status": status_block(body),
            "disposition": disposition,
            "last_path_change": {"commit": log[0], "date": log[1], "subject": log[2]},
            "last_meaningful_update": last_content_change(root, commit, path),
            "proposed_audience": audience,
            "audience_basis": "index disposition only; not a relocation or readiness decision",
            "inbound_reference_candidates": refs,
            "referenced_by": sorted({r["consumer"] for r in refs}),
            "external_references": "not assessed by this repository-only inventory",
        })
    return {
        "schema": "unitares.proposal_inventory.v1",
        "source_commit": commit,
        "scope": "all tracked files under docs/proposals at source_commit",
        "limits": [
            "Meaningful update uses the latest non-whitespace content diff as a disclosed proxy; it does not infer semantic significance.",
            "Inbound candidates are literal filename mentions; ambiguous basenames are reported for every candidate.",
            "Paper, website and plugin repositories and published deep links require separate inspection.",
            "Audience classes mirror dated index tags; Built and Parked can still contain live contracts or work.",
            "No file move, protocol change, policy decision or implementation is authorized by this inventory.",
        ],
        "counts": {"files": len(records), "dispositions": dict(Counter(
            record["disposition"] for record in records if record["disposition"]
        ))},
        "documents": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--ref", default="HEAD", help="Committed snapshot to inventory (default: HEAD)")
    parser.add_argument("--output", type=Path, help="Write JSON here; otherwise print to stdout")
    args = parser.parse_args()
    result = json.dumps(inventory(args.repo, args.ref), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(result)
    else:
        print(result, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
