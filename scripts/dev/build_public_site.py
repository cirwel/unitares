#!/usr/bin/env python3
"""Build the public UNITARES site from canonical repository markdown.

Single-source by construction: this renders ``docs/public-site/index.md``,
``docs/public-site/demo.md`` (filled from the accountability-journey test
fixture), ``docs/ontology/glossary.md``, and the latest
``glossary-drift-audit-*.md`` directly to HTML, and generates the interactive glossary viewer from a
structured parse of the same ``glossary.md`` (``scripts/dev/glossary_data.py``).
It never holds its own copy of the page content, so the published site cannot
drift from the repository markdown.

Usage:
    python3 scripts/dev/build_public_site.py [--out build/public-site] [--cname unitares.cirwel.org]

Dependencies: the pure-Python ``markdown`` package (free; no model API).
Deployed to GitHub Pages by .github/workflows/public-pages.yml.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import markdown
except ImportError:  # pragma: no cover - surfaced clearly in CI
    print("error: the 'markdown' package is required (pip install markdown).", file=sys.stderr)
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glossary_data import parse_glossary  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONTOLOGY = PROJECT_ROOT / "docs" / "ontology"
SITE = PROJECT_ROOT / "docs" / "public-site"
FAVICON = SITE / "favicon.svg"
REPO_URL = "https://github.com/cirwel/unitares"
LANDING_BLOB = f"{REPO_URL}/blob/master/docs/public-site/index.md"
GLOSSARY_BLOB = f"{REPO_URL}/blob/master/docs/ontology/glossary.md"
DEMO_BLOB = f"{REPO_URL}/blob/master/docs/public-site/demo.md"
# The demo page tells one incident. Its quoted facts, record ids and link names
# come from the fixture the accountability-journey test runs against, the build
# fails on any it cannot fill, and public-pages.yml redeploys when the fixture
# changes. The narration between them is hand-written.
JOURNEY_FIXTURE = PROJECT_ROOT / "tests" / "accountability_journey" / "incident-v0.json"
DEMO_LINKS = {
    "fixture_blob": f"{REPO_URL}/blob/master/tests/accountability_journey/incident-v0.json",
    "result_blob": f"{REPO_URL}/blob/master/docs/evaluations/accountability-journey/result-v0.md",
    "evidence_blob": f"{REPO_URL}/blob/master/docs/EVIDENCE_AND_LIMITS.md",
    "walkthrough_blob": f"{REPO_URL}/blob/master/docs/guides/AGENT_SESSION_WALKTHROUGH.md",
}

# Public-facing framing prepended to the glossary page so a cold visitor is not
# dropped into internal jargon. Kept short; the discipline speaks for itself.
INTRO_MD = """\
!!! note "What this is"
    A living glossary for [UNITARES]({repo}) — accountability infrastructure
    for long-running AI agents. Every term is defined by **the question it answers**,
    not by a list of examples, because a term pinned to its discriminating
    question survives redefinition while one pinned to examples rots. The page
    shows its own drift corrections on purpose: a system that hands you its own
    falsification harness should also show where its vocabulary was wrong and got
    fixed. Source of truth is [`docs/ontology/glossary.md`]({blob}); this page is
    generated from it.

