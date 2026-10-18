"""Docs that state the required Postgres stack must match REQUIRED_EXTENSIONS.

The doc-health lint denylists stale wordings one at a time. This asserts the
correct claim instead: every extension the server requires is created by the
Docker init SQL and named where the docs state requirements.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.db.postgres_backend import REQUIRED_EXTENSIONS

REPO = Path(__file__).resolve().parent.parent

# How the contract docs' Stack section names each extension.
_STACK_NAMES = {"age": "AGE", "vector": "pgvector"}


@pytest.mark.parametrize("ext", REQUIRED_EXTENSIONS)
def test_docker_init_creates_required_extension(ext):
    sql = (REPO / "db/postgres/init-extensions.sql").read_text()
    pattern = rf'CREATE EXTENSION IF NOT EXISTS "?{re.escape(ext)}"?\s*;'
    assert re.search(pattern, sql), f"init-extensions.sql does not create {ext}"


def test_postgres_readme_lists_exactly_the_required_extensions():
    readme = (REPO / "db/postgres/README.md").read_text()
    match = re.search(r"^Required extensions: (.+?)\.", readme, re.MULTILINE)
    assert match, "db/postgres/README.md lost its 'Required extensions:' line"
    listed = re.findall(r"`([^`]+)`", match.group(1))
    assert listed == list(REQUIRED_EXTENSIONS)


def test_doctor_checks_the_same_extensions():
    tree = ast.parse((REPO / "scripts/dev/unitares_doctor.py").read_text())
    doctor_exts = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "REQUIRED_PG_EXTENSIONS" for t in node.targets)
    )
    assert set(doctor_exts) == set(REQUIRED_EXTENSIONS)


@pytest.mark.parametrize("doc", ["CLAUDE.md", "AGENTS.md"])
def test_contract_stack_section_names_required_extensions(doc):
    text = (REPO / doc).read_text()
    stack = text.split("## Stack", 1)[1].split("\n## ", 1)[0]
    for ext, name in _STACK_NAMES.items():
        assert ext in REQUIRED_EXTENSIONS
        assert name in stack, f"{doc} ## Stack does not name {name}"
