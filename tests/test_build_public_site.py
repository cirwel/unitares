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
    assert "single-operator deployment" in ledger_text
    assert "did not establish predictive lift" in ledger_text
    assert "read-specific power" in ledger_text
    assert "inconclusive, not a demonstrated negative" in ledger_text
    # The index page is a directory of generated repository artifacts, not a
    # rival product landing page alongside cirwel.org and README.md.
    assert "<title>UNITARES — Repository artifacts</title>" in landing
    assert "UNITARES repository artifacts" in landing_text
    assert 'class="brand" href="https://cirwel.org"' in landing
    assert 'href="index.html" class="active">Artifacts</a>' in landing
    assert 'href="https://cirwel.org">cirwel.org ↗</a>' in landing
    assert 'href="demo.html"' in landing
    assert 'href="glossary.html"' in landing
    assert 'href="glossary-viewer.html"' in landing
    assert 'href="drift-audit.html"' in landing
    assert "brew install" not in landing_text
    assert "docker compose up" not in landing_text
    assert "Do not install UNITARES" not in landing_text
    assert "github.com/cirwel/unitares/discussions" in landing
    assert "https://pypi.org/project/unitares-sdk/" in landing
    assert f"releases/tag/v{server_version}" in landing
    assert f"ghcr.io/cirwel/unitares:v{server_version}" in landing
    assert "field is inventing terms" not in landing

    assert "UNITARES Glossary" in glossary
    assert "field is inventing terms" in glossary
    assert 'href="index.html">Artifacts</a>' in glossary
    assert 'class="brand" href="https://cirwel.org"' in glossary

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
    # Strip the embedded interactive widget (and its JSON payload) before
    # verifying that demo.md itself renders every oracle fact and link.
    demo_static = re.sub(r'<section class="demo-stage".*?</script>', "", demo, flags=re.S)
    demo_text = " ".join(demo_static.split())
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
    # The page shows how the record fits alongside Git, GitHub, and handoffs, and links the evidence.
    assert "works alongside repository history, pull requests, and session handoff notes" in demo_text
    assert "docs/evaluations/accountability-journey/result-v0.md" in demo
    assert "docs/EVIDENCE_AND_LIMITS.md" in demo
    assert "docs/guides/AGENT_SESSION_WALKTHROUGH.md" in demo
    assert "Probably not if one repository" not in demo_text
    # Reachable from the artifact directory and the nav.
    assert 'href="demo.html"' in landing
    # The viewer has its own nav template; it links cirwel.org and the demo too.
    viewer_html = (tmp_path / "glossary-viewer.html").read_text(encoding="utf-8")
    assert 'href="demo.html"' in viewer_html
    assert 'class="brand" href="https://cirwel.org"' in viewer_html
    assert 'href="index.html">Artifacts</a>' in viewer_html
    assert 'href="demo.html" class="active"' in demo
    # Interactive explorer is embedded single-source from demo.md and incident-v0.json,
    # starts hidden for clean no-JS degradation, and keeps <noscript> outside it.
    assert '<section class="demo-stage" id="demo-stage" aria-label="Interactive incident walkthrough" hidden>' in demo
    assert "</section>\n<noscript>" in demo
    assert "__DEMO_DATA_JSON__" not in demo
    assert '"incident_id": "reload-claim-contradiction-v0"' in demo
    assert 'id="demo-records"' in demo


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


