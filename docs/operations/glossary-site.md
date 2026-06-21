# Glossary Site (GitHub Pages)

The public glossary at `docs/ontology/glossary.md` is published to GitHub Pages,
**generated from the markdown** — the markdown stays the single source of truth;
the site is never hand-edited.

## How it builds

- `scripts/dev/build_glossary_site.py` renders `docs/ontology/glossary.md` and the
  latest `glossary-drift-audit-*.md` into a small static site (`build/glossary-site/`),
  wrapping them in a shared theme with a public-facing intro. Only the `markdown`
  pip package is needed — no model API, no paid service.
- `.github/workflows/glossary-pages.yml` runs that script and deploys to Pages on
  every push to `master` that touches the glossary, the drift audit, the build
  script, or the workflow. Also runnable via **Actions → glossary-pages → Run
  workflow**.

Build locally to preview:

```bash
pip install markdown
python3 scripts/dev/build_glossary_site.py --out build/glossary-site
# open build/glossary-site/index.html
```

## Enablement

The workflow **auto-enables Pages on its first run** via
`actions/configure-pages@v5` with `enablement: true` (it holds `pages: write`), so
no manual toggle is normally needed — push to `master` (or run the workflow
manually) and the site publishes at **https://cirwel.github.io/unitares/**.

Fallback (if an org/account policy blocks API enablement and the deploy job 404s
with "Ensure GitHub Pages has been enabled"): set it by hand once —
repo **Settings → Pages → Build and deployment → Source = "GitHub Actions"** — then
re-run the workflow.

## Custom domain (optional, branded URL)

Recommended alias: **`glossary.cirwel.org`** (shorter than `cirwelsystems.com`,
and matches the GitHub owner `cirwel`). To switch:

1. In `.github/workflows/glossary-pages.yml`, change the build step to:
   `python3 scripts/dev/build_glossary_site.py --out build/glossary-site --cname glossary.cirwel.org`
   (the script writes the `CNAME` file Pages needs).
2. At your DNS provider, add a **CNAME** record:
   `glossary.cirwel.org  →  cirwel.github.io`.
3. Repo **Settings → Pages → Custom domain** = `glossary.cirwel.org`, then enable
   **Enforce HTTPS** once the cert provisions.

That's the whole switch — one flag + one DNS record. The site content and source
of truth are unchanged; only the served hostname moves.

## What it publishes (and what it doesn't)

Deliberately scoped to the glossary + its drift audit, not the whole `docs/` tree —
the build script names its inputs explicitly. Internal proposals and runbooks are
not published as polished pages by this workflow (they remain readable in the
public repo, just not surfaced on the site).