""".format(repo=REPO_URL, blob=GLOSSARY_BLOB)

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#F5F1E8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#15110D" media="(prefers-color-scheme: dark)">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<title>{title}</title>
<style>
  /* The CIRWEL house register, as cirwel.org sets it: cream and oxblood by
     day, bistre and verdigris by night, hairlines instead of cards. Tokens are
     copied from cirwel-site's tailwind.config.mjs; system serifs stand in for
     Bodoni Moda and EB Garamond because this page ships no webfonts. */
  :root{{--bg:245 241 232;--line:201 192 174;--ink:26 22 18;--dim:92 84 74;--acc:122 31 31;color-scheme:light}}
  @media (prefers-color-scheme: dark){{:root{{--bg:21 17 13;--line:85 74 60;--ink:219 210 191;--dim:168 159 144;--acc:127 175 162;color-scheme:dark}}}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:rgb(var(--bg));color:rgb(var(--ink));font:17px/1.6 Georgia,"Times New Roman",serif}}
  header{{position:sticky;top:0;z-index:5;background:rgb(var(--bg) / .94);backdrop-filter:blur(4px);border-bottom:1px solid rgb(var(--line));padding:14px 22px;display:flex;gap:22px;align-items:baseline;flex-wrap:wrap}}
  header .brand,header a.brand{{font-family:Didot,"Bodoni MT",Georgia,serif;font-weight:700;font-variant-caps:all-small-caps;letter-spacing:.08em;font-size:19px;color:rgb(var(--ink));text-decoration:none}}
  header nav a{{color:rgb(var(--dim));text-decoration:none;margin-right:16px;font:500 12.5px/1 ui-monospace,"JetBrains Mono",Menlo,monospace;letter-spacing:.14em;text-transform:uppercase}}
  header nav a.active,header nav a:hover{{color:rgb(var(--acc))}}
  main{{max-width:880px;margin:0 auto;padding:36px 22px 90px}}
  h1,h2,h3{{font-family:Didot,"Bodoni MT",Georgia,serif;font-weight:600;line-height:1.15;letter-spacing:-.01em}}
  h1{{font-size:38px;margin:0 0 14px}} h2{{font-size:26px;margin-top:44px;border-bottom:1px solid rgb(var(--line));padding-bottom:8px;scroll-margin-top:80px}} h3{{font-size:20px;margin-top:28px}}
  a{{color:inherit;text-decoration:underline;text-decoration-color:rgb(var(--acc) / .4);text-underline-offset:.2em}} a:hover{{text-decoration-color:rgb(var(--acc))}}
  a:focus-visible{{outline:2px solid rgb(var(--acc));outline-offset:3px}}
  code{{font-family:ui-monospace,"JetBrains Mono",Menlo,monospace;font-size:.86em;color:rgb(var(--ink))}}
  pre{{border-top:1px solid rgb(var(--line));border-bottom:1px solid rgb(var(--line));padding:14px 2px;overflow:auto;font-size:14px;line-height:1.7}} pre code{{border:0;padding:0;background:none}}
  table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:15px;display:block;overflow-x:auto}}
  th,td{{border-top:1px solid rgb(var(--line));padding:9px 12px 9px 0;text-align:left;vertical-align:top}}
  tr:last-child td{{border-bottom:1px solid rgb(var(--line))}}
  th{{color:rgb(var(--dim));font:500 12px/1.4 ui-monospace,"JetBrains Mono",Menlo,monospace;text-transform:uppercase;letter-spacing:.14em;border-top:0}}
  blockquote{{border-left:2px solid rgb(var(--acc));margin:16px 0;padding:6px 18px;color:rgb(var(--dim))}}
  main > p:first-of-type{{font-family:Didot,"Bodoni MT",Georgia,serif;font-style:italic;font-size:22px;color:rgb(var(--dim));margin-top:0}}
  .hero-actions{{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0 8px}}
  .hero-actions a{{display:inline-flex;align-items:center;min-height:40px;padding:8px 16px;border:1px solid rgb(var(--acc));border-radius:0;font:500 12.5px/1 ui-monospace,"JetBrains Mono",Menlo,monospace;letter-spacing:.14em;text-transform:uppercase;text-decoration:none}}
  .hero-actions .primary{{background:rgb(var(--acc));color:rgb(var(--bg))}}
  .hero-actions .secondary{{color:rgb(var(--acc))}}
  .hero-actions a:hover{{filter:brightness(1.08)}}
  .admonition{{border-top:1px solid rgb(var(--line));border-bottom:1px solid rgb(var(--line));border-left:2px solid rgb(var(--acc));padding:12px 16px;margin:18px 0}}
  .admonition-title{{font:500 12px/1.4 ui-monospace,"JetBrains Mono",Menlo,monospace;text-transform:uppercase;letter-spacing:.14em;color:rgb(var(--acc));margin:0 0 6px}}
  hr{{border:0;border-top:1px solid rgb(var(--line));margin:28px 0}}
  footer{{max-width:880px;margin:0 auto;padding:18px 22px 50px;color:rgb(var(--dim));font:400 12.5px/1.6 ui-monospace,"JetBrains Mono",Menlo,monospace;letter-spacing:.04em;border-top:1px solid rgb(var(--line))}}
  @media (max-width:560px){{.hero-actions a{{width:100%;justify-content:center}} h1{{font-size:30px}}}}
</style>
</head>
<body>
<header>
  <a class="brand" href="https://cirwel.org">UNITARES</a>
  <nav>
    <a href="index.html"{home_active}>Artifacts</a>
    <a href="demo.html"{demo_active}>Demo</a>
    <a href="glossary.html"{glossary_active}>Glossary</a>
    <a href="glossary-viewer.html">Viewer</a>
    <a href="drift-audit.html"{audit_active}>Drift audit</a>
    <a href="https://cirwel.org">cirwel.org ↗</a>
    <a href="{repo}">Repo ↗</a>
  </nav>
</header>
<main>
{body}
</main>
<footer>
  Generated from <code>{source_path}</code> — repository markdown is the source of truth.
  Edit there, not here. <a href="{blob}">View source</a>.
</footer>
</body>
</html>
"""


