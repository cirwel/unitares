#!/usr/bin/env python3
"""Check that the plugin's freshness checker dates skills by canonical's rule.

The plugin cannot import unitares, so ``scripts/_check_freshness.py`` in
unitares-governance-plugin carries its own copy of THE RULE from
``src/skill_attestations.py`` (which attestations vouch for the SKILL.md on
disk, and so which ``verified_date`` counts). A copy can drift silently: the
plugin would keep passing its own tests while dating skills differently from
the server's ``skills`` tool and canonical's checker.

This runs both implementations over the same inputs and reports any
disagreement. The inputs are a fixed matrix of attestation directories (every
single record variant, every ordered pair, and a seeded sample of triples,
covering malformed and partial records) plus the plugin's real synced skills.
``sync-plugin-skills.sh`` calls it, because the sync is the one place both
checkouts are present.

Usage:
    python3 scripts/dev/check_plugin_attestation_rule.py <plugin_repo>

Exits 0 when the rules agree (or the plugin has no freshness checker to
compare), 1 on disagreement, 2 when the plugin's checker cannot be loaded.
Findings go to stdout.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.skill_attestations import (  # noqa: E402
    ATTESTATIONS_DIR,
    load_attestations,
    skill_text_digest,
    vouching_date,
)

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNLOADABLE = 2

PLUGIN_CHECKER = Path("scripts") / "_check_freshness.py"
SKILL = "demo"
MAX_REPORTED = 5
TRIPLE_SAMPLE = 400

_MISSING = object()
_DATES = ("2026-01-10", "2026-03-05", "2026-02-20")


def _record_variants(current_digest: str) -> list[object]:
    """Every attestation shape worth distinguishing, as JSON-able values.

    A str value is written verbatim, so it can be unparseable JSON.
    """
    variants: list[object] = ["{not json", json.dumps(["a", "list"])]
    dates = [*_DATES, "", None, 20260101, _MISSING]
    digests = [current_digest, "0000000000000000", _MISSING]
    sources = [{}, _MISSING, ["not", "a", "map"]]
    for date, digest, src in itertools.product(dates, digests, sources):
        record: dict[str, object] = {}
        for key, value in (("verified_date", date), ("skill_digest", digest),
                           ("source_digests", src)):
            if value is not _MISSING:
                record[key] = value
        variants.append(record)
    return variants


def _fill(adir: Path, records: tuple[object, ...]) -> None:
    """Write ``records`` so that file-name order is the tuple's order."""
    shutil.rmtree(adir, ignore_errors=True)
    adir.mkdir(parents=True)
    for i, record in enumerate(records):
        body = record if isinstance(record, str) else json.dumps(record)
        (adir / f"2026010{i}T000000000000Z-{i:08x}.json").write_text(body, encoding="utf-8")


def _load_plugin_checker(plugin_repo: Path):
    path = plugin_repo / PLUGIN_CHECKER
    spec = importlib.util.spec_from_file_location("_plugin_check_freshness", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(skills_dir: Path, name: str, digest: str) -> str | None:
    return vouching_date(load_attestations(skills_dir, name), digest)


def compare(plugin, skills_dir: Path, name: str, digest: str) -> tuple[str | None, str | None]:
    return _canonical(skills_dir, name, digest), plugin.attested_date(skills_dir, name, digest)


def check(plugin_repo: Path) -> tuple[int, list[str]]:
    if not (plugin_repo / PLUGIN_CHECKER).is_file():
        return EXIT_OK, [f"no {PLUGIN_CHECKER} in plugin; nothing to compare"]
    try:
        plugin = _load_plugin_checker(plugin_repo)
    except Exception as exc:  # the plugin's module is foreign code; any failure is "cannot load"
        return EXIT_UNLOADABLE, [f"cannot load plugin {PLUGIN_CHECKER}: {type(exc).__name__}: {exc}"]
    missing = [fn for fn in ("attested_date", "skill_text_digest") if not hasattr(plugin, fn)]
    if missing:
        return EXIT_DRIFT, [f"plugin {PLUGIN_CHECKER} has no {', '.join(missing)}(): "
                            "it predates the port of src/skill_attestations.py THE RULE"]

    findings: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp) / "skills"
        skill_md = skills_dir / SKILL / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\nname: demo\n---\n# Demo\n", encoding="utf-8")
        digest = skill_text_digest(skill_md)
        if plugin.skill_text_digest(skill_md) != digest:
            findings.append(f"skill_text_digest differs: canonical {digest}, "
                            f"plugin {plugin.skill_text_digest(skill_md)}")

        adir = skills_dir / ATTESTATIONS_DIR / SKILL
        variants = _record_variants(digest)
        rng = random.Random(0)
        cases: list[tuple[object, ...]] = [()]
        cases += [(v,) for v in variants]
        cases += list(itertools.product(variants, repeat=2))
        cases += [tuple(rng.choice(variants) for _ in range(3)) for _ in range(TRIPLE_SAMPLE)]

        # A missing attestations directory, then every case.
        want, got = compare(plugin, skills_dir, SKILL, digest)
        if want != got:
            findings.append(f"no attestations dir: canonical {want!r}, plugin {got!r}")
        for records in cases:
            _fill(adir, records)
            want, got = compare(plugin, skills_dir, SKILL, digest)
            if want != got:
                findings.append(f"records {records!r}: canonical {want!r}, plugin {got!r}")

    real_dir = plugin_repo / "skills"
    if real_dir.is_dir():
        for skill_dir in sorted(p for p in real_dir.iterdir() if (p / "SKILL.md").is_file()):
            digest = skill_text_digest(skill_dir / "SKILL.md")
            want, got = compare(plugin, real_dir, skill_dir.name, digest)
            if want != got:
                findings.append(f"synced skill {skill_dir.name}: canonical {want!r}, plugin {got!r}")

    if not findings:
        return EXIT_OK, []
    shown = findings[:MAX_REPORTED]
    if len(findings) > MAX_REPORTED:
        shown.append(f"... and {len(findings) - MAX_REPORTED} more")
    return EXIT_DRIFT, shown


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: check_plugin_attestation_rule.py <plugin_repo>", file=sys.stderr)
        return EXIT_UNLOADABLE
    status, lines = check(Path(argv[1]))
    for line in lines:
        print(line)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
