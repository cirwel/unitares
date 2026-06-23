/*
 * Metrics section — Chronicler fleet/project/infra time-series.
 * Ported from the classic fleet-metrics.js oracle: grouped metric picker
 * (Fleet · Project · Infra · Other), `.error`-twin handling + header badge,
 * daily-scrape status line, and empty/awaiting-scrape states.
 *
 * Redesign deltas vs the oracle:
 *   - Theme-aware: line/grid/tick colours read design tokens via getComputedStyle,
 *     so the chart re-renders correctly in paper or ink (retheme()).
 *   - Category x-axis with MM-DD labels (Chronicler scrapes daily), NOT Chart.js
 *     `type:"time"` — the redesign loads chart.umd without a date adapter.
 * Reads DATA.metricsCatalog() + DATA.metricsSeries().
 */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function rgba(hex, a) {
    const h = (hex || "").replace("#", "");
    if (h.length < 6) return hex || "rgba(136,136,136," + a + ")";
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  // Chronicler scrapes daily → 14 days ≈ 14 points, enough to read trend without
  // dragging in months of cold history. Matches the oracle's window.
  const WINDOW_DAYS = 14;

  // Three unrelated metric families share one catalog (fleet/governance state,
  // project/codebase stats, runtime-infra latency). Classify by name prefix so
  // the picker reads as optgroups; unmatched series fall through to Other so a
  // new series never silently disappears. `.error` twins classify by base name.
  const METRIC_GROUPS = [
    { label: "Fleet", test: /^(agents|checkins|kg)\./ },
    { label: "Project", test: /^(github|tests|tokei)\./ },
    { label: "Infra", test: /^(lease_plane|ode)\./ },
  ];
  const METRIC_GROUP_ORDER = ["Fleet", "Project", "Infra", "Other"];
  function groupLabel(name) {
    const base = name.replace(/\.error$/, "");
    for (const g of METRIC_GROUPS) if (g.test.test(base)) return g.label;
    return "Other";
  }

  let chart = null, currentName = null, catalogCache = [], mounted = false;
  let lastMetric = null, lastPoints = null;

  function fmtLabel(ts) {
    const d = new Date(ts);
    if (isNaN(d)) return String(ts || "");
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getMonth() + 1) + "-" + p(d.getDate());
  }
  function relTime(ts) {
    if (!ts) return "";
    const secs = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + "m ago";
    if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
    return Math.floor(secs / 86400) + "d ago";
  }

  function options(metric) {
    const grid = rgba(cssVar("--ink") || "#888", 0.06);
    const tick = cssVar("--muted") || "#888";
    const surface = cssVar("--surface") || "#222";
    const line = cssVar("--line-2") || "#444";
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: surface, borderColor: line, borderWidth: 1, titleColor: cssVar("--ink"), bodyColor: tick, titleFont: { family: "Geist Mono" }, bodyFont: { family: "Geist Mono", size: 11 }, padding: 10 },
      },
      scales: {
        x: { grid: { color: grid, drawTicks: false }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 }, maxRotation: 0, autoSkipPadding: 16 } },
        y: { beginAtZero: false, title: { display: !!(metric && metric.unit), text: (metric && metric.unit) || "", color: tick }, grid: { color: grid }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 } } },
      },
    };
  }

  function showEmpty(msg) {
    const canvas = $("#met-chart"), empty = $("#met-empty");
    if (canvas) canvas.style.display = "none";
    if (empty) { empty.style.display = ""; empty.textContent = msg; }
    if (chart) { chart.destroy(); chart = null; }
    lastMetric = null; lastPoints = null;
  }

  function paint(metric, points) {
    const canvas = $("#met-chart"), empty = $("#met-empty");
    if (!canvas) return;
    if (!points || points.length === 0) {
      showEmpty(`No data for "${metric.name}" in the last ${WINDOW_DAYS} days. Chronicler runs daily — refresh after the next cycle.`);
      return;
    }
    canvas.style.display = "";
    if (empty) empty.style.display = "none";
    lastMetric = metric; lastPoints = points;
    const labels = points.map((p) => fmtLabel(p.ts));
    const data = points.map((p) => p.value);
    const color = cssVar("--accent") || "#d97757";
    const label = metric.name + (metric.unit ? " (" + metric.unit + ")" : "");
    if (chart) {
      chart.data.labels = labels;
      chart.data.datasets[0].data = data;
      chart.data.datasets[0].label = label;
      chart.data.datasets[0].borderColor = color;
      chart.data.datasets[0].backgroundColor = rgba(color, 0.13);
      chart.options = options(metric);
      chart.update();
      return;
    }
    if (!window.Chart) { showEmpty("Chart.js not loaded."); return; }
    chart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels, datasets: [{ label, data, borderColor: color, backgroundColor: rgba(color, 0.13), borderWidth: 2, fill: true, tension: 0.25, pointRadius: 3 }] },
      options: options(metric),
    });
  }

  function setBadge(activeErrorNames) {
    const badge = $("#met-err-badge");
    if (!badge) return;
    if (!activeErrorNames || activeErrorNames.length === 0) { badge.hidden = true; badge.textContent = ""; badge.title = ""; return; }
    badge.hidden = false;
    badge.textContent = "⚠ " + activeErrorNames.length + " error" + (activeErrorNames.length === 1 ? "" : "s");
    badge.title = "Failing scrapers: " + activeErrorNames.join(", ");
  }
  function setDescription(text) { const el = $("#met-desc"); if (el) el.textContent = text || ""; }
  function setScrapeStatus(points) {
    const el = $("#met-scrape");
    if (!el) return;
    if (!points || points.length === 0) { el.textContent = "no data in last " + WINDOW_DAYS + "d — awaiting scrape"; el.title = ""; return; }
    const newest = points[points.length - 1];
    el.textContent = "last scrape: " + relTime(newest.ts) + " · " + points.length + " pt" + (points.length === 1 ? "" : "s") + " · " + WINDOW_DAYS + "d window";
    el.title = newest.ts;
  }

  function populatePicker(metrics) {
    const select = $("#met-select");
    if (!select) return;
    select.innerHTML = "";
    if (metrics.length === 0) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "(no metrics registered)";
      select.appendChild(o);
      return;
    }
    const buckets = {};
    metrics.forEach((m) => { (buckets[groupLabel(m.name)] = buckets[groupLabel(m.name)] || []).push(m); });
    METRIC_GROUP_ORDER.forEach((gl) => {
      const items = buckets[gl];
      if (!items || items.length === 0) return;
      const group = document.createElement("optgroup");
      group.label = gl;
      items.forEach((m) => {
        const o = document.createElement("option");
        o.value = m.name; o.textContent = m.name;
        if (m.description) o.title = m.description;
        group.appendChild(o);
      });
      select.appendChild(group);
    });
    if (!currentName || !metrics.some((m) => m.name === currentName)) currentName = metrics[0].name;
    select.value = currentName;
  }

  function renderShell(source) {
    $("#met-mount").innerHTML =
      `<div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4);flex-wrap:wrap">
         <span class="eyebrow" style="margin:0">Fleet metrics · Chronicler · last ${WINDOW_DAYS}d</span>
         <span class="spring"></span>
         <span id="met-err-badge" class="tag warn" hidden></span>
         <select id="met-select" class="theme-toggle" title="Select a metric series"></select>
         <button id="met-refresh" class="theme-toggle" title="Refresh">↻</button>
         <span class="src-badge ${source}" id="met-src">${source}</span></div>
       <div class="panel">
         <div class="panel-head" style="margin-bottom:var(--space-3)">
           <span class="fresh" id="met-desc"></span><span class="spring"></span>
           <span class="fresh" id="met-scrape"></span></div>
         <div style="height:300px"><canvas id="met-chart"></canvas></div>
         <p class="empty" id="met-empty" style="display:none"></p>
       </div>`;
    $("#met-select").addEventListener("change", function () { currentName = this.value; refreshSeries(); });
    $("#met-refresh").addEventListener("click", load);
  }

  function setSrc(source) { const b = $("#met-src"); if (b) { b.className = "src-badge " + source; b.textContent = source; } }

  // Fetch + paint just the selected series (picker change — no catalog refetch).
  async function refreshSeries() {
    const metric = catalogCache.find((m) => m.name === currentName) || catalogCache[0];
    if (!metric) return;
    setDescription(metric.description || "");
    const r = await DATA.metricsSeries(metric.name, WINDOW_DAYS);
    setSrc(r.source);
    setScrapeStatus(r.data);
    paint(metric, r.data || []);
  }

  async function load() {
    const cat = await DATA.metricsCatalog();
    const raw = cat.data || [];
    // Chronicler auto-twins `<name>.error` slots upfront; they only matter once a
    // scraper has actually failed. Hide empty twins, surface active ones, badge.
    const base = raw.filter((m) => !m.name.endsWith(".error"));
    const activeErr = raw.filter((m) => m.name.endsWith(".error") && m.last_point_ts);
    setBadge(activeErr.map((m) => m.name));
    catalogCache = base.concat(activeErr);

    if (!mounted) { renderShell(cat.source); mounted = true; } else { setSrc(cat.source); }
    populatePicker(catalogCache);
    if (catalogCache.length === 0) { setDescription(""); setScrapeStatus(null); showEmpty("No metrics registered yet."); return; }
    await refreshSeries();
  }

  // Theme toggle: rebuild from cached data so the chart picks up new tokens.
  function retheme() {
    if (!lastMetric || !lastPoints || !window.Chart) return;
    if (chart) { chart.destroy(); chart = null; }
    paint(lastMetric, lastPoints);
  }

  window.Metrics = { load, retheme };
})();
