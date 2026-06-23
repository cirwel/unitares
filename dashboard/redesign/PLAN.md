# Dashboard Redesign — Plan & Decisions

**Status:** **LIVE.** Cut over to the default at `/` and `/dashboard` on 2026-06-19; the classic dashboard is parked at `/dashboard/classic` as the parity oracle/fallback. All increments below have landed. The strangler is essentially complete — what remains is retiring classic (one parity item: `/phase`, see below) and the docs cleanup.
**Approach:** design-system-first *strangler* reskin — NOT a rewrite. Classic stayed live throughout and served as the oracle; it now stays parked until confirmed at full parity, then retires.

## Why not a rewrite

The live dashboard is load-bearing (operator watches Sentinel/Vigil/fleet/identity through it) and its
~17.4k lines of JS encode months of discovered real states. A big-bang rewrite would drop those lessons and
risk a dark cutover. Instead: introduce a token + primitive design layer, then convert one nav section at a
time behind the existing nav. Old and new coexist; each increment ships independently.

## The old dashboard is the oracle (robustness method)

Its accreted special-cases ARE the spec. For each section migrated:
1. Read the OLD JS for every state branch it handles (dark residents, silence detection, tight-margin
   verdicts, lineage/supersession badges, the 4x-broken Fleet Metrics panel, etc.) — mine edge cases, not
   the happy path.
2. Reproduce each state in the new component.
3. Diff the new render against the live panel on real data before the increment lands.
The old code's special-cases are the regression suite; robustness is inherited, not re-derived.

## Locked decisions

- **Theme:** `ink` (dark) default; `paper` (light) first-class via toggle. Default flips in one token line.
- **Accent:** clay (`--accent`: #d97757 ink / #b8502e paper) — the only accent; everything else neutral.
- **Type:** Fraunces (display serif) · Inter (UI) · Geist Mono (data/numbers, tabular figures).
- **Buildless:** prod serves raw HTML/CSS/JS from `dashboard/`. Redesign matches — plain CSS tokens + small
  JS primitives, no framework, no Vite-bundle in the serving path. (Vite/vitest stay for local dev/CI only.)
- **EISV/semantic color is data-only**, desaturated — never decoration. Neutral base carries the layout.
- **Migration unit:** one nav section = one draft PR, validated against the live panel first.

## Design layer (this dir)

- `tokens.css` — the system. One calm base, one accent, semantic data-hues, two themes via `[data-theme]`.
  Replaces the role of the 5,877-line `styles.css`.
- `preview.html` — landing reference (residents strip + stats grid + Pulse) on a real fleet snapshot.
  Component CSS is inline here; it gets extracted into the primitive kit in increment 1.

## Primitive kit (to extract in increment 1)

Card/Stat · Panel · EISVMeter · ResidentChip · VerdictBadge · AttentionBand · Track/Bar · eyebrow label.

## Increment order (risk-ascending) — all shipped

1. ✅ **Landing / Overview** — Stats grid + Pulse + residents strip.
2. ✅ **Agents** — table, filters, pagination, trust tiers, lineage/lifecycle badges.
3. ✅ **Discoveries** — KG list + filters + status actions.
4. ✅ **Dialectic** — sessions, phase/status counts, transcripts.
5. ✅ **Activity** — unified timeline.
6. ✅ **EISV charts** — Chart.js, theme-aware via tokens.
7. ✅ **Resident panels** — Watcher / Sentinel / Vigil / Chronicler / System Health, consolidated into the **Residents** section (`sections/residents.js`).
8. ✅ **Metrics** — Chronicler fleet/project/infra time-series, its own **Metrics** section (`sections/metrics.js`). Ported from the classic `fleet-metrics.js` oracle; the last classic *panel* to reach parity.
9. ➕ **Automations** — automation census/scorecard (a redesign-native section with no classic predecessor).

**Remaining before classic can be deleted:**

- `/phase` (D3 E/I particle plane) is **not** ported — it's a standalone classic route (`dashboard/phase.html` / `phase.js`), independent of the classic shell, so retiring classic's `/dashboard/classic` shell doesn't require it. Decide whether to leave `/phase` as a standalone specialist view or port it as an EISV alt-view.
- Retire `/dashboard/classic` (and its ~17k JS lines) once the above is settled and parity is confirmed on real data.

## Conventions to honor (from the unitares-dashboard skill)

- Script-load chain order in `index.html`; `authFetch` bearer-token helper; `DashboardAPI.callTool` →
  `/v1/tools/call`; `.panel` layout contract; Chart.js dark-theme defaults; file allowlist for served assets.
- Operator write actions need `X-Unitares-Operator` token under STRICT_IDENTITY (#425); reads don't.
