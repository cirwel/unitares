/*
 * EISV section — fleet trajectory charts (Chart.js) + per-resident heatmap.
 * Built from eisv-charts.js oracle, distilled to two line charts:
 *   upper = E, I, coherence (+ coherence equilibrium line)
 *   lower = S, V (+ zero line)
 * Plus a Fleet heatmap (revived from the classic dashboard): a residents ×
 * {E,I,S,V,coherence} grid so an outlier resident pops out instead of being
 * averaged into the blended fleet line. Reads DATA.eisv() + DATA.residents().
 * Upgrade over the oracle: series/grid/tick colours are read from the design
 * tokens via getComputedStyle, and heatmap cells use color-mix(var(--ok)…
 * var(--danger)), so everything is THEME-AWARE — re-renders correctly in
 * paper or ink.
 */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let MODEL = { series: [], coherenceEq: 0.5, source: "snapshot" };
  let upper = null, lower = null;
  // Raw eisv_update events in the live window — seeded from the REST backfill,
  // then grown by pushed events so the chart re-buckets in place (true diff-push
  // instead of a full refetch). Bounded so a long-lived tab can't grow unbounded.
  let RAW = [];
  const RAW_MAX = 400;

  function rgba(hex, a) {
    const h = hex.replace("#", "");
    if (h.length < 6) return hex;
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  function baseOptions(extraY) {
    const grid = rgba(cssVar("--ink") || "#888", 0.06);
    const tick = cssVar("--muted") || "#888";
    const surface = cssVar("--surface") || "#222";
    const line = cssVar("--line-2") || "#444";
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, position: "bottom", labels: { color: tick, font: { family: "Inter", size: 11 }, boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { backgroundColor: surface, borderColor: line, borderWidth: 1, titleColor: cssVar("--ink"), bodyColor: tick, titleFont: { family: "Geist Mono" }, bodyFont: { family: "Geist Mono", size: 11 }, padding: 10 },
      },
      scales: {
        x: { grid: { color: grid, drawTicks: false }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 }, maxRotation: 0, autoSkipPadding: 16 } },
        y: Object.assign({ grid: { color: grid }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 }, callback: (v) => v.toFixed(2) } }, extraY),
      },
    };
  }

  function ds(label, data, color, opts) {
    return Object.assign({ label, data, borderColor: color, backgroundColor: rgba(color, 0.08), borderWidth: 2, pointRadius: 0, tension: 0.35, fill: true }, opts || {});
  }

  // dashed reference-line plugin (equilibrium / zero)
  function refLine(value, color) {
    return {
      id: "ref" + value, afterDraw(chart) {
        const { ctx, chartArea: { left, right }, scales: { y } } = chart;
        if (!y) return;
        const yp = y.getPixelForValue(value);
        ctx.save(); ctx.beginPath(); ctx.setLineDash([4, 4]); ctx.strokeStyle = rgba(color, 0.5);
        ctx.moveTo(left, yp); ctx.lineTo(right, yp); ctx.stroke(); ctx.restore();
      },
    };
  }

  function build() {
    if (upper) { upper.destroy(); upper = null; }
    if (lower) { lower.destroy(); lower = null; }
    const s = MODEL.series, labels = s.map((p) => p.t);
    const E = cssVar("--eisv-e"), I = cssVar("--eisv-i"), Sc = cssVar("--eisv-s"), V = cssVar("--eisv-v"), C = cssVar("--eisv-c");
    const muted = cssVar("--muted");

    upper = new Chart($("#eisv-upper"), {
      type: "line",
      data: { labels, datasets: [
        ds("Energy", s.map((p) => p.E), E),
        ds("Integrity", s.map((p) => p.I), I),
        ds("Coherence", s.map((p) => p.C), C, { fill: false, borderDash: [5, 4], borderWidth: 1.5 }),
      ] },
      options: baseOptions({ min: 0, max: 1 }),
      plugins: [refLine(MODEL.coherenceEq, muted)],
    });
    lower = new Chart($("#eisv-lower"), {
      type: "line",
      data: { labels, datasets: [
        ds("Entropy", s.map((p) => p.S), Sc),
        ds("Valence", s.map((p) => p.V), V),
      ] },
      options: baseOptions({ min: -0.6, max: 1 }),
      plugins: [refLine(0, muted)],
    });
  }

  // Fleet heatmap — residents × {E,I,S,V,coherence}. Each cell is tinted from
  // a per-metric "health" fraction (high-good for E/I/coherence, low-good for
  // S, near-zero-good for V) via color-mix between the --ok and --danger
  // tokens, so the colour scale follows the active theme with no JS recompute.
  // Lives in its own #eisv-heatmap container so a periodic refresh can swap it
  // without tearing down the Chart.js canvases beside it.
  function heatmapHTML(residents) {
    const rows = (residents || []).filter((r) => r && r.eisv && r.eisv.E != null);
    if (!rows.length) return "";
    const clamp = (x) => Math.max(0, Math.min(1, x == null ? 0 : x));
    const fmt = (x) => (x == null ? "—" : Number(x).toFixed(2));
    const cols = [
      { label: "E", val: (r) => r.eisv.E, frac: (r) => clamp(r.eisv.E) },
      { label: "I", val: (r) => r.eisv.I, frac: (r) => clamp(r.eisv.I) },
      { label: "S", val: (r) => r.eisv.S, frac: (r) => clamp(1 - r.eisv.S) },
      { label: "V", val: (r) => r.eisv.V, frac: (r) => clamp(1 - Math.abs(r.eisv.V)) },
      { label: "Coh", val: (r) => r.coherence, frac: (r) => clamp(r.coherence) },
    ];
    const cell = (frac, value, title) => {
      const pct = Math.round(frac * 100);
      return `<div title="${esc(title)}" style="background:color-mix(in srgb, var(--ok) ${pct}%, var(--danger));color:#fff;font-family:var(--font-mono);font-size:var(--text-sm);text-align:center;padding:6px 0;border-radius:var(--radius-1)">${value}</div>`;
    };
    const headLbl = (t) => `<div style="text-align:center;font-size:var(--text-xs);color:var(--muted);text-transform:uppercase;letter-spacing:var(--tracking-label)">${t}</div>`;
    const header = `<div></div>` + cols.map((c) => headLbl(c.label)).join("");
    const body = rows.map((r) => {
      const name = `<div style="font-size:var(--text-sm);color:var(--ink-2);display:flex;align-items:center;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(r.name)}</div>`;
      return name + cols.map((c) => cell(c.frac(r), fmt(c.val(r)), `${r.name} ${c.label} = ${fmt(c.val(r))}`)).join("");
    }).join("");
    return `<div class="panel" style="margin-bottom:var(--space-5)">
        <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Fleet heatmap</h2>
          <span class="spring"></span><span class="fresh">green = healthy · red = strained</span></div>
        <div style="display:grid;grid-template-columns:minmax(72px,1.4fr) repeat(${cols.length}, 1fr);gap:4px;align-items:stretch">
          ${header}${body}</div></div>`;
  }

  function render() {
    $("#eisv-mount").innerHTML =
      `<div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4)">
         <span class="eyebrow" style="margin:0">Fleet trajectory · last ${MODEL.series.length} min</span>
         <span class="spring"></span><span class="src-badge ${MODEL.source}">${MODEL.source}</span></div>
       <div id="eisv-heatmap">${heatmapHTML(MODEL.residents)}</div>
       <div class="panel" style="margin-bottom:var(--space-5)">
         <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Energy · Integrity · Coherence</h2></div>
         <div style="height:240px"><canvas id="eisv-upper"></canvas></div>
       </div>
       <div class="panel">
         <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Entropy · Valence</h2></div>
         <div style="height:200px"><canvas id="eisv-lower"></canvas></div>
       </div>`;
    if (window.Chart) build();
    else $("#eisv-mount").insertAdjacentHTML("beforeend", `<p class="empty">Chart.js not loaded.</p>`);
  }

  // Update the existing charts' data in place (smooth, no rebuild flicker).
  function updateInPlace() {
    const s = MODEL.series, labels = s.map((p) => p.t);
    upper.data.labels = labels;
    upper.data.datasets[0].data = s.map((p) => p.E);
    upper.data.datasets[1].data = s.map((p) => p.I);
    upper.data.datasets[2].data = s.map((p) => p.C);
    lower.data.labels = labels;
    lower.data.datasets[0].data = s.map((p) => p.S);
    lower.data.datasets[1].data = s.map((p) => p.V);
    upper.update(); lower.update();
    // Swap the heatmap in place too — its own container, so the canvases above
    // are untouched.
    const hm = document.getElementById("eisv-heatmap");
    if (hm) hm.innerHTML = heatmapHTML(MODEL.residents);
    const badge = document.querySelector("#eisv-mount .src-badge");
    if (badge) { badge.className = "src-badge " + MODEL.source; badge.textContent = MODEL.source; }
  }

  async function load() {
    // Fleet trajectory (DATA.eisv) and the per-resident snapshot (DATA.residents)
    // in one batch — the heatmap reads the latter.
    const [r, res] = await Promise.all([DATA.eisv(), DATA.residents()]);
    RAW = (r.data.raw || []).slice(-RAW_MAX);
    MODEL = {
      series: r.data.series || [], coherenceEq: r.data.coherenceEq || 0.5,
      source: r.source, residents: (res && res.data) || [],
    };
    // Refresh in place if the charts are already mounted; full render on first load.
    if (upper && lower && document.getElementById("eisv-upper") && window.Chart) updateInPlace();
    else render();
  }

  // Apply one pushed eisv_update directly — no refetch. Returns true if it
  // handled the event (the caller then skips the doorbell refetch). Only acts
  // once the charts are mounted; first paint still goes through load().
  function applyEvent(msg) {
    if (!msg || msg.type !== "eisv_update" || !msg.eisv || !msg.timestamp) return false;
    if (!upper || !lower || !window.Chart) return false;
    RAW.push({ timestamp: msg.timestamp, eisv: msg.eisv, coherence: msg.coherence, risk: msg.risk });
    if (RAW.length > RAW_MAX) RAW = RAW.slice(-RAW_MAX);
    MODEL.series = DATA.bucketEisv(RAW);
    MODEL.source = "live"; // a live push by definition
    updateInPlace();
    return true;
  }
  // re-theme without refetch (called on theme toggle) — full rebuild reads new tokens
  function retheme() { if (MODEL.series.length && window.Chart) build(); }

  window.EISV = { load, retheme, applyEvent };
})();
