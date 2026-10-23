"""Regression tests for the generated public GitHub Pages site."""

from pathlib import Path

from scripts.dev.build_public_site import build


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_separates_product_landing_from_glossary(tmp_path):
    build(tmp_path, None)

    landing = (tmp_path / "index.html").read_text(encoding="utf-8")
    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")
    server_version = (PROJECT_ROOT / "PUBLISHED_VERSION").read_text(encoding="utf-8").strip()
    landing_text = " ".join(landing.split())

    # The landing copies the README's tagline and definition verbatim; cirwel.org
    # pins the same strings against the README (its claims register), so one
    # sentence stays one sentence across every surface.
    assert "Accountability infrastructure for long-running AI agents" in landing_text
    readme_text = " ".join((PROJECT_ROOT / "README.md").read_text(encoding="utf-8").split())
    definition = (
        "Its single-operator federation kernel connects independent runtimes to one "
        "operator-controlled server over MCP or HTTP"
    )
    assert definition in readme_text
    assert definition in landing_text
    # The landing states what runs and links the ledger; the scope wording
    # lives in the ledger, where a reviewer reads it beside the data.
    assert "docs/EVIDENCE_AND_LIMITS.md" in landing
    ledger_text = " ".join(
        (PROJECT_ROOT / "docs" / "EVIDENCE_AND_LIMITS.md").read_text(encoding="utf-8").split()
    )
    assert "External adoption remains unvalidated" in ledger_text
    assert "did not establish predictive lift" in ledger_text
    assert "read-specific power" in ledger_text
    assert "inconclusive, not a demonstrated negative" in ledger_text
    assert "its claims, evidence, and behavior drift apart" in landing_text
    assert "Claims and evidence" in landing
    assert 'class="hero-actions"' in landing
    assert 'href="#try-the-released-surfaces"' in landing
    assert 'href="#evaluate-the-project"' in landing
    assert "github.com/cirwel/unitares/discussions" in landing
    assert "https://pypi.org/project/unitares-sdk/" in landing
    assert f"releases/tag/v{server_version}" in landing
    assert f"ghcr.io/cirwel/unitares:v{server_version}" in landing
    assert "field is inventing terms" not in landing

    assert "UNITARES Glossary" in glossary
    assert "field is inventing terms" in glossary
    assert 'href="index.html"' in glossary

    assert (tmp_path / "drift-audit.html").is_file()
    assert (tmp_path / "favicon.svg").is_file()
    assert 'rel="icon" href="favicon.svg"' in landing
    assert "docs/public-site/index.md" in landing


def test_build_renders_readonly_demo_from_journey_fixture(tmp_path):
    import html
    import json
    import re

    build(tmp_path, None)

    demo = (tmp_path / "demo.html").read_text(encoding="utf-8")
    landing = (tmp_path / "index.html").read_text(encoding="utf-8")
    demo_text = " ".join(demo.split())
    fixture = json.loads(
        (PROJECT_ROOT / "tests" / "accountability_journey" / "incident-v0.json").read_text(
            encoding="utf-8"
        )
    )

    # Every fact on the page comes from the fixture the journey test runs on.
    for value in fixture["oracle"]["facts"].values():
        assert str(value) in demo_text
    # Each question's own row carries its answer and the record that holds it.
    facts = fixture["oracle"]["facts"]
    holders = {}
    for record in fixture["arms"]["unitares"]["records"]:
        for key in record.get("facts", {}):
            holders.setdefault(key, record["record_id"])
    cells = {}
    for row in re.findall(r"<tr>(.*?)</tr>", demo, re.S):
        tds = re.findall(r"<td>(.*?)</td>", row, re.S)
        if len(tds) == 3:
            cells[tds[0]] = tds[1:]
    for question in fixture["questions"]:
        label = question["question_id"].replace("_", " ").capitalize() + "?"
        assert cells.get(label) == [
            html.escape(str(facts[question["fact_key"]]), quote=False),
            f"<code>{holders[question['fact_key']]}</code>",
        ], label
    # The link names come from the fixture's edges.
    for edge in fixture["oracle"]["edges"]:
        assert f"<code>{edge['relation']}</code>" in demo
    assert "{{" not in demo and "{fixture_blob}" not in demo
    # The page shows the mechanism and says plainly it is not a win over git.
    assert "docs/evaluations/accountability-journey/result-v0.md" in demo
    assert "both scored 1.000" in demo_text
    # Reachable from the landing hero and the nav.
    assert 'class="primary" href="demo.html"' in landing
    # The viewer has its own nav template; it links the demo too.
    assert 'href="demo.html"' in (tmp_path / "glossary-viewer.html").read_text(encoding="utf-8")
    assert 'href="demo.html" class="active"' in demo


def test_demo_build_fails_on_unfilled_placeholder():
    import pytest

    from scripts.dev.build_public_site import render_demo_md

    fixture = {
        "oracle": {"facts": {"a": "x"}},
        "questions": [],
        "arms": {"unitares": {"records": []}},
    }
    with pytest.raises(ValueError):
        render_demo_md("{{facts.missing}}", fixture)
    fixture["questions"] = [{"question_id": "who", "fact_key": "a"}]
    with pytest.raises(ValueError):
        render_demo_md("{{questions_table}}", fixture)
    # A record whose value disagrees with the oracle fails, not the oracle shown.
    fixture["questions"] = [{"question_id": "who", "fact_key": "a"}]
    fixture["arms"]["unitares"]["records"] = [{"record_id": "r1", "facts": {"a": "y"}}]
    with pytest.raises(ValueError):
        render_demo_md("{{questions_table}}", fixture)
    fixture["arms"]["unitares"]["records"] = []
    # A mistyped link name would publish a broken relative link.
    fixture["questions"] = []
    with pytest.raises(ValueError):
        render_demo_md("[the result]({result_blobb})", fixture)


def test_demo_link_names_must_match_the_fixture_edges():
    import pytest

    from scripts.dev.build_public_site import render_demo_md

    fixture = {
        "oracle": {"facts": {}, "edges": [{"source": "p", "relation": "authored", "target": "c"}]},
        "questions": [],
        "arms": {"unitares": {"records": []}},
    }
    assert render_demo_md("an `{{relation.authored}}` link", fixture) == "an `authored` link"
    # A relation the fixture does not have stays unfilled and fails the build.
    with pytest.raises(ValueError):
        render_demo_md("{{relation.authored}} {{relation.grades}}", fixture)
    # A fixture relation the page never names fails it too.
    with pytest.raises(ValueError):
        render_demo_md("no links here", fixture)


def test_build_generates_viewer_from_glossary_markdown(tmp_path):
    build(tmp_path, None)

    viewer = (tmp_path / "glossary-viewer.html").read_text(encoding="utf-8")

    # Template tokens replaced with real data parsed from glossary.md.
    assert "__GLOSSARY_DATA_JSON__" not in viewer
    assert "__REPO_URL__" not in viewer
    assert "substrate (inference)" in viewer
    assert '"rosetta"' in viewer

    # The other pages link to the viewer.
    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")
    assert 'href="glossary-viewer.html"' in glossary


def test_build_writes_normalized_cname(tmp_path):
    build(tmp_path, " docs.example.org ")

    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == (
        "docs.example.org\n"
    )
