# Public Site (GitHub Pages)

The public UNITARES landing page, ontology glossary, interactive glossary
viewer, and latest glossary drift audit are published to GitHub Pages. Each
page is **generated from repository markdown**; the built HTML is never
hand-edited.

## How it builds

- `scripts/dev/build_public_site.py` renders `docs/public-site/index.md`,
  `docs/ontology/glossary.md`, and the latest `glossary-drift-audit-*.md` into a
  small static site (`build/public-site/`) with a shared theme. Only the
  `markdown` pip package is needed — no model API, no paid service.
- The **interactive viewer** (`glossary-viewer.html` — search + homonym /
  single-sense / Rosetta / open-gap filters) is generated from a structured
  parse of the same `glossary.md` (`scripts/dev/glossary_data.py` injected into
  `docs/public-site/glossary-viewer.template.html`), so it holds no data copy
  of its own. The old hand-maintained viewer prototype (a standalone
  `glossary-viewer.html` under `docs/ontology/`) carried its own `TERMS` array
  and drifted; it is retired, and
  `scripts/dev/check_glossary_drift.py` (run in CI by `tests.yml`) fails if it
  comes back, if `glossary.md` stops parsing, or if the glossary's
  `path.py::symbol` code references or its cross-references with the runtime
  glossary (`src/governance_glossary.py`) go stale.
- `.github/workflows/public-pages.yml` runs the build and deploys to Pages on
  every push to `master` that touches the landing page, glossary, drift audit,
  build script, parser, viewer template, or workflow. Also runnable via
  **Actions → public-pages → Run workflow**.

Build locally to preview:

```bash
pip install markdown
python3 scripts/dev/build_public_site.py --out build/public-site
# open build/public-site/index.html (landing page)
# open build/public-site/glossary.html (canonical glossary)
# open build/public-site/glossary-viewer.html (interactive viewer)
```

## How publishing works (Actions source)

The workflow builds the site and deploys it to GitHub Pages via
`actions/upload-pages-artifact` + `actions/deploy-pages` (Pages **Source =
"GitHub Actions"**). On every relevant push to `master`, the site re-publishes
to **https://cirwel.github.io/unitares/**.

### Enablement history (why it took a few tries)

The workflow `GITHUB_TOKEN` cannot *create* a Pages site — only deploy to one that
exists. So enabling Pages was a one-time human step:

- #985 (Actions deploy) and #986 (`configure-pages enablement: true`) both failed
  with `Resource not accessible by integration` / a deploy 404, because Pages was
  not yet enabled and the token may not enable it.
- #987 published to a `gh-pages` branch as a token-only fallback (that branch now
  exists but is unused under Actions source — safe to delete).
- Once Pages was enabled by hand (**Settings → Pages → Source = "GitHub
  Actions"**), this Actions-deploy path works and is the maintained one.

If you ever see the deploy job 404 again, confirm **Settings → Pages → Source**
is still "GitHub Actions".

## Custom domain (optional, branded URL)

Recommended alias: **`unitares.cirwel.org`**. To switch:

1. In `.github/workflows/public-pages.yml`, change the build step to:
   `python3 scripts/dev/build_public_site.py --out build/public-site --cname unitares.cirwel.org`
   (the script writes the `CNAME` file Pages needs).
2. At your DNS provider, add a **CNAME** record:
   `unitares.cirwel.org  →  cirwel.github.io`.
3. Repo **Settings → Pages → Custom domain** = `unitares.cirwel.org`, then enable
   **Enforce HTTPS** once the cert provisions.

That's the whole switch — one flag + one DNS record. The site content and source
of truth are unchanged; only the served hostname moves.

## What it publishes (and what it doesn't)

Deliberately scoped to the landing page, glossary, and drift audit rather than
the whole `docs/` tree. Internal proposals and runbooks are not published as
polished pages by this workflow; evaluator links point to their canonical files
in the public repository.
