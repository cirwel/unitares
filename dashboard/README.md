# Unitares Governance Dashboard

**Last updated:** June 2026

The operator dashboard is **`dashboard/redesign/`**, served buildless at `/` and
`/dashboard`. See [`redesign/PLAN.md`](redesign/PLAN.md) for its design system,
sections, and data layer.

## Layout

- `redesign/` — the live dashboard (buildless: tokens + small JS primitives, no
  framework). Served by `http_dashboard_redesign` in `src/http_api.py`.
- `phase.html` / `phase.js` — the standalone phase-space view (D3), served at
  `/phase`. Independent of the rest of the dashboard.
- `package.json` / `eslint.config.mjs` — ESLint over the redesign + `phase.js`
  (the dashboard CI job). The dashboard is buildless; there is no bundle step.

## The classic dashboard was retired

The original dashboard — a ~17k-line static `index.html` + `dashboard.js` +
per-panel modules + `styles.css` — was the operator UI until the redesign cut
over on **2026-06-19**. It was removed once the redesign reached parity
(resident panels consolidated into the **Residents** section; Chronicler's
time-series became the **Metrics** section).

**Recovering a classic file.** It lives in git history. The last commit that
contains the classic dashboard is `7c6037b` (master, pre-retirement):

```bash
git show 7c6037b:dashboard/dashboard.js        # view a file
git checkout 7c6037b -- dashboard/agents.js    # restore one into the worktree
```

**Known non-parity (intentional, see PLAN.md).** The redesign is read-only —
the classic operator write actions (archive/resume agent, request dialectic
review, update discovery status) were **not** ported; do those via the MCP
tools / CLI. The classic Resident-Progress panel and the richer EISV views
(heatmap, ODE overlay, per-agent mode) were also not carried over.
