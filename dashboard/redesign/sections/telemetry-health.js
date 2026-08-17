/*
 * EISV telemetry health — durable rollout and evidence audit.
 *
 * This view never scores an agent.  It asks whether the instrumentation is
 * present, source-labelled, internally consistent, outcome-linkable, and
 * honestly separated from policy/enforcement.  DATA owns the live-or-snapshot
 * seam; this module only renders and updates in place.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  let MODEL = null;
  let SOURCE = "snapshot";
  let DAYS = 30;
  let coverageChart = null;
  let calibrationChart = null;

  function pct(value, digits) {
    return value == null || !Number.isFinite(Number(value))
      ? "—" : `${(Number(value) * 100).toFixed(digits == null ? 1 : digits)}%`;
  }

  function num(value) {
    return value == null || !Number.isFinite(Number(value))
      ? "—" : Number(value).toLocaleString();
  }

  function dateLabel(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toISOString().slice(0, 10);
  }

  function rgba(hex, alpha) {
    const h = (hex || "").replace("#", "");
    if (h.length !== 6) return hex;
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }

  function chartOptions() {
    const tick = cssVar("--muted") || "#888";
    const grid = rgba(cssVar("--ink") || "#888888", 0.06);
    const surface = cssVar("--surface") || "#222";
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 200 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: surface,
          titleColor: cssVar("--ink") || tick,
          bodyColor: tick,
          borderColor: cssVar("--line-2") || tick,
          borderWidth: 1,
        },
      },
      scales: {
        x: { grid: { color: grid }, ticks: { color: tick, maxRotation: 0, autoSkipPadding: 14 } },
        y: {
          min: 0, max: 100,
          grid: { color: grid },
          ticks: { color: tick, callback: (value) => `${value}%` },
        },
      },
    };
  }

  function card(label, value, sub) {
    return `<div class="card"><h3>${esc(label)}</h3><div class="num">${value}</div><div class="sub">${sub}</div></div>`;
  }

  function rateRows(rows, labelKey) {
    if (!rows || !rows.length) return '<p class="empty">No envelope-covered observations yet.</p>';
    const total = rows.reduce((sum, row) => sum + Number(row.observations || 0), 0);
    return rows.map((row) => {
      const rate = row.rate == null && total ? Number(row.observations || 0) / total : row.rate;
      return `<div class="th-row">
        <span class="th-label" title="${esc(row[labelKey])}">${esc(row[labelKey])}</span>
        <span class="th-track"><i style="width:${Math.max(0, Math.min(100, Number(rate || 0) * 100))}%"></i></span>
        <span class="th-count">${num(row.observations)} · ${pct(rate)}</span>
      </div>`;
    }).join("");
  }

  function violationsHTML(checks) {
    const rows = (checks && checks.by_type) || [];
    if (!rows.length) {
      return `<p class="empty">No same-row contract violations in ${num(checks && checks.checked_rows)} envelope(s).</p>`;
    }
    return rows.map((row) => `<div class="th-detail-row"><code>${esc(row.type)}</code><strong>${num(row.observations)}</strong></div>`).join("");
  }

  function vocabularyHTML(vocabularies) {
    return (vocabularies || []).map((item) => {
      const bands = (item.bands || []).map((band) => {
        const upper = band.maximum_exclusive == null ? "1.00" : Number(band.maximum_exclusive).toFixed(2);
        return `<span class="chip">${esc(band.label)} ${Number(band.minimum).toFixed(2)}–${upper}</span>`;
      }).join(" ");
      return `<div class="th-vocab"><code>${esc(item.surface)}</code><div>${bands}</div>${item.note ? `<small>${esc(item.note)}</small>` : ""}</div>`;
    }).join("");
  }

  function calibrationTable(calibration) {
    const bins = (calibration && calibration.bins) || [];
    return `<div class="th-table-wrap"><div class="th-cal-head"><span>prior risk</span><span>outcomes</span><span>clusters</span><span>bad clusters</span><span>rate</span><span>evidence</span></div>
      ${bins.map((row) => `<div class="th-cal-row"><code>${esc(row.band)}</code><span>${num(row.outcomes)}</span><span>${num(row.clusters)}</span><span>${num(row.bad_clusters)}</span><strong>${pct(row.bad_cluster_rate)}</strong><span class="chip">${esc(row.evidence_status)}</span></div>`).join("")}</div>`;
  }

  function renderShell() {
    const mount = $("telemetry-health-mount");
    if (!mount) return;
    mount.innerHTML = `
      <div class="toolbar th-toolbar">
        <div><div class="eyebrow">Instrumentation, not judgment</div><h2 style="margin:2px 0 0">EISV telemetry health</h2></div>
        <span class="spring"></span>
        <label class="sub">window
          <select id="telemetry-health-days"><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option></select>
        </label>
        <span id="telemetry-health-source" class="src-badge"></span>
      </div>
      <p class="sub" style="max-width:78ch">Coverage, provenance, and evidence plumbing for the append-only envelope. These are fleet instrumentation metrics; they do not score agents or establish machine experience.</p>
      <div id="telemetry-health-rollout" class="attn-band" style="margin:var(--space-4) 0"></div>
      <div id="telemetry-health-cards" class="cards th-cards"></div>
      <div class="split-wide th-split">
        <section class="panel"><div class="panel-head"><h2>Envelope coverage</h2><span class="fresh" id="telemetry-health-window"></span></div><div class="th-chart"><canvas id="telemetry-health-coverage-chart"></canvas></div></section>
        <section class="panel"><div class="panel-head"><h2>Strict outcome calibration</h2><span class="fresh" id="telemetry-health-cal-status"></span></div><div class="th-chart"><canvas id="telemetry-health-calibration-chart"></canvas></div></section>
      </div>
      <div class="split-wide th-split">
        <section class="panel"><div class="panel-head"><h2>Primary state source</h2><span class="fresh">behavioral vs fallback</span></div><div id="telemetry-health-primary"></div><div class="panel-head" style="margin-top:var(--space-4)"><h2>Consumed instrument</h2></div><div id="telemetry-health-measurement"></div></section>
        <section class="panel"><div class="panel-head"><h2>Warmup & missing inputs</h2></div><div id="telemetry-health-warmup"></div><div class="panel-head" style="margin-top:var(--space-4)"><h2>Most frequent missing inputs</h2></div><div id="telemetry-health-missing"></div></section>
      </div>
      <section class="panel"><div class="panel-head"><h2>Outcome-linked evidence</h2><span class="fresh">strict external · 5m lead · prior-measurement clusters</span></div><div id="telemetry-health-calibration-table"></div><p id="telemetry-health-calibration-note" class="sub"></p></section>
      <div class="split-wide th-split">
        <section class="panel"><div class="panel-head"><h2>Cross-field contract checks</h2><span class="fresh">same-row invariants only</span></div><div id="telemetry-health-contracts"></div><p id="telemetry-health-contract-note" class="sub"></p></section>
        <section class="panel"><div class="panel-head"><h2>Cold-start decision maturity</h2><span class="fresh">shadow only · no pause suppression</span></div><div id="telemetry-health-maturity"></div><div class="panel-head" style="margin-top:var(--space-4)"><h2>Why rows were ineligible</h2></div><div id="telemetry-health-maturity-ineligible"></div><div class="panel-head" style="margin-top:var(--space-4)"><h2>Confirmation reset reasons</h2></div><div id="telemetry-health-maturity-resets"></div><p id="telemetry-health-maturity-note" class="sub"></p></section>
      </div>
      <section class="panel"><div class="panel-head"><h2>Policy → enforcement delivery</h2><span class="fresh">intervention-conditioned</span></div><div class="split-wide th-split"><div><div id="telemetry-health-enforcement"></div></div><div><div class="panel-head"><h2>Recorded enforcement basis</h2></div><div id="telemetry-health-enforcement-basis"></div></div></div><p id="telemetry-health-enforcement-note" class="sub"></p></section>
      <section class="panel"><div class="panel-head"><h2>One risk number, three vocabularies</h2><span class="fresh">visible threshold contracts</span></div><div id="telemetry-health-vocabularies"></div><p class="sub">Different labels are not counted as contradictions merely because their thresholds overlap. The contract checker above flags only fields that disagree inside the same persisted observation.</p></section>
      <style>
        .th-toolbar{display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-2)}
        .th-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--space-2);margin-bottom:var(--space-4)}
        .th-split{gap:var(--space-3);margin-bottom:var(--space-3);align-items:stretch}
        .th-chart{height:220px}
        .th-row{display:grid;grid-template-columns:minmax(150px,1fr) minmax(100px,2fr) minmax(96px,auto);gap:var(--space-2);align-items:center;padding:6px 0;border-top:1px solid var(--line-2)}
        .th-label,.th-count{font-family:var(--font-mono);font-size:var(--text-sm)}
        .th-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .th-count{text-align:right;color:var(--muted)}
        .th-track{height:6px;background:var(--bg-sunken);border-radius:var(--radius-pill);overflow:hidden}.th-track i{display:block;height:100%;background:var(--eisv-c);border-radius:inherit}
        .th-detail-row{display:flex;justify-content:space-between;gap:var(--space-2);padding:7px 0;border-top:1px solid var(--line-2)}
        .th-vocab{display:grid;grid-template-columns:minmax(150px,.7fr) 2fr;gap:var(--space-2);align-items:center;padding:8px 0;border-top:1px solid var(--line-2)}.th-vocab small{grid-column:2;color:var(--muted)}
        .th-table-wrap{overflow-x:auto}.th-cal-head,.th-cal-row{display:grid;grid-template-columns:minmax(90px,1fr) repeat(5,minmax(80px,1fr));gap:var(--space-2);align-items:center;min-width:680px;padding:7px 0;border-top:1px solid var(--line-2)}
        .th-cal-head{font-size:var(--text-xs);color:var(--muted);text-transform:uppercase;letter-spacing:var(--tracking-label)}
        @media(max-width:760px){.th-toolbar{align-items:flex-start;flex-wrap:wrap}.th-vocab{grid-template-columns:1fr}.th-vocab small{grid-column:1}}
      </style>`;

    $("telemetry-health-days").addEventListener("change", (event) => {
      DAYS = Number(event.target.value) || 30;
      load();
    });
  }

  function paintCharts(rebuild) {
    if (!MODEL || typeof Chart === "undefined") return;
    const timeline = MODEL.timeline || [];
    const labels = timeline.map((row) => String(row.day || "").slice(5));
    const coverage = timeline.map((row) => row.coverage_rate == null ? null : Number(row.coverage_rate) * 100);
    const calibration = (MODEL.calibration && MODEL.calibration.bins) || [];
    const calLabels = calibration.map((row) => row.band);
    const badRates = calibration.map((row) => row.bad_cluster_rate == null ? null : Number(row.bad_cluster_rate) * 100);
    const coverageColor = cssVar("--eisv-c") || "#3f7d93";
    const calibrationColor = cssVar("--eisv-s") || "#b07d2b";

    if (rebuild && coverageChart) { coverageChart.destroy(); coverageChart = null; }
    if (rebuild && calibrationChart) { calibrationChart.destroy(); calibrationChart = null; }
    if (!coverageChart) {
      coverageChart = new Chart($("telemetry-health-coverage-chart"), {
        type: "line",
        data: { labels, datasets: [{ label: "Envelope coverage", data: coverage, borderColor: coverageColor, backgroundColor: rgba(coverageColor, 0.12), borderWidth: 2, pointRadius: 2, tension: 0.25, fill: true }] },
        options: chartOptions(),
      });
    } else {
      coverageChart.data.labels = labels;
      coverageChart.data.datasets[0].data = coverage;
      coverageChart.update("none");
    }
    if (!calibrationChart) {
      calibrationChart = new Chart($("telemetry-health-calibration-chart"), {
        type: "bar",
        data: { labels: calLabels, datasets: [{ label: "Bad cluster rate", data: badRates, backgroundColor: rgba(calibrationColor, 0.55), borderColor: calibrationColor, borderWidth: 1 }] },
        options: chartOptions(),
      });
    } else {
      calibrationChart.data.labels = calLabels;
      calibrationChart.data.datasets[0].data = badRates;
      calibrationChart.update("none");
    }
  }

  function updateInPlace() {
    if (!MODEL) return;
    const summary = MODEL.summary || {};
    const calibration = MODEL.calibration || {};
    const contracts = MODEL.contract_checks || {};
    const maturity = MODEL.maturity_gate || {};
    const enforcement = MODEL.enforcement || {};
    const sourceBadge = $("telemetry-health-source");
    sourceBadge.className = `src-badge ${SOURCE}`;
    sourceBadge.textContent = SOURCE;
    $("telemetry-health-days").value = String(DAYS);
    $("telemetry-health-window").textContent = `${MODEL.window_days || DAYS}d · first ${dateLabel(summary.first_envelope_at)}`;
    $("telemetry-health-cal-status").textContent = calibration.status || "awaiting data";

    const rollout = $("telemetry-health-rollout");
    const invalid = Number(summary.invalid_envelopes || 0);
    if (!summary.envelopes) {
      rollout.textContent = `Awaiting envelope rollout: 0 of ${num(summary.states)} measured state rows carry ${MODEL.schema ? "the recognized telemetry envelope" : "recognized telemetry"}. ${invalid ? `${num(invalid)} malformed or unsupported envelope row(s) are visible.` : ""} No backfill is inferred.`;
    } else {
      rollout.textContent = `${num(summary.envelopes)} of ${num(summary.states)} measured state rows are envelope-covered (${pct(summary.coverage_rate)}); ${num(summary.envelope_agents)} of ${num(summary.agents)} agents contributed coverage. ${num(invalid)} malformed or unsupported envelope row(s) remain outside coverage.`;
    }

    $("telemetry-health-cards").innerHTML = [
      card("Envelope coverage", pct(summary.coverage_rate), `${num(summary.envelopes)} / ${num(summary.states)} state rows`),
      card("Behavioral primary", pct(summary.behavioral_primary_rate), `${num(summary.behavioral_primary)} covered observations`),
      card("ODE fallback", pct(summary.ode_fallback_rate), `${num(summary.ode_fallback)} covered observations`),
      card("Measurement ready", pct(summary.measurement_ready_rate), `${num(summary.measurement_ready)} behavior-authoritative observations`),
      card("Shadow would defer", num(summary.maturity_would_defer), `${pct(summary.maturity_would_defer_rate)} counterfactual only`),
      card("Missing inputs", pct(summary.missing_rate), `${num(summary.missing)} covered observations`),
      card("Invalid envelopes", num(summary.invalid_envelopes), `${pct(summary.invalid_envelope_rate)} of envelope-bearing rows`),
      card("Contract violations", num(summary.contract_violation_rows), `${pct(summary.contract_violation_rate)} of ${num(summary.contract_checked_rows)} checked rows`),
      card("Outcome clusters", num(calibration.clusters), `${num(calibration.bad_clusters)} bad · ${esc(calibration.status || "unknown")}`),
      card("Actuator delivery", `${num(summary.enforcement_delivered)} / ${num(summary.enforcement_requested)}`, `${pct(summary.enforcement_delivery_rate)} of requests applied; not causal effect`),
    ].join("");

    $("telemetry-health-primary").innerHTML = rateRows(MODEL.primary_sources, "source");
    $("telemetry-health-measurement").innerHTML = rateRows(MODEL.measurement_sources, "source");
    $("telemetry-health-warmup").innerHTML = rateRows(MODEL.warmup, "phase");
    $("telemetry-health-missing").innerHTML = rateRows(MODEL.missing_inputs, "input");
    $("telemetry-health-contracts").innerHTML = violationsHTML(contracts);
    $("telemetry-health-contract-note").textContent = contracts.note || "";
    $("telemetry-health-maturity").innerHTML = rateRows(maturity.strata, "outcome");
    $("telemetry-health-maturity-ineligible").innerHTML = rateRows(maturity.ineligibility_reasons, "reason");
    $("telemetry-health-maturity-resets").innerHTML = rateRows(maturity.reset_reasons, "reason");
    $("telemetry-health-maturity-note").textContent = maturity.note || "";
    $("telemetry-health-enforcement").innerHTML = rateRows(enforcement.strata, "stratum");
    $("telemetry-health-enforcement-basis").innerHTML = rateRows(enforcement.bases, "basis");
    $("telemetry-health-enforcement-note").textContent = enforcement.note || "";
    $("telemetry-health-vocabularies").innerHTML = vocabularyHTML(MODEL.risk_vocabularies);
    $("telemetry-health-calibration-table").innerHTML = calibrationTable(calibration);
    $("telemetry-health-calibration-note").textContent = `${calibration.note || ""} ${num(calibration.fixtures_excluded)} controlled fixture(s) excluded; ${num(calibration.with_envelope)} of ${num(calibration.strict_outcomes)} strict outcomes have a lead-separated envelope.`;
    paintCharts(false);
  }

  async function load() {
    const mount = $("telemetry-health-mount");
    if (!mount) return;
    const result = await window.DATA.eisvTelemetryHealth(DAYS);
    if (!result || !result.data) {
      mount.innerHTML = '<p class="empty">Telemetry health is unavailable.</p>';
      return;
    }
    SOURCE = result.source;
    MODEL = result.data;
    if (SOURCE === "snapshot" && Number.isFinite(Number(MODEL.window_days))) DAYS = Number(MODEL.window_days);
    if (!$("telemetry-health-days")) renderShell();
    updateInPlace();
  }

  function retheme() {
    paintCharts(true);
  }

  window.TelemetryHealth = { load, retheme };
})();