_REL_MD_LINK_RE = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s#]+\.md)(#[^)\s]*)?\)")
_FENCE_LINE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")


def rewrite_repo_relative_links(md_text: str, source_dir: Path) -> str:
    """Rewrite relative .md links outside code fences to GitHub blob URLs."""
    root = PROJECT_ROOT.resolve()

    def _replace(match: re.Match[str]) -> str:
        rel_path, fragment = match.group(1), match.group(2) or ""
        target = (source_dir / rel_path).resolve()
        try:
            repo_rel = target.relative_to(root).as_posix()
        except ValueError:
            return match.group(0)
        return f"]({REPO_URL}/blob/master/{repo_rel}{fragment})"

    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in md_text.splitlines(keepends=True):
        fence_match = _FENCE_LINE_RE.match(line)
        if fence_match:
            marker = fence_match.group(2)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            out_lines.append(line)
            continue
        out_lines.append(line if in_fence else _REL_MD_LINK_RE.sub(_replace, line))
    return "".join(out_lines)


def render(md_text: str, source_dir: Path | None = None) -> str:
    if source_dir is not None:
        md_text = rewrite_repo_relative_links(md_text, source_dir)
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "attr_list", "sane_lists", "admonition"],
        output_format="html5",
    )
    return md.convert(md_text)


