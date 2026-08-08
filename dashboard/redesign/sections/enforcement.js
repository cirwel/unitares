/*
 * Enforcement section — the produced-vs-delivered honesty meter (decision D3,
 * core-math audit 2026-08-06). As deployed the system is an ADVISORY
 * instrument: pause verdicts are produced and recorded, but gap-suppression
 * downgrades them to proceed at any >150s inter-check-in gap (ordinary
 * resident cadence), so a verdict count is NOT an enforcement count. This
 * section puts both meters side by side so no operator view can conflate
 * them. Reads DATA.enforcementDivergence(); no fetch logic here, no styles
 * here. Pure-HTML bars (design tokens) — no Chart.js, so no retheme hook.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function relDays(iso) {
    if (!iso) return "never";
    const ms = Date.now() - Date.parse(iso);
    if (isNaN(ms)) return esc(iso);
    const d = Math.floor(ms / 86400000);
    return d < 1 ? "today" : d + "d ago";
  }

  function bars(weekly) {
    const max = Math.max(1, ...weekly.map((w) => w.produced));
    return weekly.map((w) => {
      const ph = Math.round((w.produced / max) * 64);
      const dh = Math.round((w.delivered / max) * 64);
      return `<div class="enf-col" title="${esc(w.week)}: ${w.produced} produced, ${w.delivered} delivered">
        <div class="enf-bars">
          <div style="height:${ph}px;width:8px;background:var(--warn);opacity:.75;border-radius:2px 2px 0 0"></div>
          <div style="height:${dh}px;width:8px;background:var(--danger);border-radius:2px 2px 0 0"></div>
        </div>
        <div class="enf-wk">${esc(w.week)}</div>
      </div>`;
    }).join("");
  }

  async function load() {
    const mount = $("enforcement-mount");
    if (!mount) return;
    const { source, data: d } = await window.DATA.enforcementDivergence(90);
    if (!d) { mount.innerHTML = `<p class="sub">no data</p>`; return; }
    const suppressedPct = d.produced_pauses
      ? Math.round((d.gap_suppressed / d.produced_pauses) * 100) : 0;
    const stat = (label, value, sub, color) =>
      `<div class="card"><h3>${label}</h3><div class="num"${color ? ` style="color:${color}"` : ""}>${value}</div><div class="sub">${sub}</div></div>`;
    mount.innerHTML = `
      <div class="toolbar" style="display:flex;align-items:center;gap:var(--space-2)">
        <h2>Enforcement <span class="src-badge ${esc(source)}">${esc(source)}</span></h2>
      </div>
      <p class="sub" style="max-width:64ch">
        <strong>Advisory posture (ratified 2026-08-06):</strong> a produced pause
        <em>verdict</em> is not a delivered enforcement <em>action</em>. Gap-suppression
        downgrades pauses to proceed at any &gt;150s check-in gap — ordinary resident
        cadence — so verdict counts must never be read as enforcement counts.
      </p>
      <div class="cards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:var(--space-2)">
        ${stat("Produced pauses", d.produced_pauses, `${d.window_days}d of pause verdicts recorded`)}
        ${stat("Gap-suppressed", `${d.gap_suppressed} <span style="font-size:.6em">(${suppressedPct}%)</span>`, "downgraded to proceed", "var(--warn)")}
        ${stat("Delivered pauses", d.delivered_pauses, "lifecycle_paused events (may include test fixtures)", "var(--danger)")}
        ${stat("Last delivered", relDays(d.last_delivered_at), d.last_delivered_at ? esc(d.last_delivered_at.slice(0, 10)) : "no delivered pause on record")}
      </div>
      <div class="card" style="margin-top:var(--space-2)">
        <h3>Weekly — produced (amber) vs delivered (red)</h3>
        <div style="display:flex;align-items:flex-end;gap:6px;overflow-x:auto;padding-top:var(--space-2)">
          ${bars(d.weekly || [])}
        </div>
      </div>
      <p class="sub">${esc(d.note || "")} Full posture record: the proprioception
      contract, “Deployed posture — ratified 2026-08-06”.</p>
      <style>
        .enf-col{display:flex;flex-direction:column;align-items:center;gap:2px}
        .enf-bars{display:flex;align-items:flex-end;gap:2px;height:64px}
        .enf-wk{font-size:.6rem;color:var(--muted);writing-mode:vertical-rl;transform:rotate(180deg)}
      </style>`;
  }

  window.Enforcement = { load };
})();
