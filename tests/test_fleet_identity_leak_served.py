"""The fleet-identity guard also reads what the server serves verbatim.

The AST scan covers shipped Python. A fresh install also receives the
dashboard (served to the operator's browser) and the skills and tool
descriptions (served to every agent). On 2026-09-26 an audit found the guard
clean while those surfaces named one deployment's residents, because none of
them was Python. These pin the two rules: dashboard code keeps the literal
rule, and served prose has no comments, so any name in it is delivered.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[1] / "scripts/dev/check_fleet_identity_leak.py"
_spec = importlib.util.spec_from_file_location("check_fleet_identity_leak_served", _GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

REPO = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# --- JavaScript: the Python rule --------------------------------------------


def test_js_label_literal_is_flagged(tmp_path):
    hits = guard.scan_served_file(_write(tmp_path, "a.js", 'if (inRoster("Watcher")) x();\n'))
    assert len(hits) == 1 and 'fleet identity "Watcher"' in hits[0]


def test_js_label_inside_template_interpolation_is_flagged(tmp_path):
    # The dashboard builds markup this way; an opaque-template scanner missed it.
    src = 'const p = `<div>${head("Sentinel", s)}</div>`;\n'
    hits = guard.scan_served_file(_write(tmp_path, "b.js", src))
    assert len(hits) == 1 and 'fleet identity "Sentinel"' in hits[0]


def test_js_nested_braces_inside_interpolation(tmp_path):
    src = 'const p = `${f({a: {b: "Vigil"}})} tail`;\nconst q = "Lumen";\n'
    names = [h.split('"')[1] for h in guard.scan_served_file(_write(tmp_path, "c.js", src))]
    assert names == ["Vigil", "Lumen"]


def test_js_comments_are_provenance(tmp_path):
    src = (
        "// Watcher's panel was gated here first\n"
        "/* Sentinel and Vigil build from\n   their own endpoints */\n"
        "const label = cfg.label;\n"
    )
    assert guard.scan_served_file(_write(tmp_path, "d.js", src)) == []


def test_js_name_inside_a_longer_literal_is_not_a_label(tmp_path):
    # Route paths and prose name the subsystem, they do not dispatch on a label.
    src = 'fetch("/v1/sentinel/summary"); log("Sentinel summary loaded");\n'
    assert guard.scan_served_file(_write(tmp_path, "e.js", src)) == []


def test_js_line_numbers_survive_multiline_templates(tmp_path):
    src = 'const a = `line1\nline2 ${x}\nline3`;\nconst b = "Lumen";\n'
    hits = guard.scan_served_file(_write(tmp_path, "f.js", src))
    assert len(hits) == 1 and ":4:" in hits[0]


def test_js_operator_domain_is_flagged(tmp_path):
    hits = guard.scan_served_file(_write(tmp_path, "g.js", 'const u = "https://gov.cirwel.org/x";\n'))
    assert len(hits) == 1 and 'operator domain "cirwel.org"' in hits[0]


# --- Prose: every word is delivered ------------------------------------------


def test_prose_name_anywhere_is_flagged(tmp_path):
    hits = guard.scan_served_file(_write(tmp_path, "SKILL.md", "Poll Lumen's sensors every minute.\n"))
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0]


def test_prose_word_boundaries(tmp_path):
    # "Sentinels" and "Vigilant" are ordinary words, not labels.
    assert guard.scan_served_file(_write(tmp_path, "x.md", "Sentinels stay vigilant; Vigilant.\n")) == []


def test_json_descriptions_are_prose(tmp_path):
    hits = guard.scan_served_file(_write(tmp_path, "t.json", '{"observe": "e.g. target_agent_id=\\"Lumen\\""}\n'))
    assert len(hits) == 1


def test_html_splits_markup_from_script(tmp_path):
    src = (
        "<html>\n<p>Welcome</p>\n"
        "<script>\n// Watcher comment is provenance\nconst a = \"Watcher\";\n</script>\n"
        "<p>Lumen</p>\n</html>\n"
    )
    hits = guard.scan_served_file(_write(tmp_path, "p.html", src))
    lines = sorted(int(h.split(":")[1]) for h in hits)
    assert lines == [5, 7]  # the literal on line 5, the markup on line 7; not the comment


# --- Discovery and triage ------------------------------------------------------


def test_served_files_cover_every_served_surface():
    rels = {p.relative_to(REPO).as_posix() for p in guard.served_files()}
    assert "src/tool_descriptions.json" in rels
    assert "dashboard/redesign/app.html" in rels
    assert "dashboard/redesign/data.js" in rels
    assert any(r.startswith("skills/") and r.endswith("/SKILL.md") for r in rels)
    # Attestation records are read by the server, not served as text.
    assert not any("/.attestations/" in r for r in rels)


def test_known_served_couplings_are_reported_not_failed():
    rel = next(iter(guard.SERVED_KNOWN_COUPLINGS))
    name_hit = f'  {rel}:1: fleet identity "Lumen" in served text'
    domain_hit = f'  {rel}:2: operator domain "cirwel.org" in served text'
    failing, deferred = guard.triage_served(rel, [name_hit, domain_hit])
    assert failing == [domain_hit] and deferred == [name_hit]
    assert guard.triage_served("skills/new/SKILL.md", [name_hit]) == ([name_hit], [])


def test_every_known_served_coupling_still_exists_and_still_couples():
    # A fixed entry must be deleted, not left to exempt a file that no longer needs it.
    for rel in guard.SERVED_KNOWN_COUPLINGS:
        path = REPO / rel
        assert path.is_file(), f"{rel} is listed in SERVED_KNOWN_COUPLINGS but does not exist"
        assert guard.scan_served_file(path), f"{rel} no longer names a resident; delete its entry"


def test_served_tree_has_no_new_leak():
    failing = []
    for path in guard.served_files():
        rel = path.relative_to(REPO).as_posix()
        failing += guard.triage_served(rel, guard.scan_served_file(path))[0]
    assert failing == []