def render_demo_md(demo_md: str, fixture: dict) -> str:
    """Fill demo.md's placeholders from the journey fixture.

    ``{{facts.<key>}}`` takes the oracle fact; ``{{relation.<name>}}`` takes a
    link name from the oracle's edges; ``{{questions_table}}`` lists each
    question with the answer and the UNITARES-arm record that holds it. Any
    placeholder left unfilled, a question no record answers, or an edge relation
    the page never names fails the build rather than publishing a gap.
    """
    facts = fixture["oracle"]["facts"]
    text = demo_md
    for key, value in facts.items():
        text = text.replace("{{facts.%s}}" % key, str(value))

    relations = {edge["relation"] for edge in fixture["oracle"].get("edges", [])}
    unnamed = sorted(r for r in relations if "{{relation.%s}}" % r not in text)
    if unnamed:
        raise ValueError(f"demo: docs/public-site/demo.md never names the fixture's {unnamed} links")
    for relation in relations:
        text = text.replace("{{relation.%s}}" % relation, relation)

    # The answer shown is the record's own value; one that disagrees with the
    # oracle fails the build rather than printing the oracle beside it.
    holders: dict[str, dict] = {}
    for record in fixture["arms"]["unitares"]["records"]:
        for key in record.get("facts", {}):
            holders.setdefault(key, record)
    rows = ["| Question | Answer from the record | Record |", "|---|---|---|"]
    for question in fixture["questions"]:
        key = question["fact_key"]
        if key not in holders:
            raise ValueError(f"demo: no record answers {question['question_id']!r}")
        record = holders[key]
        answer = record["facts"][key]
        if str(answer) != str(facts[key]):
            raise ValueError(
                f"demo: record {record['record_id']!r} answers {question['question_id']!r} "
                "differently from the oracle"
            )
        label = question["question_id"].replace("_", " ").capitalize() + "?"
        rows.append(f"| {label} | {answer} | `{record['record_id']}` |")
    text = text.replace("{{questions_table}}", "\n".join(rows))

    for name, url in DEMO_LINKS.items():
        text = text.replace("{%s}" % name, url)
    # Any {name} left over is a placeholder nobody filled, a typo included.
    if "{{" in text or re.search(r"\{[A-Za-z_][\w.]*\}", text):
        raise ValueError("demo: unfilled placeholder in docs/public-site/demo.md")
    return text


_DEMO_STEP_PROGRESSION: tuple[dict, ...] = (
    {
        "number": 1,
        "short_label": "Claim",
        "relations": ("authored",),
        "visible_records": ("identity-writer", "finding-original"),
        "show_reconstruction": False,
    },
    {
        "number": 2,
        "short_label": "Evidence",
        "relations": ("contradicts", "grades"),
        "visible_records": ("identity-writer", "finding-original", "outcome-evidence"),
        "show_reconstruction": False,
    },
    {
        "number": 3,
        "short_label": "Challenge",
        "relations": ("challenged",),
        "visible_records": ("identity-writer", "finding-original", "outcome-evidence", "review-challenge"),
        "show_reconstruction": False,
    },
    {
        "number": 4,
        "short_label": "Supersede",
        "relations": ("supersedes",),
        "visible_records": (
            "identity-writer",
            "finding-original",
            "outcome-evidence",
            "review-challenge",
            "finding-correction",
        ),
        "show_reconstruction": False,
    },
    {
        "number": 5,
        "short_label": "Reconstruct",
        "relations": ("authored", "contradicts", "grades", "challenged", "supersedes"),
        "visible_records": (
            "identity-writer",
            "finding-original",
            "outcome-evidence",
            "review-challenge",
            "finding-correction",
        ),
        "show_reconstruction": True,
    },
)

_NUMBERED_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
_UNSUPPORTED_BLOCK_MD_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+|#{1,6}\s+)")
_UNSUPPORTED_INLINE_MD_RE = re.compile(
    r"\[[^\]]+\]\([^)]+\)|\*\*|__|<[A-Za-z/!][^>]*>|(?<!\w)[*_](?=\S).+?(?<=\S)[*_](?!\w)"
)


def _strip_inline_code(text: str) -> str:
    return re.sub(r"`([^`]+)`", r"\1", text)


def _reject_unsupported_markdown(num: int, text: str) -> None:
    stripped = re.sub(r"`[^`]+`", "", text)
    if _UNSUPPORTED_INLINE_MD_RE.search(stripped):
        raise ValueError(
            f"demo-interactive: section {num} contains markdown syntax "
            f"unsupported by textContent: {text!r}"
        )


