#!/usr/bin/env python3
"""Build the public glossary site from the canonical ontology markdown.

Single-source by construction: this renders ``docs/ontology/glossary.md`` and the
latest ``glossary-drift-audit-*.md`` directly to HTML. It never holds its own copy
of the content, so the published site cannot drift from the markdown the way a
hand-maintained app would.

Usage:
    python3 scripts/dev/build_glossary_site.py [--out build/glossary-site] [--cname glossary.cirwel.org]

Dependencies: the pure-Python ``markdown`` package (free; no model API).
Deployed to GitHub Pages by .github/workflows/glossary-pages.yml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import markdown
except ImportError:  # pragma: no cover - surfaced clearly in CI
    print("error: the 'markdown' package is required (pip install markdown).", file=sys.stderr)
    raise SystemExit(1)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONTOLOGY = PROJECT_ROOT / "docs" / "ontology"
REPO_URL = "https://github.com/cirwel/unitares"
GLOSSARY_BLOB = f"{REPO_URL}/blob/master/docs/ontology/glossary.md"

# Public-facing framing prepended to the glossary page so a cold visitor is not
# dropped into internal jargon. Kept short; the discipline speaks for itself.
INTRO_MD = """\
!!! note "What this is"
    A living glossary for [UNITARES]({repo}) — runtime state telemetry for
    long-lived AI agents. Every term is defined by **the question it answers**,
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
<title>{title}</title>
<style>
  :root{{--bg:#0e1116;--panel:#161b22;--line:#2a313c;--ink:#e6edf3;--dim:#8b949e;--acc:#58a6ff}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
  header{{position:sticky;top:0;z-index:5;background:#0e1116ee;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:14px 22px;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}}
  header .brand{{font-weight:600}}
  header nav a{{color:var(--dim);text-decoration:none;margin-right:14px;font-size:14px}}
  header nav a.active,header nav a:hover{{color:var(--ink)}}
  main{{max-width:880px;margin:0 auto;padding:30px 22px 90px}}
  h1,h2,h3{{line-height:1.25}} h1{{font-size:26px;margin-top:0}} h2{{margin-top:34px;border-bottom:1px solid var(--line);padding-bottom:6px}} h3{{margin-top:26px}}
  a{{color:var(--acc)}}
  code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;background:#1c232c;border:1px solid var(--line);border-radius:5px;padding:.08em .4em}}
  pre{{background:#1c232c;border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow:auto}} pre code{{border:0;padding:0;background:none}}
  table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14.5px;display:block;overflow-x:auto}}
  th,td{{border:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}}
  th{{background:#1c232c;color:var(--dim);font-size:12.5px;text-transform:uppercase;letter-spacing:.4px}}
  blockquote{{border-left:3px solid var(--acc);background:#1c232c;margin:16px 0;padding:10px 16px;border-radius:0 8px 8px 0;color:var(--dim)}}
  .admonition{{border:1px solid var(--line);border-left:3px solid var(--acc);background:#161b22;border-radius:0 8px 8px 0;padding:12px 16px;margin:18px 0}}
  .admonition-title{{font-weight:600;color:var(--ink);margin:0 0 6px}}
  hr{{border:0;border-top:1px solid var(--line);margin:28px 0}}
  footer{{max-width:880px;margin:0 auto;padding:0 22px 50px;color:var(--dim);font-size:12.5px;border-top:1px solid var(--line)}}
</style>
</head>
<body>
<header>
  <span class="brand">UNITARES · glossary</span>
  <nav>
    <a href="index.html"{glossary_active}>Glossary</a>
    <a href="drift-audit.html"{audit_active}>Drift audit</a>
    <a href="{repo}">Repo ↗</a>
  </nav>
</header>
<main>
{body}
</main>
<footer>
  Generated from <code>docs/ontology/</code> — the markdown is the source of truth.
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
    glossary_md = (ONTOLOGY / "glossary.md").read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Glossary page (with the public intro prepended).
    out_dir.joinpath("index.html").write_text(
        PAGE_TEMPLATE.format(
            title="UNITARES Glossary",
            glossary_active=' class="active"',
            audit_active="",
            repo=REPO_URL,
            blob=GLOSSARY_BLOB,
            body=render(INTRO_MD + glossary_md),
        ),
        encoding="utf-8",
    )

    # Drift-audit page (latest dated sweep).
    audit = latest_drift_audit()
    if audit is not None:
        out_dir.joinpath("drift-audit.html").write_text(
            PAGE_TEMPLATE.format(
                title="UNITARES Glossary — Drift Audit",
                glossary_active="",
                audit_active=' class="active"',
                repo=REPO_URL,
                blob=f"{REPO_URL}/blob/master/docs/ontology/{audit.name}",
                body=render(audit.read_text(encoding="utf-8")),
            ),
            encoding="utf-8",
        )

    # Optional custom domain for GitHub Pages.
    if cname:
        out_dir.joinpath("CNAME").write_text(cname.strip() + "\n", encoding="utf-8")

    print(f"Built glossary site -> {out_dir} ({'with' if cname else 'no'} CNAME)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="build/glossary-site", help="Output directory")
    p.add_argument(
        "--cname",
        default=None,
        help="Custom domain to serve from (writes a CNAME file, e.g. glossary.cirwel.org)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    build(Path(args.out), args.cname)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
