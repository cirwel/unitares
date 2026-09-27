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


def test_js_regex_literal_does_not_desync_string_scanning(tmp_path):
    # A regex literal containing a quote must not be read as opening a string
    # — the resident literal right after it must still be found.
    src = '/["\']/;\nconst label = "Lumen";\n'
    hits = guard.scan_served_file(_write(tmp_path, "r.js", src))
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0]


def test_js_division_is_not_mistaken_for_a_regex(tmp_path):
    # A genuine division must not be swallowed as an (unterminated) regex
    # body, which would desync scanning of what follows on the same line.
    src = 'const ratio = a / b; const label = "Lumen";\n'
    hits = guard.scan_served_file(_write(tmp_path, "s.js", src))
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0]


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


def test_script_end_tag_with_embedded_whitespace_is_recognized(tmp_path):
    # CodeQL: a script end tag like `</script\t\n bar>` must still close the
    # <script> block, or its content (here, a provenance comment) gets
    # rescanned under the stricter no-comments prose rule instead of the code
    # rule, and the markup after it goes unrecognized as markup.
    src = (
        "<script>\nconst a = cfg.label; // Watcher provenance comment\n"
        "</script\t\n bar>\n<p>Lumen</p>\n"
    )
    hits = guard.scan_served_file(_write(tmp_path, "q.html", src))
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0]


def test_served_files_cover_every_served_surface():
    rels = {p.relative_to(REPO).as_posix() for p in guard.served_files()}
    assert "src/tool_descriptions.json" in rels
    assert "dashboard/redesign/app.html" in rels
    assert "dashboard/redesign/data.js" in rels
    assert "dashboard/redesign/tokens.css" in rels  # the redesign route serves .css
    assert any(r.startswith("skills/") and r.endswith("/SKILL.md") for r in rels)
    # Attestation records are read by the server, not served as text.
    assert not any("/.attestations/" in r for r in rels)


def test_known_coupling_defers_up_to_its_known_occurrences():
    rel = "dashboard/redesign/snapshot.js"
    approved = guard.SERVED_KNOWN_COUPLINGS[rel][0]
    at_budget = [
        f'  {rel}:{k}: hardcoded fleet identity "{name}" in a string literal'
        for k, name in enumerate(approved)
    ]
    assert guard.triage_served(rel, at_budget) == ([], at_budget)
    # A partial fix (fewer than the recorded occurrences) still defers.
    fewer = at_budget[:-1]
    assert guard.triage_served(rel, fewer) == ([], fewer)
    # One more reference of an already-budgeted name is a NEW leak, not a pass.
    over = at_budget + [f'  {rel}:9999: hardcoded fleet identity "{approved[0]}" in a string literal']
    assert guard.triage_served(rel, over) == (over, [])


def test_known_coupling_name_substitution_is_caught():
    # The bug this pins: removing one known reference and adding a DIFFERENT
    # name in the same file must not pass just because the total count is
    # unchanged — the old ceiling-only check let this through.
    rel = "dashboard/redesign/snapshot.js"
    approved = list(guard.SERVED_KNOWN_COUPLINGS[rel][0])
    swapped = approved[:-1] + ["Steward"]  # a name never recorded for this file
    hits = [
        f'  {rel}:{k}: hardcoded fleet identity "{name}" in a string literal'
        for k, name in enumerate(swapped)
    ]
    failing, deferred = guard.triage_served(rel, hits)
    assert failing == hits and deferred == []


def test_domain_is_never_deferred_by_a_known_coupling():
    rel = next(iter(guard.SERVED_KNOWN_COUPLINGS))
    approved_name = guard.SERVED_KNOWN_COUPLINGS[rel][0][0]
    name_hit = f'  {rel}:1: fleet identity "{approved_name}" in served text'
    domain_hit = f'  {rel}:2: operator domain "cirwel.org" in served text'
    failing, deferred = guard.triage_served(rel, [name_hit, domain_hit])
    assert failing == [domain_hit] and deferred == [name_hit]
    assert guard.triage_served("skills/new/SKILL.md", [name_hit]) == ([name_hit], [])


def test_every_known_occurrence_is_exact():
    # The recorded multiset only ratchets down: a fix removes one entry from
    # it, and the last removal deletes the file's whole entry. A recorded set
    # that does not match reality would either fail to defer a legitimate
    # occurrence or, worse, silently budget for one that no longer exists
    # (freeing room for an unrelated new leak of the same name).
    from collections import Counter

    for rel, (approved, _reason) in guard.SERVED_KNOWN_COUPLINGS.items():
        path = REPO / rel
        assert path.is_file(), f"{rel} is listed in SERVED_KNOWN_COUPLINGS but does not exist"
        hits = [h for h in guard.scan_served_file(path) if "operator domain" not in h]
        assert hits, f"{rel} no longer names a resident; delete its entry"
        observed = Counter(guard._hit_value(h) for h in hits)
        approved_count = Counter(approved)
        assert observed == approved_count, (
            f"{rel}: observed {dict(observed)} != recorded {dict(approved_count)}; "
            "update SERVED_KNOWN_COUPLINGS to match reality"
        )


def test_served_tree_has_no_new_leak():
    failing = []
    for path in guard.served_files():
        rel = path.relative_to(REPO).as_posix()
        failing += guard.triage_served(rel, guard.scan_served_file(path))[0]
    assert failing == []