def _parse_demo_sections(demo_md: str) -> dict[int, tuple[str, str, str, str]]:
    """Extract numbered step sections (## 1. .. ## 5.) from filled demo.md.

    Returns ``{number: (title, lead_in, quote, detail)}`` where ``lead_in`` is
    the prose before any blockquote, ``quote`` is the verbatim blockquote text
    (empty when a section has none, such as step 5), and ``detail`` is the prose
    after the blockquote. Fails closed on duplicate section numbers or markdown
    constructs (links, emphasis, lists, sub-headings, inline HTML) that
    ``textContent`` would render literally.
    """
    matches = list(_NUMBERED_SECTION_RE.finditer(demo_md))
    sections: dict[int, tuple[str, str, str, str]] = {}
    for idx, match in enumerate(matches):
        num = int(match.group(1))
        if num in sections:
            raise ValueError(f"demo-interactive: duplicate section '## {num}.' in demo.md")
        raw_title = match.group(2).strip()
        _reject_unsupported_markdown(num, raw_title)
        title = _strip_inline_code(raw_title)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(demo_md)
        body = demo_md[start:end]
        # Stop at the next unnumbered heading (e.g. "## Working with Git...").
        next_h2 = re.search(r"^##\s+", body, re.MULTILINE)
        if next_h2 is not None:
            body = body[: next_h2.start()]
        lead_lines: list[str] = []
        quote_lines: list[str] = []
        detail_lines: list[str] = []
        seen_quote = False
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.startswith("|"):
                continue
            if line.startswith(">"):
                seen_quote = True
                quote_lines.append(line.lstrip(">").strip())
            else:
                if _UNSUPPORTED_BLOCK_MD_RE.match(line):
                    raise ValueError(
                        f"demo-interactive: section {num} contains markdown syntax "
                        f"unsupported by textContent: {line!r}"
                    )
                if seen_quote:
                    detail_lines.append(line)
                else:
                    lead_lines.append(line)
        lead_joined = " ".join(lead_lines).strip()
        detail_joined = " ".join(detail_lines).strip()
        _reject_unsupported_markdown(num, lead_joined)
        _reject_unsupported_markdown(num, detail_joined)
        lead_in = _strip_inline_code(lead_joined)
        quote = " ".join(quote_lines).strip()
        detail = _strip_inline_code(detail_joined)
        sections[num] = (title, lead_in, quote, detail)
    return sections


