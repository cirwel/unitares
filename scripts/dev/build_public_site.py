#!/usr/bin/env python3
"""Build the public UNITARES site from canonical repository markdown.

Single-source by construction: this renders ``docs/public-site/index.md``,
``docs/ontology/glossary.md``, and the latest ``glossary-drift-audit-*.md``
directly to HTML, and generates the interactive glossary viewer from a
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
  header .brand{{font-family:Didot,"Bodoni MT",Georgia,serif;font-weight:700;font-variant-caps:all-small-caps;letter-spacing:.08em;font-size:19px}}
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
  <span class="brand">UNITARES</span>
  <nav>
    <a href="index.html"{home_active}>Home</a>
    <a href="glossary.html"{glossary_active}>Glossary</a>
    <a href="glossary-viewer.html">Viewer</a>
    <a href="drift-audit.html"{audit_active}>Drift audit</a>
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


def render(md_text: str) -> str:
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "attr_list", "sane_lists", "admonition"],
        output_format="html5",
    )
    return md.convert(md_text)


def latest_drift_audit() -> Path | None:
    audits = sorted(ONTOLOGY.glob("glossary-drift-audit-*.md"))
    return audits[-1] if audits else None


def build(out_dir: Path, cname: str | None) -> None:
    landing_md = (SITE / "index.md").read_text(encoding="utf-8")
    glossary_md = (ONTOLOGY / "glossary.md").read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FAVICON, out_dir / "favicon.svg")

    # Product/evaluator landing page. Its copy remains reviewable as markdown.
    out_dir.joinpath("index.html").write_text(
        PAGE_TEMPLATE.format(
            title="UNITARES — Accountability infrastructure for long-running AI agents",
            home_active=' class="active"',
            glossary_active="",
            audit_active="",
            repo=REPO_URL,
            source_path="docs/public-site/index.md",
            blob=LANDING_BLOB,
            body=render(landing_md),
        ),
        encoding="utf-8",
    )

    # Glossary page (with the public intro prepended).
    out_dir.joinpath("glossary.html").write_text(
        PAGE_TEMPLATE.format(
            title="UNITARES Glossary",
            home_active="",
            glossary_active=' class="active"',
            audit_active="",
            repo=REPO_URL,
            source_path="docs/ontology/glossary.md",
            blob=GLOSSARY_BLOB,
            body=render(INTRO_MD + glossary_md),
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
                glossary_active="",
                audit_active=' class="active"',
                repo=REPO_URL,
                source_path=f"docs/ontology/{audit.name}",
                blob=f"{REPO_URL}/blob/master/docs/ontology/{audit.name}",
                body=render(audit.read_text(encoding="utf-8")),
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
