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

import pytest

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
    # Any still-listed entry works. Earlier examples (snapshot.js, PLAN.md)
    # left the list when their files stopped naming residents.
    rel = "src/tool_descriptions.json"
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
    # Any still-listed entry works. Earlier examples (snapshot.js, PLAN.md)
    # left the list when their files stopped naming residents.
    rel = "src/tool_descriptions.json"
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


# --- Tool input schemas: Python literals that are served prose -----------------
#
# A Field's description= and json_schema_extra "brief", and a schema model's
# docstring, are Python literals that every agent is served as tool-schema
# text. The literal rule fires only on a literal that IS a name; these get the
# served-prose rule instead.


def _schema(tmp_path: Path, source: str, *, served: bool = True) -> list[str]:
    return guard.scan_file(_write(tmp_path, "schema.py", source), served_schema=served)


# The text a Codex review on #2489 caught in observe.target_agent_id. The
# guard passed it, because no single literal in it IS a name.
_OBSERVE_2489 = (
    "from pydantic import BaseModel, Field\n"
    "class ObserveParams(BaseModel):\n"
    "    target_agent_id: str = Field(\n"
    "        None,\n"
    "        description=(\n"
    '            "Agent to observe or filter by. That is a UUID for most "\n'
    '            "agents, but some audit writers are stored by name (e.g. system, "\n'
    '            "sentinel). Use list_agents to find."\n'
    "        ),\n"
    "    )\n"
)


def test_schema_description_naming_a_resident_is_flagged(tmp_path):
    hits = _schema(tmp_path, _OBSERVE_2489)
    assert len(hits) == 1
    assert 'fleet identity "sentinel" in served schema text' in hits[0]
    assert ":6:" in hits[0]  # where the literal starts
    # The literal rule alone passes the same text: this is the gap.
    assert _schema(tmp_path, _OBSERVE_2489, served=False) == []


