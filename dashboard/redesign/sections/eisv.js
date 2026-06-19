/*
 * EISV section — fleet trajectory charts (Chart.js).
 * Built from eisv-charts.js oracle, distilled to two line charts:
 *   upper = E, I, coherence (+ coherence equilibrium line)
 *   lower = S, V (+ zero line)
 * Upgrade over the oracle: series/grid/tick colours are read from the
 * design tokens via getComputedStyle, so the charts are THEME-AWARE —
 * they re-render correctly in paper or ink. Reads DATA.eisv().
 */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let MODEL = { series: [], coherenceEq: 0.5, source: "snapshot" };
  let upper = null, lower = null;

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

  function render() {
    $("#eisv-mount").innerHTML =
      `<div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4)">
         <span class="eyebrow" style="margin:0">Fleet trajectory · last ${MODEL.series.length} min</span>
         <span class="spring"></span><span class="src-badge ${MODEL.source}">${MODEL.source}</span></div>
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

  async function load() {
    const r = await DATA.eisv();
    MODEL = { series: r.data.series || [], coherenceEq: r.data.coherenceEq || 0.5, source: r.source };
    render();
  }
  // re-theme without refetch (called on theme toggle)
  function retheme() { if (MODEL.series.length && window.Chart) build(); }

  window.EISV = { load, retheme };
})();
