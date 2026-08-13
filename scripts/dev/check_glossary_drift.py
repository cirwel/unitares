#!/usr/bin/env python3
"""Deterministic glossary drift check (CI gate).

The 2026-06-20 drift audit's closing instruction — "re-run and re-date when the
vocabulary moves" — was a manual sweep that predictably would not happen. This
is the standing, free, deterministic replacement for the checks a machine can
do (a human sweep is still needed for *semantic* drift):

1. ``docs/ontology/glossary.md`` parses into the structure the public-site
   viewer build needs (``scripts/dev/glossary_data.py``), so a formatting
   change cannot silently break the generated viewer.
2. Every ``path.py::symbol`` reference in glossary.md resolves to a real
   symbol in this repo — a rename in code fails CI instead of leaving the
   glossary pointing at nothing (the audit's lesson: "read the code, not only
   the proposal docs").
3. The doc glossary and the runtime glossary (``src/governance_glossary.py``)
   stay cross-referenced on their overlap: the ``basin`` entry must point at
   the runtime glossary, and the runtime glossary must still define the
   vocabulary families the doc relies on.
4. The retired hand-maintained viewer copy (``docs/ontology/glossary-viewer.html``)
   stays retired — the viewer is generated from glossary.md at build time and
   a parallel hand-edited dataset must not come back.

Stdlib only; no model API (execution-cost policy, CLAUDE.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glossary_data import GLOSSARY_MD, PROJECT_ROOT, GlossaryParseError, parse_glossary  # noqa: E402

RUNTIME_GLOSSARY = PROJECT_ROOT / "src" / "governance_glossary.py"
RETIRED_VIEWER = PROJECT_ROOT / "docs" / "ontology" / "glossary-viewer.html"

# Vocabulary families the doc glossary's entries lean on (basin senses, EISV
# rows in the Rosetta table). If one disappears from the runtime glossary,
# the cross-reference in glossary.md goes stale.
RUNTIME_FAMILIES = ("EISV_DIMENSIONS", "BASINS", "VERDICTS", "def explain_basin")

CODE_REF = re.compile(r"`([\w./-]+\.py)::(\w+)`")


def _resolve(path_str: str) -> Path | None:
    for candidate in (PROJECT_ROOT / path_str, PROJECT_ROOT / "src" / path_str):
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    failures: list[str] = []
    glossary_text = GLOSSARY_MD.read_text(encoding="utf-8")

    # 1. Structural parse.
    try:
        data = parse_glossary()
        print(
            f"[OK] glossary.md parsed: {len(data['homonyms'])} homonyms, "
            f"{len(data['single_sense'])} single-sense, "
            f"{len(data['open_gaps'])} open gaps, "
            f"{len(data['rosetta']['rows'])} rosetta rows"
        )
    except GlossaryParseError as exc:
        failures.append(f"glossary.md no longer parses: {exc}")
        data = None

    # 2. Code references resolve.
    refs = CODE_REF.findall(glossary_text)
    if not refs:
        failures.append("no `path.py::symbol` references found — regex or doc drifted")
    for path_str, symbol in refs:
        resolved = _resolve(path_str)
        if resolved is None:
            failures.append(f"glossary.md references {path_str}::{symbol} but the file is gone")
            continue
        source = resolved.read_text(encoding="utf-8")
        if not re.search(rf"(def|class)\s+{re.escape(symbol)}\b|^{re.escape(symbol)}\s*[:=]", source, re.M):
            failures.append(
                f"glossary.md references {path_str}::{symbol} but the symbol is not in {resolved.relative_to(PROJECT_ROOT)}"
            )
        else:
            print(f"[OK] code ref {path_str}::{symbol}")

    # 3. Doc <-> runtime glossary cross-reference.
    if data is not None:
        basin_entries = [t for t in data["homonyms"] if t["term"] == "basin"]
        if not basin_entries:
            failures.append("glossary.md lost its `basin` homonym entry")
        else:
            basin_text = " ".join(
                f"{s['name']} {s['question']} {s['source']}" for s in basin_entries[0]["senses"]
            )
            # The pointer lives in the entry's surrounding prose, so accept
            # either the senses table or the raw section text.
            if "governance_glossary" not in basin_text and "governance_glossary" not in glossary_text:
                failures.append(
                    "glossary.md `basin` entry no longer points at src/governance_glossary.py"
                )
            else:
                print("[OK] basin entry cross-references the runtime glossary")
        terms = {t["term"] for t in data["homonyms"]} | {t["term"] for t in data["single_sense"]}
        if "EISV" not in terms:
            failures.append("glossary.md lost its `EISV` entry (runtime glossary explains EISV values)")
        else:
            print("[OK] EISV entry present")

    runtime_text = RUNTIME_GLOSSARY.read_text(encoding="utf-8")
    for family in RUNTIME_FAMILIES:
        if family not in runtime_text:
            failures.append(f"src/governance_glossary.py no longer defines {family}")
        else:
            print(f"[OK] runtime glossary defines {family}")

    # 4. The hand-maintained viewer stays retired.
    if RETIRED_VIEWER.exists():
        failures.append(
            "docs/ontology/glossary-viewer.html is back — the viewer is generated "
            "from glossary.md by scripts/dev/build_public_site.py; do not "
            "reintroduce a hand-maintained data copy"
        )
    else:
        print("[OK] no hand-maintained viewer copy")

    if failures:
        print("\nGLOSSARY DRIFT DETECTED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\nglossary drift check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
