"""Every Cypher builder in src/db/age_queries.py must RETURN exactly one column.

``graph_query`` wraps the Cypher in ``cypher(...) AS (result agtype)``: one
result column. A builder whose final RETURN projects two expressions fails on
column arity when it runs, which is how concept extraction failed on every run
for months (KG 2026-10-20T03:44:57.244104+00:00) and how the cross-agent
knowledge-flow query could never have worked. A row with several fields is a
map literal, ``RETURN {a: x, b: y}``, unpacked in Python.

This test reads every public builder in the module, so a new builder is
checked without anyone remembering to add it here.
"""

from __future__ import annotations

import inspect
import re

import pytest

from src.db import age_queries

# Extra calls for builders whose optional arguments change the Cypher text.
EXTRA_CALLS = {
    "query_agent_discoveries": [{"discovery_type": "note", "status": "open"}],
}

_CLAUSE_END = re.compile(r"\b(ORDER\s+BY|SKIP|LIMIT|UNION)\b", re.IGNORECASE)
_RETURN = re.compile(r"\bRETURN\b(\s+DISTINCT\b)?", re.IGNORECASE)


def _builders():
    return sorted(
        (name, func)
        for name, func in inspect.getmembers(age_queries, inspect.isfunction)
        if func.__module__ == age_queries.__name__
        and not name.startswith("_")
        and name.startswith(("create_", "query_"))
    )


def _required_args(func) -> dict:
    args = {}
    for name, param in inspect.signature(func).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        args[name] = f"test-{name}"
    return args


def _blank_strings(cypher: str) -> str:
    """Replace string literal contents so quotes cannot hide or fake a comma."""
    return re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", cypher)


def _depths(text: str) -> list[int]:
    depth, out = 0, []
    for char in text:
        if char in "({[":
            depth += 1
        out.append(depth)
        if char in ")}]":
            depth -= 1
    return out


def top_level_return_columns(cypher: str) -> list[str]:
    """The expressions projected by the final top-level RETURN clause."""
    text = _blank_strings(cypher)
    depths = _depths(text)
    returns = [m for m in _RETURN.finditer(text) if depths[m.start()] == 0]
    if not returns:
        return []
    start = returns[-1].end()
    body = text[start:]
    body_depths = depths[start:]
    end = len(body)
    for match in _CLAUSE_END.finditer(body):
        if body_depths[match.start()] == 0:
            end = match.start()
            break
    columns, current = [], ""
    for char, depth in zip(body[:end], body_depths[:end]):
        if char == "," and depth == 0:
            columns.append(current.strip())
            current = ""
        else:
            current += char
    columns.append(current.strip())
    return [column for column in columns if column]


def test_every_public_function_is_a_checked_builder():
    public = {
        name
        for name, func in inspect.getmembers(age_queries, inspect.isfunction)
        if func.__module__ == age_queries.__name__ and not name.startswith("_")
    }
    assert public == {name for name, _ in _builders()}, (
        "a public function in age_queries is neither create_* nor query_*; "
        "name it so this test checks it, or make it private"
    )
    assert len(public) > 10


@pytest.mark.parametrize("name, func", _builders(), ids=[name for name, _ in _builders()])
def test_builder_returns_one_column(name, func):
    calls = [_required_args(func)]
    calls += [{**_required_args(func), **extra} for extra in EXTRA_CALLS.get(name, [])]
    for kwargs in calls:
        cypher, params = func(**kwargs)
        assert isinstance(params, dict)
        columns = top_level_return_columns(cypher)
        assert len(columns) == 1, (
            f"{name}({kwargs}) RETURNs {len(columns)} columns {columns}; "
            "graph_query reads one. Return a map literal instead: RETURN {a: x, b: y}"
        )


@pytest.mark.parametrize(
    "cypher, expected",
    [
        ("MATCH (d) RETURN d", ["d"]),
        ("MATCH (d) RETURN {a: d.x, b: d.y} ORDER BY d.x LIMIT 3", ["{a: d.x, b: d.y}"]),
        ("MATCH (d) RETURN d.a, d.b", ["d.a", "d.b"]),
        ("MATCH (d) WHERE d.s = 'x, y' RETURN count(d)", ["count(d)"]),
        ("MATCH (d) RETURN DISTINCT d.a AS a, d.b ORDER BY a", ["d.a AS a", "d.b"]),
        ("CALL { MATCH (x) RETURN x.a, x.b } RETURN [1, 2]", ["[1, 2]"]),
    ],
)
def test_return_parser(cypher, expected):
    assert top_level_return_columns(cypher) == expected