def test_schema_brief_naming_a_resident_is_flagged(tmp_path):
    src = (
        "from pydantic import Field\n"
        "x = Field(None, description='Findings to list.',\n"
        "          json_schema_extra={'brief': 'Findings, e.g. from Watcher.'})\n"
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1 and 'fleet identity "Watcher" in served schema text' in hits[0]


@pytest.mark.parametrize("source, name", [
    ("x = Field(None, json_schema_extra=dict(brief='Ask Lumen first.'))\n", "Lumen"),
    ("model_config = ConfigDict(title='Watcher findings')\n", "Watcher"),
])
def test_schema_text_built_by_a_constructor_call_is_flagged(tmp_path, source, name):
    """Review on #2536: dict(brief=...) and ConfigDict(title=...) build the
    same served text as the literal forms, so they are read the same way."""
    hits = _schema(tmp_path, "from pydantic import ConfigDict, Field\n" + source)
    assert len(hits) == 1 and f'fleet identity "{name}" in served schema text' in hits[0]


def test_schema_text_in_class_keywords_is_flagged(tmp_path):
    """Review on #2536: class P(BaseModel, title=...) serves that title."""
    hits = _schema(tmp_path, "from pydantic import BaseModel\n"
                             "class P(BaseModel, title='Ask Lumen'):\n    x: int = 0\n")
    assert len(hits) == 1 and 'fleet identity "Lumen" in served schema text' in hits[0]


@pytest.mark.parametrize("body", [
    "    x: str = Field(default='Ask Lumen first')\n",
    "    x: str = Field('Ask Lumen first')\n",
    "    x: str = 'Ask Lumen first'\n",
    "    _DESC = 'Ask Lumen first'\n    x: str = Field(None, description=_DESC)\n",
])
def test_defaults_and_class_scoped_constants_are_served_text(tmp_path, body):
    """Review on #2536: a non-null default is kept in the advertised schema,
    and a class-scoped constant reaches it as a module one does."""
    hits = _schema(tmp_path, "from pydantic import BaseModel, Field\nclass P(BaseModel):\n" + body)
    assert len(hits) == 1 and 'fleet identity "Lumen" in served schema text' in hits[0]


def test_schema_model_docstring_is_served_other_docstrings_are_not(tmp_path):
    # Pydantic serves a model's docstring as the schema's description. A
    # module docstring, a validator's docstring and a comment are not served.
    src = (
        '"""Module notes: Lumen\'s sensors set these limits."""\n'
        "from pydantic import BaseModel, field_validator\n"
        "class ReadParams(BaseModel):\n"
        '    """Read state, as Chronicler does nightly."""\n'
        "    # Vigil's cadence set this default: provenance, not served.\n"
        "    limit: int = 5\n"
        "    @field_validator('limit')\n"
        "    def _check(cls, v):\n"
        '        """Steward once sent 0 here."""\n'
        "        return v\n"
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1
    assert 'fleet identity "Chronicler" in served schema text' in hits[0] and ":4:" in hits[0]


def test_schema_description_read_through_a_module_constant(tmp_path):
    # knowledge.py builds discovery_type's description this way.
    src = (
        "from pydantic import Field\n"
        '_KINDS = ("note", "insight")\n'
        '_DESC = "Type of finding, as Lumen files them. One of: " + ", ".join(_KINDS)\n'
        "x = Field(None, description=_DESC)\n"
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0] and ":3:" in hits[0]


def test_schema_title_naming_a_resident_is_flagged(tmp_path):
    # Served when UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep keeps titles.
    src = (
        "from pydantic import Field\n"
        'x = Field(None, title="Ask Lumen")\n'
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0]


def test_a_constant_reassigned_after_the_model_still_counts(tmp_path):
    # The class body captured the first value; the later assignment does not
    # change what the schema serves.
    src = (
        "from pydantic import BaseModel, Field\n"
        '_DESC = "Ask Lumen"\n'
        "class P(BaseModel):\n"
        "    x: str = Field(description=_DESC)\n"
        '_DESC = "safe"\n'
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0] and ":2:" in hits[0]


def test_schema_text_is_read_in_every_piece(tmp_path):
    src = (
        "from pydantic import Field\n"
        "n = 3\n"
        'a = Field(None, description="Filter by agent; " + "e.g. Steward.")\n'
        'b = Field(None, description=f"Last {n} check-ins from vigil.")\n'
        "c = Field(None, examples=['observe Lumen'])\n"
    )
    names = sorted(h.split('"')[1] for h in _schema(tmp_path, src))
    assert names == ["Lumen", "Steward", "vigil"]


def test_alias_override_text_is_served(tmp_path):
    src = (
        "OVERRIDES = {\n"
        '    "observe": {"target_agent_id": {\n'
        '        "description": "Agent to observe.",\n'
        '        "brief": "Agent to observe, e.g. Lumen.",\n'
        "    }},\n"
        "}\n"
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1 and 'fleet identity "Lumen"' in hits[0] and ":4:" in hits[0]


def test_schema_text_reports_the_domain_and_the_name(tmp_path):
    # Served prose reports both on one line; schema text must not drop one.
    src = (
        "from pydantic import Field\n"
        "x = Field(None, description='Ask Lumen at https://gov.cirwel.org/mcp/.')\n"
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 2
    assert any('operator domain "cirwel.org"' in h for h in hits)
    assert any('fleet identity "Lumen" in served schema text' in h for h in hits)
    failing, deferred = guard.triage("src/mcp_handlers/schemas/x.py", hits)
    assert failing == hits and deferred == []


def test_schema_text_keeps_ordinary_english(tmp_path):
    # Same word boundaries as served prose: these are words, not labels.
    src = (
        "from pydantic import Field\n"
        "x = Field(None, description='Sentinels and watchers stay vigilant; "
        "stewardship of the chronicle.')\n"
    )
    assert _schema(tmp_path, src) == []


def test_schema_module_code_literals_keep_the_literal_rule(tmp_path):
    # Only served text is prose. A Literal value and an error message in the
    # same module are code: a literal that IS a name still fails, one that
    # merely contains one (a subsystem name) does not.
    src = (
        "from typing import Literal\n"
        "from pydantic import Field\n"
        'Kind = Literal["summary", "Lumen"]\n'
        'ERR = "Sentinel summary requires a window"\n'
        "x: Kind = Field(None, description='Which view.')\n"
    )
    hits = _schema(tmp_path, src)
    assert len(hits) == 1 and 'hardcoded fleet identity "Lumen" in a string literal' in hits[0]


def test_schema_modules_are_recognized_by_path():
    assert guard.is_served_schema_module("src/mcp_handlers/schemas/observability.py")
    assert guard.is_served_schema_module("src/alias_schema.py")
    assert not guard.is_served_schema_module("src/mcp_handlers/observability/handlers.py")
    assert not guard.is_served_schema_module("src/http_routes/sentinel.py")


def test_served_schema_text_is_never_deferred_by_a_name_exemption():
    # NOT_IDENTITIES and KNOWN_COUPLINGS are about what code does with a word;
    # prose has no such job to point to, so neither defers it.
    for rel in (
        next(iter(guard.NOT_IDENTITIES)),
        next(iter(guard.KNOWN_COUPLINGS)),
        "src/mcp_handlers/schemas/core.py",
    ):
        hit = f'  {rel}:1: fleet identity "Sentinel" in served schema text'
        assert guard.triage(rel, [hit]) == ([hit], [])


def test_shipped_schema_text_has_no_resident_name():
    hits = []
    for root in guard.DEFAULT_PATHS:
        for path in sorted((REPO / root).rglob("*.py")):
            hits += [h for h in guard.scan_file(path) if "served schema text" in h]
    assert hits == []


def _classes_serving_schema_text(model) -> set[type]:
    """The model, every model or enum its fields nest, and their bases."""
    import enum
    import typing

    from pydantic import BaseModel

    found: set[type] = set()
    stack: list = [model]
    while stack:
        t = stack.pop()
        if isinstance(t, type) and issubclass(t, (BaseModel, enum.Enum)):
            if t in found:
                continue
            found.add(t)
            if issubclass(t, BaseModel):
                stack.extend(f.annotation for f in t.model_fields.values())
        stack.extend(typing.get_args(t))
    return {base for cls in found for base in cls.__mro__}


def test_the_rule_reads_every_module_that_defines_served_schema_text():
    # The static rule picks its files by path. Every class whose docstring or
    # fields reach a served schema must live in one of them, or its text is
    # read by the literal rule only.
    from src.tool_schemas import get_pydantic_schemas

    modules = {
        cls.__module__
        for model in get_pydantic_schemas().values()
        for cls in _classes_serving_schema_text(model)
        if cls.__module__.startswith("src.")
    }
    assert "src.mcp_handlers.schemas.mixins" in modules  # bases are followed
    unread = sorted(m for m in modules if not guard.is_served_schema_module(m.replace(".", "/") + ".py"))
    assert unread == []


def test_built_schemas_carry_no_resident_name():
    # The static rule reads the text where it is written. This reads it as
    # built, from the schemas Pydantic generates and the alias overrides, so a
    # text the static rule cannot follow (an imported constant, a computed
    # string) is still caught here.
    from src.alias_schema import ALIAS_SCHEMA_PROPERTY_OVERRIDES
    from src.tool_schemas import get_pydantic_schemas

    texts: list[tuple[str, str]] = []

    def every_string(node):
        # Structured examples ({"agent": ...} inside an examples list) are
        # served too, so a served value is read to any depth.
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for key, value in node.items():
                yield from every_string(key)
                yield from every_string(value)
        elif isinstance(node, list):
            for value in node:
                yield from every_string(value)

    def walk(node, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in guard.SERVED_SCHEMA_KEYS:
                    # Whatever its shape (a string, a list, or a mapping such
                    # as examples={...}), every string in it is served.
                    texts.extend((where, v) for v in every_string(value))
                else:
                    walk(value, where)
        elif isinstance(node, list):
            for value in node:
                walk(value, where)

    for tool, model in sorted(get_pydantic_schemas().items()):
        walk(model.model_json_schema(), tool)
    walk(ALIAS_SCHEMA_PROPERTY_OVERRIDES, "alias overrides")
    assert len(texts) > 400  # the walk read the schemas, not nothing
    leaks = [(where, text) for where, text in texts if guard._NAME_WORD.search(text)]
    assert leaks == []