def test_demo_interactive_escapes_script_close_and_requires_placeholder():
    import json
    import pytest

    from scripts.dev.build_public_site import (
        JOURNEY_FIXTURE,
        SITE,
        build_demo_interactive_payload,
        inject_demo_interactive,
        render_demo_interactive,
        render_demo_md,
    )

    rendered = render_demo_interactive(
        "<script>const X = __DEMO_DATA_JSON__;</script>",
        {"close": "</script><script>alert(1)</script>", "comment": "<!--<script>x"},
    )
    assert "</script><script>" not in rendered
    assert "<!--<script" not in rendered
    assert "\\u003c/script\\u003e" in rendered
    assert "\\u003c!--\\u003cscript" in rendered
    assert "__DEMO_DATA_JSON__" not in rendered

    with pytest.raises(ValueError):
        render_demo_interactive("<script>const X = {};</script>", {})

    # Fail-closed validation: renaming a record id, adding an unmapped relation,
    # dropping a numbered step section from demo.md, or missing <h2> placement
    # must fail the build.
    base_fixture = json.loads(JOURNEY_FIXTURE.read_text(encoding="utf-8"))
    filled_md = render_demo_md((SITE / "demo.md").read_text(encoding="utf-8"), base_fixture)
    payload = build_demo_interactive_payload(filled_md, base_fixture)
    # Steps 1-4 preserve lead_in/quote/detail separately (no colon splicing); step 5 has no fake quote.
    assert payload["steps"][0]["lead_in"].endswith(":")
    assert payload["steps"][0]["quote"] == base_fixture["oracle"]["facts"]["original_claim"]
    assert payload["steps"][0]["detail"].startswith("The record binds")
    assert payload["steps"][4]["quote"] == ""

    bad_record = json.loads(json.dumps(base_fixture))
    bad_record["arms"]["unitares"]["records"][0]["record_id"] = "renamed-record"
    with pytest.raises(ValueError, match="unknown record"):
        build_demo_interactive_payload(filled_md, bad_record)

    dup_record = json.loads(json.dumps(base_fixture))
    dup_record["arms"]["unitares"]["records"].append(dup_record["arms"]["unitares"]["records"][0])
    with pytest.raises(ValueError, match="duplicate record_id"):
        build_demo_interactive_payload(filled_md, dup_record)

    extra_record = json.loads(json.dumps(base_fixture))
    extra_record["arms"]["unitares"]["records"].append({"record_id": "extra-record", "kind": "note", "facts": {}})
    with pytest.raises(ValueError, match="never shown"):
        build_demo_interactive_payload(filled_md, extra_record)

    bad_relation = json.loads(json.dumps(base_fixture))
    bad_relation["oracle"]["edges"].append(
        {"source": "a", "relation": "extra_unmapped", "target": "b"}
    )
    with pytest.raises(ValueError, match="never shown"):
        build_demo_interactive_payload(filled_md, bad_relation)

    bad_md = filled_md.replace("## 3. A reviewer challenges it", "## Unnumbered")
    with pytest.raises(ValueError, match="missing section"):
        build_demo_interactive_payload(bad_md, base_fixture)

    subhead_md = filled_md.replace("## 3. A reviewer challenges it", "### Subheading")
    with pytest.raises(ValueError, match="unsupported by textContent"):
        build_demo_interactive_payload(subhead_md, base_fixture)

    bad_md_syntax = filled_md.replace("The record binds", "The **record** binds")
    with pytest.raises(ValueError, match="unsupported by textContent"):
        build_demo_interactive_payload(bad_md_syntax, base_fixture)

    # Hard-wrapped link across lines must still be caught after joining.
    wrapped_link_md = filled_md.replace(
        "The record binds the finding to the process that wrote it",
        "The [record\nbinds](https://example.org) the finding to the process that wrote it",
    )
    with pytest.raises(ValueError, match="unsupported by textContent"):
        build_demo_interactive_payload(wrapped_link_md, base_fixture)

    extra_section_md = filled_md.replace(
        "## Working with Git",
        "## 6. Extra step\n\nExtra text.\n\n## Working with Git",
    )
    with pytest.raises(ValueError, match="unmapped numbered section"):
        build_demo_interactive_payload(extra_section_md, base_fixture)

    dup_section_md = filled_md.replace(
        "## 4. A correction replaces it",
        "## 3. A correction replaces it",
    )
    with pytest.raises(ValueError, match="duplicate section"):
        build_demo_interactive_payload(dup_section_md, base_fixture)

    bad_record_edge = json.loads(json.dumps(base_fixture))
    bad_record_edge["arms"]["unitares"]["records"][4]["edges"][0]["relation"] = "replaces"
    with pytest.raises(ValueError, match="missing from visible records"):
        build_demo_interactive_payload(filled_md, bad_record_edge)

    with pytest.raises(ValueError, match="<h2>"):
        inject_demo_interactive("<h1>Title</h1><p>No section heading</p>", "<section/>")


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


def test_build_rewrites_relative_markdown_links_to_github_blobs(tmp_path):
    import re

    from scripts.dev.build_public_site import ONTOLOGY, rewrite_repo_relative_links

    build(tmp_path, None)

    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")
    assert (
        'href="https://github.com/cirwel/unitares/blob/master/'
        'docs/ontology/eisv-proprioception-contract.md#canonical-operational-doctrine"'
    ) in glossary

    for html_page in sorted(tmp_path.glob("*.html")):
        content = html_page.read_text(encoding="utf-8")
        relative_md_hrefs = re.findall(
            r'href="(?!https?://|mailto:|#)([^"#]+\.md(?:#[^"]*)?)"',
            content,
        )
        assert relative_md_hrefs == [], f"{html_page.name} has unresolved .md hrefs: {relative_md_hrefs}"

    sample = "Prose [link](glossary.md)\n```markdown\n[code](glossary.md)\n```\n"
    rewritten = rewrite_repo_relative_links(sample, ONTOLOGY)
    assert "https://github.com/cirwel/unitares/blob/master/docs/ontology/glossary.md" in rewritten
    assert "[code](glossary.md)" in rewritten