def build_demo_interactive_payload(demo_md: str, fixture: dict) -> dict:
    """Build and fail-closed-validate the interactive demo widget payload.

    Shows the UNITARES arm narrated by ``demo.md``: step titles, lead-ins,
    blockquotes, and follow-up prose are parsed directly from the filled
    ``demo.md``; compact step-strip labels and record/relation progressions are
    declared in ``_DEMO_STEP_PROGRESSION`` and validated against ``fixture`` so
    duplicate record ids, renames, or uncovered records/relations fail the build.
    """
    sections = _parse_demo_sections(demo_md)
    facts = fixture["oracle"]["facts"]
    oracle_edges = fixture["oracle"].get("edges", [])
    oracle_relations = {e["relation"] for e in oracle_edges}
    arms = fixture.get("arms", {})

    if "unitares" not in arms:
        raise ValueError("demo-interactive: missing 'unitares' arm in fixture")
    records = arms["unitares"].get("records", [])
    raw_ids = [r["record_id"] for r in records]
    record_ids = set(raw_ids)
    if len(record_ids) != len(raw_ids):
        raise ValueError("demo-interactive: duplicate record_id in 'unitares' arm")

    holders: dict[str, dict] = {}
    for rec in records:
        for key in rec.get("facts", {}):
            holders.setdefault(key, rec)
    for question in fixture.get("questions", []):
        key = question["fact_key"]
        if key not in holders:
            raise ValueError(
                f"demo-interactive: no UNITARES record answers {question['question_id']!r}"
            )
        if str(holders[key]["facts"][key]) != str(facts[key]):
            raise ValueError(
                f"demo-interactive: UNITARES record disagrees with oracle on {key!r}"
            )

    expected_nums = {spec["number"] for spec in _DEMO_STEP_PROGRESSION}
    extra_sections = sorted(set(sections) - expected_nums)
    if extra_sections:
        raise ValueError(
            f"demo-interactive: docs/public-site/demo.md has unmapped numbered section(s) {extra_sections}"
        )

    records_by_id = {r["record_id"]: r for r in records}
    covered_relations: set[str] = set()
    covered_records: set[str] = set()
    steps: list[dict] = []
    for spec in _DEMO_STEP_PROGRESSION:
        num = spec["number"]
        if num not in sections:
            raise ValueError(f"demo-interactive: docs/public-site/demo.md is missing section '## {num}.'")
        title, lead_in, quote, detail = sections[num]
        if not title or not lead_in:
            raise ValueError(f"demo-interactive: section {num} in demo.md has empty title or lead-in")
        if not spec["show_reconstruction"] and not quote:
            raise ValueError(f"demo-interactive: section {num} in demo.md is missing a blockquote")

        step_ids = list(spec["visible_records"])
        unknown_ids = [rid for rid in step_ids if rid not in record_ids]
        if unknown_ids:
            raise ValueError(
                f"demo-interactive: step {num} references unknown record(s) {unknown_ids}"
            )
        covered_records.update(step_ids)

        visible_record_relations = {
            e["relation"]
            for rid in step_ids
            for e in records_by_id[rid].get("edges", [])
        }
        for rel in spec["relations"]:
            if rel not in oracle_relations:
                raise ValueError(f"demo-interactive: step {num} references unknown relation {rel!r}")
            if rel not in visible_record_relations:
                raise ValueError(
                    f"demo-interactive: step {num} relation {rel!r} is missing from visible records' edges"
                )
            covered_relations.add(rel)

        steps.append(
            {
                "number": num,
                "short_label": spec["short_label"],
                "title": title,
                "lead_in": lead_in,
                "quote": quote,
                "detail": detail,
                "relations": list(spec["relations"]),
                "visible_records": step_ids,
                "show_reconstruction": spec["show_reconstruction"],
            }
        )

    missing_records = sorted(record_ids - covered_records)
    if missing_records:
        raise ValueError(
            f"demo-interactive: fixture record(s) {missing_records} are never shown in any step"
        )

    uncovered = sorted(oracle_relations - covered_relations)
    if uncovered:
        raise ValueError(f"demo-interactive: fixture relation(s) {uncovered} are never shown in any step")

    return {
        "incident_id": fixture["incident"]["incident_id"],
        "records": records,
        "oracle_edges": oracle_edges,
        "questions": fixture["questions"],
        "steps": steps,
    }


def inject_demo_interactive(demo_body: str, widget_html: str) -> str:
    """Insert ``widget_html`` right before the first ``<h2>`` in ``demo_body``.

    Fails closed if no ``<h2>`` heading is present rather than silently
    prepending the widget above ``<h1>``.
    """
    split_marker = "\n<h2"
    if split_marker not in demo_body:
        raise ValueError("demo-interactive: rendered demo.html has no <h2> section heading")
    intro_part, rest_part = demo_body.split(split_marker, 1)
    return f"{intro_part}\n{widget_html}\n<h2{rest_part}"


