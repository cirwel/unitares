---
name: unitares-dashboard
description: >
  Use when adding, editing, or reviewing panels on the unitares dashboard
  (dashboard/index.html and peer dashboard/*.js modules). Captures the
  conventions that broke a recent Fleet Metrics panel four times before
  they were visible: file allowlist, Chart.js dark-theme defaults, the
  authFetch helper, the script-load chain, and the .panel layout
  contract. A repo-specific reference — not general dashboard advice.
last_verified: "2026-06-11"
freshness_days: 30
source_files:
  - unitares/dashboard/index.html
  - unitares/dashboard/eisv-charts.js
  - unitares/dashboard/utils.js
  - unitares/dashboard/colors.js
  - unitares/dashboard/styles.css
  - unitares/src/http_api.py
---

# Adding a Panel to the UNITARES Dashboard

## Why This Skill Exists

The unitares dashboard has five invisible conventions a new panel must follow. Missing any one of them produces a panel that "looks bad" in ways the operator cannot articulate precisely — invisible axis labels, 404 JavaScript, foreign fonts, drifted colors. A recent Fleet Metrics panel shipped, then required **four** follow-up PRs to get right because I treated each symptom in isolation. This skill captures all five conventions so the next panel ships correct on the first try.

**Core principle:** On this dashboard, "the chart renders" and "the chart is readable" are two different problems. Chart.js's defaults actively fight the dark theme.

## Quick Reference — Panel Integration Checklist

When adding a new panel, every item below must be done:

| # | Do | File | Why |
|---|----|----|-----|
| 1 | Add filename to `allowed_files` list | `src/http_api.py::http_dashboard_static()` | Static handler rejects (403) anything not on the allowlist — the browser silently fails to load |
| 2 | Add `<script src="/dashboard/NAME.js"></script>` **without** `defer` | `dashboard/index.html` (Layer 2 block) | All peer modules load non-deferred and the IIFE has its own `DOMContentLoaded` guard |
| 3 | Use `authFetch(url)` from `utils.js` | module JS | Shared helper reads the canonical `unitares_api_token` localStorage key; don't re-invent |
| 4 | Set `Chart.defaults.color`, `.font.family`, `.borderColor` from body CSS vars | before `new Chart()` | Chart.js defaults to dark-grey ticks on your dark-grey background — invisible axis labels |
| 5 | Copy option structure from `eisv-charts.js::makeChartOptions` | module JS | Themed tooltip (`rgba(13,13,18,0.9)`), mono body font, white-alpha grid, `interaction.mode: 'index'` at **top level** not under `tooltip` (Chart.js v4 change) |
| 6 | Use `MetricColors.HEX.chart*` for line colors | `colors.js` | Eight curated series colors; don't hand-pick hex |
| 7 | Panel container `<div class="panel" id="SECTION">` with `.panel-header` | `dashboard/index.html` | `.panel` provides `padding: 25px`, flex column, `max-height: 800px`, and the `::after` accent bar |
| 8 | Chart wrapper needs `position: relative; height: Npx; contain: strict` | `styles.css` | Chart.js's ResizeObserver will thrash parent layout without `contain: strict` |
| 9 | Canvas: `width: 100% !important; height: 100% !important` | `styles.css` | Required when Chart.js `maintainAspectRatio: false` lives inside a fixed-height parent |
| 10 | Add nav link `<a href="#SECTION" class="section-nav-item" data-section="SECTION">…</a>` | `dashboard/index.html` top nav | Scroll-spy wires automatically by matching `id` |

## The Chart.js Dark-Theme Trap (Item 4) — Biggest Pitfall

Chart.js 4's defaults:
- `Chart.defaults.color` = near-black
- `Chart.defaults.font.family` = Helvetica
- `scales.*.ticks.color` and `.grid.color` inherit the same

On a dark panel, all of this renders **invisible**. The line draws in your chosen color, so you see "a colored squiggle in a void" — no axis labels, no tooltip text (default tooltip is white-on-white against dark bg).

**Fix once at panel init:**

```js
function applyChartDefaults() {
    if (typeof Chart === 'undefined' || !Chart.defaults) return;
    var bodyStyle = getComputedStyle(document.body);
    var textSecondary = (bodyStyle.getPropertyValue('--text-secondary') || '').trim() || '#a0a0b0';
    var fontFamily = (bodyStyle.getPropertyValue('--font-family') || '').trim() || "'Outfit', sans-serif";
    Chart.defaults.color = textSecondary;
    if (Chart.defaults.font) Chart.defaults.font.family = fontFamily;
    Chart.defaults.borderColor = 'rgba(255,255,255,0.08)';
}
```

And in every chart's `options`, explicitly set tooltip theme + tick colors — don't rely on inheritance alone:

```js
options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    interaction: { mode: 'index', intersect: false },     // v4: top level, NOT under tooltip
    plugins: {
        legend: { display: false },
        tooltip: {
            backgroundColor: 'rgba(13,13,18,0.9)',
            titleFont: { family: "'Inter', sans-serif" },
            bodyFont: { family: "'JetBrains Mono', monospace", size: 12 },
            padding: 10,
            borderColor: '#333',
            borderWidth: 1,
        },
    },
    scales: {
        x: {
            type: 'time',
            grid: { color: 'rgba(255,255,255,0.05)' },
            ticks: { color: '#a0a0b0', font: { size: 11 }, maxRotation: 0 },
        },
        y: {
            grid: { color: 'rgba(255,255,255,0.05)' },
            ticks: {
                color: '#a0a0b0',
                font: { family: "'JetBrains Mono', monospace", size: 11 },
            },
        },
    },
}
```

The reference implementation is `dashboard/eisv-charts.js::makeChartOptions`. Copy that function's shape; vary only the specifics.

## The Allowlist Trap (Item 1)

`src/http_api.py::http_dashboard_static()` has a hardcoded `allowed_files` list. A file not on it returns a **403** with JSON body `{"error": "File not allowed"}` — not a 404. The browser still fails to load the script and `Console` shows the failed request. Easy to miss.

Verify by `curl`:
```bash
curl -sI http://127.0.0.1:8767/dashboard/NAME.js | head -1
# Must be: HTTP/1.1 200 OK
curl -s http://127.0.0.1:8767/dashboard/NAME.js | head -1
# Must be JavaScript, not `{"error":…}`
```

The allowlist is Python code loaded at process start — adding a name requires a governance-mcp **restart**, not just a `git pull`. Restart:
```bash
launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
```

A regression guard for this lives in `tests/test_dashboard_static_allowlist.py` — it parses index.html for every `/dashboard/<file>` href and asserts each is on the list.

## Module Skeleton (Copy This)

```js
(function () {
    'use strict';

    var chart = null;
    var currentFoo = null;

    function applyChartDefaults() { /* see above */ }

    async function fetchData() {
        try {
            var resp = await authFetch('/api/whatever');        // shared helper
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return await resp.json();
        } catch (e) {
            console.warn('[NAME] fetch failed:', e);
            return null;
        }
    }

    function renderChart(ctx, data) {
        if (chart) chart.destroy();                             // always destroy before re-create
        var lineColor = MetricColors.HEX.chartCoherence;        // palette, not hand-picked
        chart = new Chart(ctx, { type: 'line', data: {...}, options: chartOptionsFor(...) });
    }

    function wire() {
        applyChartDefaults();
        // attach event listeners, initial fetch
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }

    window.NAMEPanel = { refresh: /* … */ };
})();
```

## Verification Before Claiming Done

Before declaring a dashboard change ready:

1. **`curl` the JS file** — must be 200 with JavaScript body, not `{"error":"File not allowed"}`
2. **Hard-refresh the browser** (⌘⇧R) — script cache-busting doesn't override localStorage / service worker caches
3. **Open DevTools → Network** — confirm JS loads 200, no 403/404 on any `/dashboard/*` asset
4. **DevTools → Console** — no errors, especially not `Chart is not defined` or `authFetch is not defined`
5. **DevTools → Elements** — hover a tick label; `computed color` must NOT be `rgb(102,102,102)` or similar dark grey
6. **Screenshot from the operator** if you are reasoning blind — you cannot verify "looks bad" without seeing pixels

## Red Flags — You're About to Thrash

If any of these are true, **stop and run subagent research** before writing another PR:

- You've shipped ≥2 iterations for the same panel and the operator still reports "looks bad"
- You're guessing at what "looks bad" means (height? color? layout?)
- You're changing dimensions without evidence from DevTools
- The operator can't articulate the issue precisely

The correct move at this point is to dispatch agents to:
- Survey the full dashboard conventions (Explore agent, thoroughness "very thorough")
- Audit the new panel vs. working peers (code-reviewer)
- Give a skeptical "what's the most-likely remaining issue" take (superpowers:code-reviewer)

One hour of three parallel agents beats four iterations of single-guess PRs.

## Anti-Patterns That Burned Hours

| Anti-pattern | What it felt like | What it actually was |
|---|---|---|
| Shrink chart height to "fix looks bad" | "Panel is too tall" | Panel was fine; tick labels invisible |
| Add `defer` to new script tag | "Seems safer, load after DOM" | Breaks initialization order vs. peers |
| Invent a hex color like `#4a9eff` | "Looks close to the theme" | Not in `MetricColors.HEX`; drifts |
| Write private `getAuthToken()` helper | "Self-contained is cleaner" | Duplicates `utils.js::authFetch`, wrong key order |
| Inline `style="height:320px"` on chart container | "One less CSS class" | Doesn't match theme conventions; no `contain: strict` |

## When NOT to Use This Skill

- Editing the `phase.html` view — different layout, different rules
- Pure server-side changes that don't render anything
- Work in `agents/*/` resident agents — they don't have panels
- Any dashboard beyond `/dashboard` path
