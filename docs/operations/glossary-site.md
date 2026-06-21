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

## How publishing works (branch-source, token-only)

The workflow builds the site and **pushes it to the `gh-pages` branch** with
`contents: write`. It deliberately does *not* use the Pages API or the
Actions-source deploy: the workflow `GITHUB_TOKEN` cannot create a Pages site
(`Resource not accessible by integration` — tried in #986), so an API/Actions path
needs a human to enable Pages first. Pushing a branch needs no such permission, so
**the workflow always succeeds** and the published HTML lands on `gh-pages`.

Going live then depends on GitHub's branch-source behavior:

- If Pages auto-serves `gh-pages`, the site is live at
  **https://cirwel.github.io/unitares/** with no manual step.
- If it does not, enable it **once** (no re-run needed):
  **Settings → Pages → Build and deployment → Source = "Deploy from a branch" →
  Branch = `gh-pages` / `/ (root)`**.

History note: the Actions-source + `configure-pages enablement: true` paths were
tried first (#985, #986) and both hit the token's inability to create the Pages
site. Branch-source is the token-only route (#987) and at worst reduces the manual
step to a one-click branch selection.

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
