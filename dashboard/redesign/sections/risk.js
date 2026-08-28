/*
 * Risk section — the time axis risk never had.
 *
 * Risk was the one governance quantity the dashboard showed only as an
 * instantaneous scalar: a bar on the landing page, a column in the agents
 * table. Every horizon of it already existed in the data layer and none of it
 * reached a chart — `bucketEisv` computed a fleet-average `R` that nothing
 * read, and `/v1/agents/{id}/history` returned a `risk` per point that the
 * EISV trajectory chart dropped on the floor. This section gives those
 * horizons a surface:
 *
 *   1. Fleet mean risk       — Chronicler's daily scrape of governance.risk.mean.7d
 *   2. Verdict pressure      — governance.pause.7d + governance.guide.7d
 *   3. Per-agent trajectory  — one resident's own risk over its lifespan
 *
 * Reads DATA.riskTrend(), DATA.residents() and DATA.agentHistory(). No fetch
 * logic here.
 *
 * ── Three things this view must not say ───────────────────────────────────
 *
 * A produced pause verdict is NOT a delivered enforcement action. Gap-
 * suppression downgrades pauses to proceed at any >150s inter-check-in gap —
 * ordinary resident cadence — so the pause series is a count of verdicts
 * produced, never of interventions delivered. The Enforcement section carries
 * the full produced-vs-delivered meter; this one links to it rather than
 * quietly implying the stronger reading.
 *
 * `risk` here is DECISION risk — the scalar paired with the governance
 * verdict. It is not Φ-derived risk telemetry, which is expressive but has
 * never been shown to carry outcome signal and is never recovery authority.
 * The agents table already keeps those two named apart; so does this.
 *
 * And the persisted risk is PRE-adjustment. The trajectory-identity enrichment
 * (enrichments.py, order=170) shifts risk by up to ±0.15 for trust tier and
 * lineage anomaly, but it writes `ctx.response_data["metrics"]` — the response
 * envelope — while `record_agent_state` persists `ctx.risk_score`, which comes
 * from the separate `ctx.metrics_dict`. So the number an agent is told about
 * itself and the number charted here can differ. That seam is stated in the
 * footer instead of being smoothed over.
 *
 * Colour discipline follows the EISV section: fleet readings use the neutral
 * accent rather than converting an observation into a red/green verdict. Only
 * the pause bars take --warn, where the quantity really is an intervention
 * count. No threshold bands — this view does not assert a risk cut it cannot
 * source.
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

  const WINDOWS = [30, 60, 90, 180];
  let windowDays = 60;

  let trendChart = null, pressureChart = null, agentChart = null;
  let mounted = false;
  // Cached model so retheme() can rebuild from data without refetching.
  let TREND = null, RESIDENTS = [], AGENT = { id: null, name: null, points: [], loading: false, total: 0 };

  const num = (x, d) => (x == null || isNaN(x) ? "—" : Number(x).toFixed(d == null ? 3 : d));

  // Chronicler scrapes daily → MM-DD category labels. NOT a Chart.js `time`
  // scale: app.html loads chart.umd without the date adapter, so a time axis
  // renders blank.
  function fmtDay(ts) {
    const d = new Date(ts);
    if (isNaN(d)) return String(ts || "");
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getMonth() + 1) + "-" + p(d.getDate());
  }
  function fmtStamp(ts) {
    const d = new Date(ts);
    if (isNaN(d)) return String(ts || "");
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
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
        y: Object.assign({ grid: { color: grid }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 } } }, extraY || {}),
      },
    };
  }

  // ── panel 1: fleet mean risk ────────────────────────────────────────────
  function buildTrend() {
    if (trendChart) { trendChart.destroy(); trendChart = null; }
    const canvas = $("#risk-trend");
    const pts = (TREND && TREND.risk) || [];
    if (!canvas || !window.Chart || !pts.length) return;
    const accent = cssVar("--accent") || "#d97757";
    trendChart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: pts.map((p) => fmtDay(p.ts)),
        datasets: [{
          label: "fleet mean risk (7d rolling)", data: pts.map((p) => p.value),
          borderColor: accent, backgroundColor: rgba(accent, 0.13),
          borderWidth: 2, fill: true, tension: 0.25, pointRadius: 2,
        }],
      },
      // beginAtZero: fleet risk sits near the floor (~0.01–0.07). Letting the
      // axis auto-zoom to that band turns scrape jitter into a mountain range.
      options: baseOptions({ beginAtZero: true, ticks: { callback: (v) => Number(v).toFixed(2) } }),
    });
  }

  // ── panel 2: verdict pressure (dual axis — counts differ by ~500x) ──────
  function buildPressure() {
    if (pressureChart) { pressureChart.destroy(); pressureChart = null; }
    const canvas = $("#risk-pressure");
    const pause = (TREND && TREND.pause) || [], guide = (TREND && TREND.guide) || [];
    if (!canvas || !window.Chart || !(pause.length || guide.length)) return;
    const warn = cssVar("--warn") || "#d4a24c";
    const teal = cssVar("--eisv-c") || "#3f7d93";
    const tick = cssVar("--muted") || "#888";
    const grid = rgba(cssVar("--ink") || "#888", 0.06);
    const spine = pause.length >= guide.length ? pause : guide;
    const opts = baseOptions();
    // Guide runs in the thousands against pause in the tens — one axis would
    // flatten pause into the baseline. Right axis carries guide, and its grid
    // is suppressed so two rulers don't overprint each other.
    opts.scales.y = { position: "left", beginAtZero: true, grid: { color: grid }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 }, precision: 0 }, title: { display: true, text: "pause verdicts", color: tick, font: { family: "Inter", size: 10 } } };
    opts.scales.y1 = { position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { color: tick, font: { family: "Geist Mono", size: 10 } }, title: { display: true, text: "guide verdicts", color: tick, font: { family: "Inter", size: 10 } } };
    pressureChart = new window.Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: spine.map((p) => fmtDay(p.ts)),
        datasets: [
          { label: "pause produced (7d)", data: pause.map((p) => p.value), backgroundColor: rgba(warn, 0.65), borderColor: warn, borderWidth: 1, yAxisID: "y", order: 2 },
          { label: "guide (7d)", type: "line", data: guide.map((p) => p.value), borderColor: teal, backgroundColor: rgba(teal, 0.08), borderWidth: 2, pointRadius: 0, tension: 0.3, fill: false, yAxisID: "y1", order: 1 },
        ],
      },
      options: opts,
    });
  }

  // ── panel 3: one agent's risk over its own lifespan ─────────────────────

  // The decision vocabulary as it is actually persisted (verified against 30d
  // of core.agent_state: guide 27175, approve 12102, cirs_block 38,
  // risk_pause 20). `pause` here means every action that is not approve/guide,
  // which is exactly how governance.pause.7d counts them — so the chart and the
  // fleet series can never disagree about what a pause is.
  //
  // A missing action is its own class. Rows written before the action-write
  // landed carry none, and reading those as "approve" would invent a clean
  // record the data does not support.
  function actionClass(action) {
    if (!action) return "unknown";
    if (action === "approve" || action === "proceed") return "approve";
    if (action === "guide") return "guide";
    return "pause";
  }

  function actionCounts(points) {
    const c = { approve: 0, guide: 0, pause: 0, unknown: 0 };
    points.forEach((p) => { c[actionClass(p.action)] += 1; });
    return c;
  }

  function buildAgent() {
    if (agentChart) { agentChart.destroy(); agentChart = null; }
    const canvas = $("#risk-agent");
    if (!canvas || !window.Chart || !AGENT.points.length) return;
    const accent = cssVar("--accent") || "#d97757";
    const teal = cssVar("--eisv-c") || "#3f7d93";
    const danger = cssVar("--danger") || "#d4705e";
    agentChart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: AGENT.points.map((p) => fmtStamp(p.t)),
        datasets: [
          // Only the hard actions get a marker. Guide is ~69% of all rows —
          // marking those would paint the whole line and bury the 0.1% that a
          // reader actually needs to find.
          {
            label: "decision risk", data: AGENT.points.map((p) => p.risk),
            borderColor: accent, backgroundColor: rgba(accent, 0.1),
            borderWidth: 2, tension: 0.3, fill: true,
            pointRadius: AGENT.points.map((p) => (actionClass(p.action) === "pause" ? 4 : 0)),
            pointHoverRadius: AGENT.points.map((p) => (actionClass(p.action) === "pause" ? 6 : 3)),
            pointBackgroundColor: AGENT.points.map((p) => (actionClass(p.action) === "pause" ? danger : accent)),
            pointBorderColor: AGENT.points.map((p) => (actionClass(p.action) === "pause" ? danger : accent)),
          },
          // Coherence as a faint reference on the same 0–1 axis. It is here to
          // show scale, not to be read as a second risk signal: fleet coherence
          // varies across residents by ~0.03 where risk varies by ~0.3.
          { label: "coherence (reference)", data: AGENT.points.map((p) => p.coherence), borderColor: teal, backgroundColor: "transparent", borderWidth: 1.25, borderDash: [5, 4], pointRadius: 0, tension: 0.3, fill: false },
        ],
      },
      options: agentOptionsFor(),
    });
  }

  // Tooltip names the recorded action, so a marked point says which verdict was
  // produced instead of leaving the reader to guess from the colour.
  function agentOptionsFor() {
    const o = baseOptions({ min: 0, max: 1, ticks: { callback: (v) => Number(v).toFixed(2) } });
    o.plugins.tooltip.callbacks = {
      afterBody: (items) => {
        const i = items && items[0] && items[0].dataIndex;
        const p = typeof i === "number" ? AGENT.points[i] : null;
        if (!p) return "";
        const act = p.action || "no action recorded";
        const verdict = p.verdict ? " · " + p.verdict : "";
        return actionClass(p.action) === "pause"
          ? "action: " + act + verdict + " (produced, not delivered)"
          : "action: " + act + verdict;
      },
    };
    return o;
  }

  function statCards() {
    const pts = (TREND && TREND.risk) || [];
    const latest = pts.length ? pts[pts.length - 1] : null;
    const pausePts = (TREND && TREND.pause) || [];
    const latestPause = pausePts.length ? pausePts[pausePts.length - 1] : null;
    const withRisk = RESIDENTS.filter((r) => typeof r.risk === "number");
    const top = withRisk.slice().sort((a, b) => b.risk - a.risk)[0] || null;
    const lo = withRisk.length ? Math.min.apply(null, withRisk.map((r) => r.risk)) : null;
    const hi = withRisk.length ? Math.max.apply(null, withRisk.map((r) => r.risk)) : null;
    const card = (label, value, sub, title) =>
      `<div class="card"${title ? ` title="${esc(title)}"` : ""}><h3>${esc(label)}</h3><div class="num">${value}</div><div class="sub">${esc(sub)}</div></div>`;
    return [
      card("Fleet mean risk", num(latest ? latest.value : null),
        latest ? "7d rolling · scraped " + fmtDay(latest.ts) : "no scrape in window",
        "governance.risk.mean.7d — fleet-mean risk_score over non-synthetic check-ins"),
      card("Pause verdicts", latestPause ? String(latestPause.value) : "—",
        "produced in trailing 7d — not deliveries",
        "A produced pause is not a delivered enforcement action; gap-suppression downgrades at >150s check-in gaps."),
      card("Highest-risk resident", top ? num(top.risk) : "—",
        top ? top.name : "no resident risk available",
        "Current decision risk, live residents"),
      card("Resident spread", lo == null ? "—" : num(lo, 2) + "–" + num(hi, 2),
        withRisk.length + " residents reporting",
        "Between-resident range of current decision risk"),
    ].join("");
  }

  function agentOptions() {
    const withId = RESIDENTS.filter((r) => r.id);
    if (!withId.length) return `<option value="">(no residents)</option>`;
    return `<option value="">select a resident…</option>` + withId.map((r) =>
      `<option value="${esc(r.id)}" ${r.id === AGENT.id ? "selected" : ""}>${esc(r.name)}${typeof r.risk === "number" ? " · " + num(r.risk, 2) : ""}</option>`).join("");
  }

  function agentPanelBody() {
    if (!AGENT.id) return `<p class="empty">Pick a resident to see its own risk history. Live reads ${'/v1/agents/{id}/history'} — thousands of real check-ins, decimated evenly across the agent's whole lifespan.</p>`;
    if (AGENT.loading) return `<p class="empty">Loading ${esc(AGENT.name || "agent")} history…</p>`;
    if (!AGENT.points.length) return `<p class="empty">No check-in history available for ${esc(AGENT.name || "this agent")}.</p>`;
    const c = actionCounts(AGENT.points);
    return `<div style="height:260px"><canvas id="risk-agent"></canvas></div>
      <p class="sub" style="margin-top:var(--space-2)">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--danger);vertical-align:middle"></span>
        marks a check-in whose recorded action was neither approve nor guide —
        the same definition <code>governance.pause.7d</code> counts by. ${c.pause
          ? c.pause + " in this window."
          : "None in this window."}
        These are verdicts the policy <em>produced</em>; whether any was
        delivered is a separate question the
        <a href="#enforcement" data-section="enforcement">Enforcement</a>
        section answers.${c.unknown
          ? ` ${c.unknown} row${c.unknown === 1 ? "" : "s"} predate the action-write and record none — shown unmarked rather than assumed clean.`
          : ""}
      </p>`;
  }

  function renderShell(source) {
    const mount = $("#risk-mount");
    if (!mount) return;
    mount.innerHTML = `
      <div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4);flex-wrap:wrap">
        <span class="eyebrow" style="margin:0">Risk history · decision risk</span>
        <span class="spring"></span>
        <select id="risk-window" class="theme-toggle" title="Trend window">
          ${WINDOWS.map((d) => `<option value="${d}" ${d === windowDays ? "selected" : ""}>${d}d</option>`).join("")}
        </select>
        <button id="risk-refresh" class="theme-toggle" title="Refresh">↻</button>
        <span class="src-badge ${esc(source)}" id="risk-src">${esc(source)}</span>
      </div>
      <div class="cards" id="risk-stats" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:var(--space-2)"></div>
      <div class="panel" style="margin-top:var(--space-4)">
        <div class="panel-head" style="margin-bottom:var(--space-3)">
          <h2>Fleet mean risk</h2><span class="spring"></span>
          <span class="fresh" id="risk-trend-meta"></span>
        </div>
        <div style="height:240px"><canvas id="risk-trend"></canvas></div>
        <p class="empty" id="risk-trend-empty" style="display:none"></p>
      </div>
      <div class="panel" style="margin-top:var(--space-4)">
        <div class="panel-head" style="margin-bottom:var(--space-3)">
          <h2>Verdict pressure</h2><span class="spring"></span>
          <span class="fresh">produced verdicts · 7d trailing</span>
        </div>
        <div style="height:240px"><canvas id="risk-pressure"></canvas></div>
        <p class="empty" id="risk-pressure-empty" style="display:none"></p>
        <p class="sub" style="max-width:74ch;margin-top:var(--space-2)">
          <strong>Produced, not delivered.</strong> Gap-suppression downgrades a
          pause to proceed at any &gt;150s inter-check-in gap — ordinary resident
          cadence — so these are verdicts the policy produced, never a count of
          interventions that landed. The
          <a href="#enforcement" data-section="enforcement">Enforcement</a>
          section carries the produced-vs-delivered meter.
        </p>
      </div>
      <div class="panel" style="margin-top:var(--space-4)">
        <div class="panel-head" style="margin-bottom:var(--space-3)">
          <h2>Per-agent risk trajectory</h2><span class="spring"></span>
          <select id="risk-agent-pick" class="theme-toggle" title="Select a resident"></select>
          <span class="fresh" id="risk-agent-meta" style="margin-left:var(--space-2)"></span>
        </div>
        <div id="risk-agent-body"></div>
      </div>
      <p class="sub" style="max-width:74ch;margin-top:var(--space-4)">
        <strong>What "risk" means here.</strong> This is <em>decision</em> risk —
        the scalar paired with the governance verdict at each check-in. It is not
        Φ-derived risk telemetry, which is expressive across regimes but is never
        recovery authority. The two are kept separately named in the
        <a href="#agents" data-section="agents">Agents</a> table too.
        <br><br>
        The charted value is also the <em>persisted</em> risk, which is taken
        before the trajectory-identity adjustment: that enrichment shifts risk by
        up to ±0.15 for trust tier and lineage anomaly, but it writes the response
        envelope an agent reads, not the state row recorded here. An agent's own
        reported risk and this series can therefore differ.
      </p>`;
    $("#risk-window").addEventListener("change", function () {
      windowDays = parseInt(this.value, 10) || 60;
      loadTrend();
    });
    $("#risk-refresh").addEventListener("click", load);
    $("#risk-agent-pick").addEventListener("change", function () { selectAgent(this.value); });
  }

  function setSrc(source) {
    const b = $("#risk-src");
    if (b) { b.className = "src-badge " + source; b.textContent = source; }
  }

  function paintTrend() {
    const pts = (TREND && TREND.risk) || [];
    const meta = $("#risk-trend-meta"), empty = $("#risk-trend-empty");
    const canvas = $("#risk-trend");
    if (meta) meta.textContent = pts.length
      ? pts.length + " scrape" + (pts.length === 1 ? "" : "s") + " · " + fmtDay(pts[0].ts) + " → " + fmtDay(pts[pts.length - 1].ts)
      : "no data";
    if (!pts.length) {
      if (canvas) canvas.style.display = "none";
      if (empty) { empty.style.display = ""; empty.textContent = "No risk scrapes in the last " + windowDays + " days. Chronicler runs daily — check back after the next cycle."; }
      if (trendChart) { trendChart.destroy(); trendChart = null; }
      return;
    }
    if (canvas) canvas.style.display = "";
    if (empty) empty.style.display = "none";
    buildTrend();
  }

  function paintPressure() {
    const pause = (TREND && TREND.pause) || [], guide = (TREND && TREND.guide) || [];
    const canvas = $("#risk-pressure"), empty = $("#risk-pressure-empty");
    if (!pause.length && !guide.length) {
      if (canvas) canvas.style.display = "none";
      if (empty) { empty.style.display = ""; empty.textContent = "No verdict-pressure scrapes in this window."; }
      if (pressureChart) { pressureChart.destroy(); pressureChart = null; }
      return;
    }
    if (canvas) canvas.style.display = "";
    if (empty) empty.style.display = "none";
    buildPressure();
  }

  function paintAgent() {
    const body = $("#risk-agent-body");
    const meta = $("#risk-agent-meta");
    if (meta) {
      if (!AGENT.points.length) meta.textContent = "";
      else {
        const c = actionCounts(AGENT.points);
        const parts = [AGENT.points.length + " of " + (AGENT.total || AGENT.points.length) + " check-ins"];
        if (c.approve) parts.push(c.approve + " approve");
        if (c.guide) parts.push(c.guide + " guide");
        if (c.pause) parts.push(c.pause + " pause produced");
        if (c.unknown) parts.push(c.unknown + " no action recorded");
        meta.textContent = parts.join(" · ");
      }
    }
    if (!body) return;
    body.innerHTML = agentPanelBody();
    if (AGENT.points.length && !AGENT.loading) buildAgent();
  }

  async function selectAgent(id) {
    if (!id) {
      AGENT = { id: null, name: null, points: [], loading: false, total: 0 };
      if (agentChart) { agentChart.destroy(); agentChart = null; }
      paintAgent();
      return;
    }
    const res = RESIDENTS.find((r) => r.id === id);
    AGENT = { id, name: res ? res.name : id, points: [], loading: true, total: 0 };
    paintAgent();
    // mode:"all" decimates evenly across the whole lifespan (every point is a
    // real check-in, not an average) so the chart reads as history rather than
    // as the last few minutes.
    const r = await window.DATA.agentHistory(id, { mode: "all", limit: 200 });
    const points = (r && r.data && r.data.points) || [];
    AGENT.loading = false;
    AGENT.points = points.filter((p) => typeof p.risk === "number");
    AGENT.total = (r && r.data && r.data.total) || AGENT.points.length;
    paintAgent();
  }

  async function loadTrend() {
    const r = await window.DATA.riskTrend(windowDays);
    TREND = r.data;
    setSrc(r.source);
    const stats = $("#risk-stats");
    if (stats) stats.innerHTML = statCards();
    paintTrend();
    paintPressure();
  }

  async function load() {
    const mount = $("#risk-mount");
    if (!mount) return;
    const res = await window.DATA.residents();
    RESIDENTS = (res && res.data) || [];
    if (!mounted) { renderShell(res.source); mounted = true; }
    const pick = $("#risk-agent-pick");
    // Repopulate without clobbering an operator's current selection.
    if (pick) { const cur = AGENT.id || ""; pick.innerHTML = agentOptions(); pick.value = cur; }
    await loadTrend();
    if (AGENT.id) paintAgent();
  }

  // Theme toggle: token values changed, so rebuild every chart from cache.
  function retheme() {
    if (!mounted || !window.Chart) return;
    paintTrend();
    paintPressure();
    if (AGENT.points.length) buildAgent();
  }

  window.Risk = { load, retheme };
})();
