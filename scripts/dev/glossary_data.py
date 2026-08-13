#!/usr/bin/env python3
"""Parse ``docs/ontology/glossary.md`` into structured data.

Single source of truth for glossary structure: the public-site build renders
the interactive viewer from this parse (so the viewer cannot drift from the
markdown), and ``check_glossary_drift.py`` fails CI when the markdown stops
parsing into the shape both consumers need.

The glossary's sections map to:

- ``homonyms``      — ``### <term> — ...`` headings under "High-risk homonyms",
                      each with a Sense/Question/Canonical-source table.
- ``single_sense``  — the Term/Question/Canonical-source table under
                      "Single-sense load-bearing terms".
- ``open_gaps``     — top-level ``- **title** detail`` bullets under "Open gaps".
- ``rosetta``       — the cross-register table (columns + rows, one row per
                      referent). Status glyphs (✓ ◐ ~ ✗) are kept verbatim.

Stdlib only; no model API (see the execution-cost policy in CLAUDE.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GLOSSARY_MD = PROJECT_ROOT / "docs" / "ontology" / "glossary.md"

# Sentinel for escaped pipes (``\|``) inside table cells so cell splitting
# doesn't break on them.
_PIPE = "\x00"

_HOMONYM_HEADING = "## High-risk homonyms"
_SINGLE_HEADING = "## Single-sense load-bearing terms"
_GAPS_HEADING = "## Open gaps"
_ROSETTA_HEADING = "## Cross-register map"


class GlossaryParseError(ValueError):
    """The glossary markdown no longer matches the structure consumers need."""


def _clean(text: str) -> str:
    """Strip markdown emphasis/backticks for display as plain text."""
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return text.strip()


def _split_row(line: str) -> List[str]:
    cells = line.replace("\\|", _PIPE).strip().strip("|").split("|")
    return [c.replace(_PIPE, "|").strip() for c in cells]


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_separator_row(line: str) -> bool:
    body = line.strip().strip("|")
    return bool(body) and set(body) <= set("-: |")


def _tables_in(lines: List[str]) -> List[List[List[str]]]:
    """All markdown tables in ``lines``, each as a list of cell-rows (header first)."""
    tables: List[List[List[str]]] = []
    current: List[List[str]] = []
    for line in lines:
        if _is_table_row(line):
            if not _is_separator_row(line):
                current.append(_split_row(line))
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _section(lines: List[str], heading: str) -> List[str]:
    """Lines from ``heading`` up to the next ``## `` heading (exclusive)."""
    out: List[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line.startswith(heading)
            continue
        if inside:
            out.append(line)
    if not out:
        raise GlossaryParseError(f"section not found in glossary.md: {heading!r}")
    return out


def _parse_homonyms(lines: List[str]) -> List[Dict[str, Any]]:
    terms: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("### "):
            heading = line[4:].strip()
            term = _clean(heading.split(" — ")[0])
            code_level = "code level" in heading.lower()
            # Senses come from the first table after the heading.
            block: List[str] = []
            j = i + 1
            while j < len(lines) and not lines[j].startswith("### "):
                block.append(lines[j])
                j += 1
            tables = _tables_in(block)
            if not tables:
                raise GlossaryParseError(f"homonym {term!r} has no sense table")
            senses = [
                {"name": _clean(r[0]), "question": _clean(r[1]), "source": _clean(r[2])}
                for r in tables[0][1:]
                if len(r) >= 3
            ]
            if len(senses) < 2:
                raise GlossaryParseError(f"homonym {term!r} has fewer than two senses")
            terms.append({"term": term, "code_level": code_level, "senses": senses})
            i = j
        else:
            i += 1
    return terms


def _parse_single_sense(lines: List[str]) -> List[Dict[str, str]]:
    tables = _tables_in(lines)
    if not tables:
        raise GlossaryParseError("single-sense section has no table")
    return [
        {"term": _clean(r[0]), "question": _clean(r[1]), "source": _clean(r[2])}
        for r in tables[0][1:]
        if len(r) >= 3
    ]


def _parse_open_gaps(lines: List[str]) -> List[Dict[str, str]]:
    gaps: List[Dict[str, str]] = []
    bullet: List[str] = []

    def flush() -> None:
        if not bullet:
            return
        text = " ".join(part.strip() for part in bullet if part.strip())
        m = re.match(r"-\s+\*\*(.+?)\*\*\s*(.*)", text)
        if m:
            gaps.append({"title": _clean(m.group(1)), "detail": _clean(m.group(2))})
        bullet.clear()

    for line in lines:
        if line.startswith("- "):
            flush()
            bullet.append(line)
        elif bullet and (line.startswith("  ") or not line.strip()):
            bullet.append(line)
        elif bullet:
            flush()
    flush()
    return gaps


def _parse_rosetta(lines: List[str]) -> Dict[str, Any]:
    # First table in the section is the referent × register table; the later
    # "FEP promotion conditions" table has its own heading deeper in.
    tables = _tables_in(lines)
    if not tables:
        raise GlossaryParseError("rosetta section has no table")
    table = tables[0]
    columns = [_clean(c) for c in table[0]]
    rows = [[_clean(c) for c in r] for r in table[1:]]
    if len(columns) < 4:
        raise GlossaryParseError("rosetta table has too few register columns")
    return {"columns": columns, "rows": rows}


def parse_glossary(path: Path = GLOSSARY_MD) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    data = {
        "homonyms": _parse_homonyms(_section(lines, _HOMONYM_HEADING)),
        "single_sense": _parse_single_sense(_section(lines, _SINGLE_HEADING)),
        "open_gaps": _parse_open_gaps(_section(lines, _GAPS_HEADING)),
        "rosetta": _parse_rosetta(_section(lines, _ROSETTA_HEADING)),
    }
    if not data["homonyms"]:
        raise GlossaryParseError("no homonym terms parsed")
    if not data["single_sense"]:
        raise GlossaryParseError("no single-sense terms parsed")
    for entry in data["homonyms"]:
        for sense in entry["senses"]:
            if not sense["question"] or not sense["source"]:
                raise GlossaryParseError(
                    f"sense {sense['name']!r} of {entry['term']!r} is missing "
                    "its question or canonical source"
                )
    return data


if __name__ == "__main__":
    import json

    print(json.dumps(parse_glossary(), indent=2, ensure_ascii=False))
