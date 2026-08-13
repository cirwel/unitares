"""Tests for the glossary.md structured parser and the drift check.

The parser (scripts/dev/glossary_data.py) feeds the generated public-site
viewer; the checker (scripts/dev/check_glossary_drift.py) is the CI gate that
replaces manual drift sweeps for everything a machine can verify. These tests
run both against the real repository files, so they double as a regression
gate on docs/ontology/glossary.md's structure.
"""

from scripts.dev.check_glossary_drift import main as drift_check_main
from scripts.dev.glossary_data import parse_glossary


def test_parse_glossary_structure():
    data = parse_glossary()

    homonyms = {t["term"]: t for t in data["homonyms"]}
    assert len(homonyms) >= 8
    assert len(homonyms["substrate"]["senses"]) == 3
    # Code-level flags come from the section headings.
    assert homonyms["basin"]["code_level"] is True
    assert homonyms["free energy"]["code_level"] is True
    assert homonyms["substrate"]["code_level"] is False
    # Every sense carries the full question-keyed triple.
    for term in data["homonyms"]:
        for sense in term["senses"]:
            assert sense["name"] and sense["question"] and sense["source"]

    single_terms = {t["term"] for t in data["single_sense"]}
    assert "EISV" in single_terms
    assert "lease" in single_terms

    assert len(data["open_gaps"]) >= 1
    assert all(g["title"] for g in data["open_gaps"])

    rosetta = data["rosetta"]
    assert "Referent" in rosetta["columns"]
    assert "fep" in rosetta["columns"]
    assert len(rosetta["rows"]) >= 5
    assert all(len(r) == len(rosetta["columns"]) for r in rosetta["rows"])


def test_parse_glossary_strips_markdown_emphasis():
    data = parse_glossary()
    blob = str(data)
    assert "**" not in blob
    assert "`" not in blob


def test_glossary_drift_check_passes_on_current_tree():
    assert drift_check_main() == 0
