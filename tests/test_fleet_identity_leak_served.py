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


def test_prose_lowercase_label_is_flagged(tmp_path):
    # Labels are compared case-insensitively in code; prose must not be the bypass.
    hits = guard.scan_served_file(_write(tmp_path, "y.md", "observe(target_agent_id: lumen)\n"))
    assert len(hits) == 1


def test_css_is_served_prose(tmp_path):
    hits = guard.scan_served_file(_write(tmp_path, "t.css", '[data-resident="Lumen"] { color: red; }\n'))
    assert len(hits) == 1


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
    assert "dashboard/redesign/tokens.css" in rels  # the redesign route serves .css
    assert any(r.startswith("skills/") and r.endswith("/SKILL.md") for r in rels)
    # Attestation records are read by the server, not served as text.
    assert not any("/.attestations/" in r for r in rels)


def _occ(tmp_path, name, src):
    return guard.served_occurrences(_write(tmp_path, name, src))


def test_pinned_occurrence_is_deferred(tmp_path, monkeypatch):
    found = _occ(tmp_path, "a.js", 'if (inRoster("Watcher")) show();\n')
    monkeypatch.setitem(guard.SERVED_KNOWN_COUPLINGS, "a.js",
                        {"reason": "r", "occurrences": [list(found[0].key)]})
    assert guard.triage_served("a.js", found) == ([], [found[0].message])


def test_swapping_a_pinned_reference_for_a_new_one_fails(tmp_path, monkeypatch):
    # Review finding: a count ceiling passed a file that fixed one reference and
    # added a different one. Pins compare the occurrence, not the tally.
    before = _occ(tmp_path, "a.js", 'if (inRoster("Watcher")) show();\n')
    monkeypatch.setitem(guard.SERVED_KNOWN_COUPLINGS, "a.js",
                        {"reason": "r", "occurrences": [list(before[0].key)]})
    after = _occ(tmp_path, "a.js", 'if (inRoster("Sentinel")) show();\n')
    failing, deferred = guard.triage_served("a.js", after)
    assert len(failing) == 1 and "unpinned" in failing[0] and deferred == []
    assert guard.stale_pins("a.js", after) == [before[0].key]


def test_duplicating_a_pinned_reference_fails(tmp_path, monkeypatch):
    one = _occ(tmp_path, "a.js", 'x("Lumen");\n')
    monkeypatch.setitem(guard.SERVED_KNOWN_COUPLINGS, "a.js",
                        {"reason": "r", "occurrences": [list(one[0].key)]})
    two = _occ(tmp_path, "a.js", 'x("Lumen");\nx("Lumen");\n')
    failing, deferred = guard.triage_served("a.js", two)
    assert len(failing) == 1 and len(deferred) == 1


def test_domain_is_never_deferred(tmp_path, monkeypatch):
    found = _occ(tmp_path, "a.md", "Lumen lives at gov.cirwel.org\n")
    monkeypatch.setitem(guard.SERVED_KNOWN_COUPLINGS, "a.md",
                        {"reason": "r", "occurrences": [list(o.key) for o in found if o.key]})
    failing, deferred = guard.triage_served("a.md", found)
    assert len(failing) == 1 and "operator domain" in failing[0] and len(deferred) == 1
    unlisted_failing, unlisted_deferred = guard.triage_served("unlisted.md", found)
    assert len(unlisted_failing) == 2 and unlisted_deferred == []


def test_every_pin_is_still_present():
    # Pins only go away: a fix deletes its line, and the last fix the entry.
    for rel, entry in guard.SERVED_KNOWN_COUPLINGS.items():
        path = REPO / rel
        assert path.is_file(), f"{rel} is pinned but does not exist"
        assert entry.get("reason"), f"{rel} has no reason"
        stale = guard.stale_pins(rel, guard.served_occurrences(path))
        assert stale == [], f"{rel}: fixed, delete these pins: {stale}"


# --- Regex literals ------------------------------------------------------------


def test_quote_inside_regex_does_not_hide_the_next_literal(tmp_path):
    # Review finding: /["']/ opened a string that swallowed "Lumen".
    names = [h.split('"')[1] for h in guard.scan_served_file(
        _write(tmp_path, "r.js", 'const q = /["\']/; const label = "Lumen";\n'))]
    assert names == ["Lumen"]


def test_regex_after_keyword_and_division_are_told_apart(tmp_path):
    src = (
        'function f(s) { return /"/.test(s) ? "Vigil" : a / b / "x".length; }\n'
        'const r = s.replace(/[\'"]/g, ""), k = "Sentinel";\n'
    )
    names = [h.split('"')[1] for h in guard.scan_served_file(_write(tmp_path, "d.js", src))]
    assert names == ["Vigil", "Sentinel"]


def test_regex_class_may_hold_a_slash(tmp_path):
    names = [h.split('"')[1] for h in guard.scan_served_file(
        _write(tmp_path, "c.js", 'const p = /[/"]+/; const n = "Watcher";\n'))]
    assert names == ["Watcher"]


def test_served_tree_has_no_new_leak():
    failing = []
    for path in guard.served_files():
        rel = path.relative_to(REPO).as_posix()
        failing += guard.triage_served(rel, guard.served_occurrences(path))[0]
    assert failing == []