def render_demo_interactive(template: str, payload: dict) -> str:
    """Embed ``payload`` into the interactive demo template for an inline script.

    Escapes ``<``, ``>``, ``&``, and Unicode line separators so no payload string
    can terminate ``<script>`` or enter HTML script-data-double-escaped state
    (e.g. ``<!--<script``), and fails the build if the placeholder is missing.
    """
    placeholder = "__DEMO_DATA_JSON__"
    if placeholder not in template:
        raise ValueError("demo: docs/public-site/demo-interactive.template.html is missing __DEMO_DATA_JSON__")
    demo_data_json = (
        json.dumps(payload, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return template.replace(placeholder, demo_data_json)


def latest_drift_audit() -> Path | None:
    audits = sorted(ONTOLOGY.glob("glossary-drift-audit-*.md"))
    return audits[-1] if audits else None


def build(out_dir: Path, cname: str | None) -> None:
    landing_md = (SITE / "index.md").read_text(encoding="utf-8")
    glossary_md = (ONTOLOGY / "glossary.md").read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FAVICON, out_dir / "favicon.svg")

    # Repository artifacts index page. Its copy remains reviewable as markdown.
    out_dir.joinpath("index.html").write_text(
        PAGE_TEMPLATE.format(
            title="UNITARES — Repository artifacts",
            home_active=' class="active"',
            demo_active="",
            glossary_active="",
            audit_active="",
            repo=REPO_URL,
            source_path="docs/public-site/index.md",
            blob=LANDING_BLOB,
            body=render(landing_md, source_dir=SITE),
        ),
        encoding="utf-8",
    )

    # Read-only demo: one incident from the tested fixture, nothing to install.
    journey_fixture = json.loads(JOURNEY_FIXTURE.read_text(encoding="utf-8"))
    demo_md = render_demo_md(
        (SITE / "demo.md").read_text(encoding="utf-8"),
        journey_fixture,
    )
    demo_body = render(demo_md, source_dir=SITE)
    demo_interactive = render_demo_interactive(
        (SITE / "demo-interactive.template.html").read_text(encoding="utf-8"),
        build_demo_interactive_payload(demo_md, journey_fixture),
    )
    demo_html_body = inject_demo_interactive(demo_body, demo_interactive)
    out_dir.joinpath("demo.html").write_text(
        PAGE_TEMPLATE.format(
            title="UNITARES demo: one incident, as the record holds it",
            home_active="",
            demo_active=' class="active"',
            glossary_active="",
            audit_active="",
            repo=REPO_URL,
            source_path="docs/public-site/demo.md",
            blob=DEMO_BLOB,
            body=demo_html_body,
        ),
        encoding="utf-8",
    )

    # Glossary page (with the public intro prepended).
    out_dir.joinpath("glossary.html").write_text(
        PAGE_TEMPLATE.format(
            title="UNITARES Glossary",
            home_active="",
            demo_active="",
            glossary_active=' class="active"',
            audit_active="",
            repo=REPO_URL,
            source_path="docs/ontology/glossary.md",
            blob=GLOSSARY_BLOB,
            body=render(INTRO_MD + glossary_md, source_dir=ONTOLOGY),
        ),
        encoding="utf-8",
    )

    # Interactive glossary viewer — generated from a structured parse of the
    # same glossary.md, so the viewer holds no hand-maintained data copy (the
    # retired docs/ontology/glossary-viewer.html prototype did, and drifted).
    viewer_template = (SITE / "glossary-viewer.template.html").read_text(encoding="utf-8")
    # "</" escaped so no glossary text can terminate the inline <script> block.
    data_json = json.dumps(parse_glossary(), ensure_ascii=False).replace("</", "<\\/")
    out_dir.joinpath("glossary-viewer.html").write_text(
        viewer_template.replace("__GLOSSARY_DATA_JSON__", data_json).replace(
            "__REPO_URL__", REPO_URL
        ),
        encoding="utf-8",
    )

    # Drift-audit page (latest dated sweep).
    audit = latest_drift_audit()
    if audit is not None:
        out_dir.joinpath("drift-audit.html").write_text(
            PAGE_TEMPLATE.format(
                title="UNITARES Glossary — Drift Audit",
                home_active="",
                demo_active="",
                glossary_active="",
                audit_active=' class="active"',
                repo=REPO_URL,
                source_path=f"docs/ontology/{audit.name}",
                blob=f"{REPO_URL}/blob/master/docs/ontology/{audit.name}",
                body=render(audit.read_text(encoding="utf-8"), source_dir=ONTOLOGY),
            ),
            encoding="utf-8",
        )

    # Optional custom domain for GitHub Pages.
    if cname:
        out_dir.joinpath("CNAME").write_text(cname.strip() + "\n", encoding="utf-8")

    print(f"Built public site -> {out_dir} ({'with' if cname else 'no'} CNAME)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="build/public-site", help="Output directory")
    p.add_argument(
        "--cname",
        default=None,
        help="Custom domain to serve from (writes a CNAME file, e.g. unitares.cirwel.org)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    build(Path(args.out), args.cname)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
