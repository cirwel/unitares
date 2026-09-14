/*
 * EISV section — fleet trajectory charts (Chart.js) + per-resident heatmap.
 * Built from eisv-charts.js oracle, distilled to two line charts:
 *   upper = E, I, coherence (+ coherence equilibrium line)
 *   lower = S, V (+ zero line)
 * Plus a Fleet heatmap (revived from the classic dashboard): a residents ×
 * {E,I,S,V,coherence} grid so an outlier resident pops out instead of being
 * averaged into the blended fleet line. Reads DATA.eisv() + DATA.residents().
 * Upgrade over the oracle: source lanes/filtering keep instruments distinct,
 * and every chart/table colour is read from design tokens. Fleet readings use
 * a neutral surface rather than converting observations into red/green verdicts.
 */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let MODEL = { series: [], coherenceEq: 0.5, source: "snapshot", sourceLanes: [] };
  let SOURCE_FILTER = "all";
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

  function fmtValue(value) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
  }

  function observationAge(timestamp) {
    const time = timestamp && Date.parse(timestamp);
    if (!Number.isFinite(time)) return "observation time unrecorded";
    const seconds = Math.floor((Date.now() - time) / 1000);
    if (seconds < 0) return "observation time ahead of browser clock";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  function sourceBadge(source) {
    const label = source === "live" || source === "snapshot" ? source : "unavailable";
    return `<span class="src-badge ${label}">${label}</span>`;
  }

  function sourceOptions(lanes) {
    const mixed = (lanes || []).length > 1 ? "all sources · mixed" : "all sources";
    return [`<option value="all">${mixed}</option>`].concat(
      (lanes || []).map((lane) => `<option value="${esc(lane.source)}">${esc(lane.source)} · ${lane.events}</option>`),
    ).join("");
  }

  function sourceLanesHTML(lanes) {
    if (!lanes || !lanes.length) {
      return `<div class="panel" style="margin-bottom:var(--space-5)">
        <div class="panel-head"><h2>Measurement lanes</h2></div>
        <p class="empty">No source envelope in this window.</p></div>`;
    }
    const grid = "minmax(210px,2fr) repeat(6,minmax(62px,1fr)) minmax(90px,1fr) minmax(115px,1fr)";
    const rowStyle = `display:grid;grid-template-columns:${grid};gap:6px;align-items:center;min-width:960px`;
    const header = ["source / input coverage", "events", "E", "I", "S", "V", "confidence", "actuator req/app", "latest observation"]
      .map((label) => `<div style="font-size:var(--text-xs);color:var(--muted);text-transform:uppercase;letter-spacing:var(--tracking-label)">${label}</div>`).join("");
    const rows = lanes.map((lane) => {
      const unknown = lane.unknownProvenance ?? lane.events;
      const missing = [
        lane.missingObservations ? `${lane.missingObservations} observation(s) with missing inputs: ${(lane.missingInputs || []).join(", ")}` : "",
        unknown ? `${unknown} observation(s): input coverage unrecorded` : "",
        !unknown && !lane.missingObservations ? "No missing inputs reported" : "",
      ].filter(Boolean).join(" · ");
      return `<div style="${rowStyle};padding:7px 0;border-top:1px solid var(--line-2)" title="${esc(missing)}">
        <div style="font-family:var(--font-mono);font-size:var(--text-sm);color:var(--ink-2)">${esc(lane.source)}<div class="sub">${esc(missing)}</div></div>
        <div>${lane.events}</div><div>${fmtValue(lane.E)}</div><div>${fmtValue(lane.I)}</div>
        <div>${fmtValue(lane.S)}</div><div>${fmtValue(lane.V)}</div><div>${fmtValue(lane.confidence)}</div>
        <div>${lane.enforcementRequested || 0} / ${lane.enforcementApplied || 0}</div>
        <div title="${esc(lane.latest || "No observation timestamp recorded")}">${observationAge(lane.latest)}</div></div>`;
    }).join("");
    return `<div class="panel" style="margin-bottom:var(--space-5)">
      <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Measurement lanes</h2>
        <span class="spring"></span><span class="fresh">event-weighted · sources never averaged together here</span></div>
      <div style="overflow-x:auto;font-family:var(--font-mono);font-size:var(--text-sm)">
        <div style="${rowStyle};padding-bottom:6px">${header}</div>${rows}</div></div>`;
  }

  // Fleet readings — residents × {E,I,S,V,coherence}. Values use a neutral
  // surface rather than a red/green health transform: an observation is not a
  // verdict, and each dimension's interpretation belongs in the policy layer.
  // Lives in its own #eisv-heatmap container so a periodic refresh can swap it
  // without tearing down the Chart.js canvases beside it.
  function heatmapHTML(residents) {
    const rows = (residents || []).filter((r) => r && r.eisv && r.eisv.E != null);
    if (!rows.length) return "";
    const fmt = (x) => (x == null ? "—" : Number(x).toFixed(2));
    const cols = [
      { label: "E", val: (r) => r.eisv.E },
      { label: "I", val: (r) => r.eisv.I },
      { label: "S", val: (r) => r.eisv.S },
      { label: "V", val: (r) => r.eisv.V },
      { label: "Coh", val: (r) => r.coherence },
    ];
    const cell = (value, title) => `<div title="${esc(title)}" style="background:var(--surface-2,var(--surface));border:1px solid var(--line-2);color:var(--ink);font-family:var(--font-mono);font-size:var(--text-sm);text-align:center;padding:6px 0;border-radius:var(--radius-1)">${value}</div>`;
    const headLbl = (t) => `<div style="text-align:center;font-size:var(--text-xs);color:var(--muted);text-transform:uppercase;letter-spacing:var(--tracking-label)">${t}</div>`;
    // Each resident is its own grid row (shared column template) so the whole
    // row is a click target for the per-agent trajectory below. Rows without an
    // agent id (e.g. snapshot-only) stay inert.
    const gridCols = `minmax(72px,1.4fr) repeat(${cols.length}, 1fr)`;
    const rowGrid = `display:grid;grid-template-columns:${gridCols};gap:4px;align-items:stretch`;
    const header = `<div style="${rowGrid};padding:0 1px"><div></div>${cols.map((c) => headLbl(c.label)).join("")}</div>`;
    const body = rows.map((r) => {
      const name = `<div style="font-size:var(--text-sm);color:var(--ink-2);display:flex;align-items:center;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(r.name)}</div>`;
      const cells = cols.map((c) => cell(fmt(c.val(r)), `${r.name} ${c.label} = ${fmt(c.val(r))}`)).join("");
      const clickable = !!r.id;
      const attrs = clickable
        ? ` data-traj-id="${esc(r.id)}" data-traj-name="${esc(r.name)}" title="Click for ${esc(r.name)}'s EISV trajectory"` : "";
      return `<div${attrs} style="${rowGrid};border-radius:var(--radius-1);padding:1px${clickable ? ";cursor:pointer" : ""}">${name}${cells}</div>`;
    }).join("");
    return `<div class="panel" style="margin-bottom:var(--space-5)">
        <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Fleet readings</h2>
          <span class="spring"></span><span class="fresh">raw values · neutral display · click a row</span></div>
        <div style="display:flex;flex-direction:column;gap:4px">${header}${body}</div></div>`;
  }

  // ---- Per-agent EISV trajectory (drill-down from the heatmap) -------------
  let trajUpper = null, trajLower = null, selectedId = null, selectedName = null;
  let trajPoints = [], trajLoading = false, clickBound = false, controlsBound = false;
  let trajSource = null, trajContext = null;
  let inspector = null;
  let selectionVersion = 0;

  function fmtT(t) {
    const d = new Date(t);
    return isNaN(d) ? "" : (d.getMonth() + 1) + "/" + d.getDate();
  }

  function buildTrajectory() {
    if (trajUpper) { trajUpper.destroy(); trajUpper = null; }
    if (trajLower) { trajLower.destroy(); trajLower = null; }
    const upperCanvas = $("#eisv-traj-upper"), lowerCanvas = $("#eisv-traj-lower");
    if (!upperCanvas || !lowerCanvas || !window.Chart || !trajPoints.length) return;
    const E = cssVar("--eisv-e"), I = cssVar("--eisv-i"), S = cssVar("--eisv-s"), V = cssVar("--eisv-v");
    const C = cssVar("--eisv-c"), muted = cssVar("--muted");
    const labels = trajPoints.map((p) => fmtT(p.t));
    trajUpper = new Chart(upperCanvas, {
      type: "line",
      data: { labels, datasets: [
        ds("Energy", trajPoints.map((p) => p.E), E),
        ds("Integrity", trajPoints.map((p) => p.I), I),
        ds("Coherence", trajPoints.map((p) => p.coherence), C, { fill: false, borderDash: [5, 4], borderWidth: 1.5 }),
      ] },
      options: baseOptions({ min: 0, max: 1 }),
      plugins: [refLine(MODEL.coherenceEq, muted)],
    });
    trajLower = new Chart(lowerCanvas, {
      type: "line",
      data: { labels, datasets: [
        ds("Entropy", trajPoints.map((p) => p.S), S),
        ds("Valence", trajPoints.map((p) => p.V), V),
      ] },
      options: baseOptions({ min: -1, max: 1 }),
      plugins: [refLine(0, muted)],
    });
  }

  function trajectorySources() {
    const counts = {};
    trajPoints.forEach((point) => {
      const source = DATA.eisvMeasurementSource(point);
      counts[source] = (counts[source] || 0) + 1;
    });
    return Object.keys(counts).sort().map((source) => `${source} ${counts[source]}`).join(" · ");
  }

  function componentsHTML(derivation) {
    const components = derivation.components;
    const dimensions = components && components.dimensions;
    if (!dimensions || typeof dimensions !== "object") {
      return '<p class="sub">Component contributions were not recorded in this envelope.</p>';
    }
    const observed = (value) => value === true ? "observed" : value === false ? "defaulted / unobserved" : "unrecorded";
    const row = (cells) => `<tr>${cells.map((cell) => `<td style="padding:6px;border-top:1px solid var(--line-2)">${cell}</td>`).join("")}</tr>`;
    const rows = ["E", "I", "S", "V"].map((dimension) => {
      const detail = dimensions[dimension];
      if (!detail || typeof detail !== "object") return "";
      const terms = Array.isArray(detail.components) ? detail.components.slice(0, 16) : [];
      const adjustments = Array.isArray(detail.adjustments) ? detail.adjustments.slice(0, 8) : [];
      const role = detail.measurement_role || "role unrecorded";
      return `<tr><th colspan="7" style="text-align:left;padding:10px 6px 4px">${dimension} · base ${fmtValue(detail.base_value)} · submitted output ${fmtValue(detail.value)} · ${esc(role)}</th></tr>` +
        terms.filter((term) => term && typeof term === "object").map((term) => row([
          esc(term.name), esc(term.source || "unrecorded"), fmtValue(term.value),
          fmtValue(term.weight), fmtValue(term.weighted_contribution),
          observed(term.observed), esc(term.default_reason || "—"),
        ])).join("") + adjustments.filter((term) => term && typeof term === "object").map((term) => row([
          `${esc(term.name)} (adjustment)`, esc(term.source || "unrecorded"), fmtValue(term.input_value),
          fmtValue(term.input_weight), `output ${fmtValue(term.output_value)}`,
          observed(term.observed), `retained weight ${fmtValue(term.retained_weight)}`,
        ])).join("");
    }).join("");
    return `<h3>Recorded component contributions</h3><p class="sub">${esc(components.schema || "version unrecorded")} · submitted sensor outputs before BehavioralState input clamping and EMA; adjustments are applied in recorded order. E/I/S are consumed as behavioral inputs. Sensor V is diagnostic and does not produce behavioral V, which is separately derived as an EMA of raw E−I. Input freshness is unrecorded.</p>
      <div style="overflow-x:auto"><table style="width:100%;min-width:760px;font-family:var(--font-mono);font-size:var(--text-sm);border-collapse:collapse;text-align:left">
        <thead><tr>${["component", "source", "value", "weight", "contribution / output", "input", "detail"].map((label) => `<th style="padding:6px">${label}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function calibrationHTML(derivation) {
    const signal = derivation.calibration_signal;
    if (!signal || typeof signal !== "object" || !Object.keys(signal).length) {
      return '<p class="sub">Calibration scope, freshness, and coverage were not recorded in this envelope.</p>';
    }
    const deployed = signal.deployed || {};
    const candidate = signal.agent_candidate || {};
    const cell = (value) => esc(value == null ? "unrecorded" : String(value));
    const row = (label, value) => `<tr><th style="padding:5px;text-align:left">${label}</th><td style="padding:5px">${value}</td></tr>`;
    return `<h3>Calibration evidence</h3><p class="sub">${cell(signal.schema)} · ${signal.policy_applied === false ? "measurement only; agent candidate is not applied to policy" : "policy status unrecorded"}</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:var(--space-3)">
        <table style="font-family:var(--font-mono);font-size:var(--text-sm)"><caption style="text-align:left">deployed · ${cell(deployed.scope)}</caption><tbody>
          ${row("error", fmtValue(deployed.calibration_error))}${row("samples", cell(deployed.sample_count))}${row("eligible samples", cell(deployed.eligible_sample_count))}${row("eligible bins", cell(deployed.eligible_bin_count))}${row("freshness", cell(deployed.freshness_status || deployed.freshness_reason))}
        </tbody></table>
        <table style="font-family:var(--font-mono);font-size:var(--text-sm)"><caption style="text-align:left">candidate · ${cell(candidate.scope)}</caption><tbody>
          ${row("status", cell(candidate.evidence_status))}${row("error", fmtValue(candidate.calibration_error))}${row("samples", cell(candidate.sample_count))}${row("sample window", cell(candidate.sample_window))}${row("eligible samples", cell(candidate.eligible_sample_count))}${row("eligible bins", cell(candidate.eligible_bin_count))}${row("freshness", cell(candidate.freshness_status))}${row("freshness rule", cell(candidate.freshness_rule))}${row("age days", fmtValue(candidate.age_days))}${row("freshness note", cell(candidate.freshness_note))}
        </tbody></table>
      </div>`;
  }

  function compactDuration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "duration unrecorded";
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${(seconds / 3600).toFixed(seconds % 3600 ? 1 : 0)}h`;
    return `${(seconds / 86400).toFixed(seconds % 86400 ? 1 : 0)}d`;
  }

  function trajectoryContextHTML() {
    if (!trajContext || typeof trajContext !== "object") {
      return '<p class="sub">Clock-time trajectory context is unrecorded for this source.</p>';
    }
    const cadence = trajContext.cadence || {};
    const transitions = trajContext.transitions || {};
    const slopes = trajContext.slopes_per_hour || {};
    const slopeText = ["E", "I", "S", "V"].map((key) => `${key} ${fmtValue(slopes[key])}`).join(" · ");
    return `<p class="sub">Requested scope: ${cellText(trajContext.observations)} observations over ${compactDuration(trajContext.elapsed_seconds)}; ${cellText(trajContext.points_returned)} plotted. Median cadence ${compactDuration(cadence.median_seconds)}; max gap ${compactDuration(cadence.max_seconds)}. Source transitions ${cellText(transitions.measurement_source)}, maturity transitions ${cellText(transitions.maturity_phase)}. Descriptive slopes/hour: ${slopeText}. These describe recorded measurements, not outcomes.</p>`;
  }

  function cellText(value) {
    return esc(value == null ? "unrecorded" : String(value));
  }

  function renderInspector() {
    const mount = $("#eisv-observation-inspector");
    const button = $("#eisv-inspect-latest");
    if (!mount || !button) return;
    button.disabled = !!(inspector && inspector.loading);
    button.setAttribute("aria-expanded", String(!!inspector));
    button.textContent = inspector ? "Refresh latest observation" : "Inspect latest observation";
    if (!inspector) { mount.innerHTML = ""; return; }
    if (inspector.loading) { mount.innerHTML = '<p class="sub">Loading the latest observation…</p>'; return; }
    const point = inspector.point;
    const head = `<div class="panel-head"><h3>Latest observation · inputs and derivation</h3><span class="spring"></span>${sourceBadge(inspector.source)}</div>`;
    if (!point) {
      mount.innerHTML = head + '<p class="empty">No latest observation is available from this source.</p>';
      return;
    }
    const envelope = point.telemetry_envelope;
    const time = (envelope && envelope.observed_at) || point.t;
    const age = `<p class="sub" title="${esc(time || "unrecorded")}">Observation recorded ${esc(time || "at an unrecorded time")} · ${observationAge(time)}. Input timestamps are unrecorded; observation age does not establish input freshness.</p>`;
    if (!envelope || typeof envelope !== "object") {
      mount.innerHTML = head + age + '<p class="sub">No derivation envelope is available for this latest state observation. An earlier envelope has not been substituted.</p>';
      return;
    }
    const measurement = envelope.measurement || {};
    const primary = measurement.primary || {};
    const behavioral = measurement.behavioral || {};
    const raw = behavioral.raw_observation || {};
    const smoothed = behavioral.smoothed || {};
    const derivation = envelope.derivation || {};
    const maturity = behavioral.warmup || {};
    const missing = Array.isArray(derivation.missing_inputs)
      ? (derivation.missing_inputs.length ? `Missing inputs: ${derivation.missing_inputs.join(", ")}` : "No missing inputs reported")
      : "Input coverage unrecorded";
    const rows = ["E", "I", "S", "V"].map((dimension) => `<tr>
      <th style="padding:6px;text-align:left">${dimension}</th><td>${fmtValue((primary.values || {})[dimension])}</td>
      <td>${fmtValue(raw[dimension])}</td><td>${fmtValue(smoothed[dimension])}</td></tr>`).join("");
    const distinction = primary.source === "behavioral"
      ? "Primary values use the behavioral estimate. Raw E/I/S are clamped inputs before smoothing; behavioral V is an EMA of their raw E−I imbalance."
      : "Primary values use a separate instrument; the latent behavioral observations below do not explain the primary values.";
    const features = derivation.inputs && derivation.inputs.features;
    const featureRows = features && typeof features === "object" ? Object.entries(features).slice(0, 20).map(([name, value]) =>
      `<span><code>${esc(name)}</code> ${fmtValue(value)}</span>`).join(" · ") : "";
    mount.innerHTML = head + age +
      `<p class="sub">Primary source: <code>${esc(primary.source || "unknown")}</code> · behavioral input: <code>${esc(behavioral.observation_source || "unrecorded")}</code> · maturity: ${esc(maturity.phase || "unrecorded")}. ${distinction}</p>
       <p class="sub">${esc(missing)}. Observation count: ${fmtValue(behavioral.updates)}. V has no independent raw behavioral input; formula version: ${esc(behavioral.v_formula_version ?? "unrecorded")}.</p>
       <table style="width:100%;max-width:640px;font-family:var(--font-mono);font-size:var(--text-sm);text-align:left"><thead><tr><th>dimension</th><th>primary</th><th>raw behavioral</th><th>smoothed behavioral</th></tr></thead><tbody>${rows}</tbody></table>
       ${componentsHTML(derivation)}
       ${calibrationHTML(derivation)}
       ${featureRows ? `<details><summary>Recorded input features</summary><p class="sub">${featureRows}</p></details>` : ""}`;
  }

  async function inspectLatest() {
    if (!selectedId || (inspector && inspector.loading)) return;
    const version = selectionVersion;
    const id = selectedId;
    inspector = { loading: true };
    renderInspector();
    let result;
    try {
      result = await DATA.agentHistory(id, { mode: "recent", limit: 1, includeTelemetry: "latest" });
    } catch {
      result = null;
    }
    if (selectionVersion !== version) return;
    const points = (result && result.data && result.data.points) || [];
    // The history API orders by (recorded_at, state_id), so the final row is
    // newest even when multiple observations share a timestamp. Selecting by
    // timestamp alone could choose the older equal-time row whose envelope was
    // intentionally omitted by include_telemetry=latest.
    const latest = points.length ? points[points.length - 1] : null;
    inspector = { loading: false, point: latest, source: result && result.source };
    renderInspector();
  }

  function renderTrajectory() {
    const mount = $("#eisv-trajectory");
    if (!mount) return;
    const headHTML = (sub) => `<div class="panel-head" style="margin-bottom:var(--space-3)">
        <h2>${selectedName ? esc(selectedName) + " · trajectory" : "Agent trajectory"}</h2>
        <span class="spring"></span><span class="fresh">${sub}</span>${trajSource ? sourceBadge(trajSource) : ""}</div>`;
    const note = (txt) => `<p style="color:var(--muted);font-size:var(--text-sm);margin:0">${txt}</p>`;
    let inner;
    if (!selectedId) inner = headHTML("click a resident above") + note("Select a resident in the heatmap to see its own EISV observation history.");
    else if (trajLoading) inner = headHTML("loading…") + note("Loading trajectory…");
    else if (!trajPoints.length) inner = headHTML("no history") + note("No observation history available" + (trajSource === "snapshot" ? " offline." : "."));
    else inner = headHTML(trajPoints.length + " state observations · " + esc(trajectorySources())) +
      `<div style="height:210px"><canvas id="eisv-traj-upper"></canvas></div>
       <div style="height:170px;margin-top:var(--space-3)"><canvas id="eisv-traj-lower"></canvas></div>
       ${trajectoryContextHTML()}`;
    if (selectedId && !trajLoading) inner += `<div style="margin-top:var(--space-4)"><button id="eisv-inspect-latest" class="theme-toggle" aria-expanded="false" aria-controls="eisv-observation-inspector">Inspect latest observation</button><div id="eisv-observation-inspector" style="margin-top:var(--space-3)"></div></div>`;
    mount.innerHTML = `<div class="panel" style="margin-bottom:var(--space-5)">${inner}</div>`;
    renderInspector();
    if (selectedId && !trajLoading && trajPoints.length) buildTrajectory();
  }

  function applySelectionHighlight() {
    document.querySelectorAll("#eisv-heatmap [data-traj-id]").forEach((el) => {
      el.style.outline = el.getAttribute("data-traj-id") === selectedId ? "1.5px solid var(--accent)" : "none";
      el.style.outlineOffset = "-1px";
    });
  }

  async function selectAgent(id, name) {
    const version = ++selectionVersion;
    selectedId = id; selectedName = name; trajLoading = true; trajPoints = [];
    trajSource = null; trajContext = null; inspector = null;
    if (trajUpper) { trajUpper.destroy(); trajUpper = null; }
    if (trajLower) { trajLower.destroy(); trajLower = null; }
    renderTrajectory(); applySelectionHighlight();
    // Compact provenance is present on every point. Full derivation histories
    // remain an explicit API opt-in and are unnecessary for these charts.
    const r = await DATA.agentHistory(id, { mode: "all", limit: 120 });
    if (selectionVersion !== version) return; // a newer selection won — drop this result
    trajLoading = false;
    // withFallback wraps the result as { source, data: { points, ... } }.
    trajPoints = (r && r.data && r.data.points) || [];
    trajSource = r && r.source;
    trajContext = r && r.data && r.data.trajectoryContext;
    renderTrajectory();
  }

  // Delegated click on the stable mount, bound once. Closest [data-traj-id]
  // survives the heatmap's periodic in-place re-render.
  function bindHeatmapClicks() {
    if (clickBound) return;
    const mount = document.getElementById("eisv-mount");
    if (!mount) return;
    mount.addEventListener("click", (e) => {
      if (e.target.closest("#eisv-inspect-latest")) { inspectLatest(); return; }
      const row = e.target.closest("[data-traj-id]");
      if (!row || !mount.contains(row)) return;
      const id = row.getAttribute("data-traj-id");
      if (id) selectAgent(id, row.getAttribute("data-traj-name") || id);
    });
    clickBound = true;
  }

  function recomputeSeries() {
    if (RAW.length) {
      MODEL.series = DATA.bucketEisv(RAW, SOURCE_FILTER);
      MODEL.sourceLanes = DATA.summarizeEisvSources(RAW);
    }
  }

  function bindSourceControl() {
    if (controlsBound) return;
    const mount = document.getElementById("eisv-mount");
    if (!mount) return;
    mount.addEventListener("change", (event) => {
      if (!event.target || event.target.id !== "eisv-source-filter") return;
      SOURCE_FILTER = event.target.value || "all";
      recomputeSeries();
      updateInPlace();
    });
    controlsBound = true;
  }

  function render() {
    $("#eisv-mount").innerHTML =
      `<div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4);flex-wrap:wrap">
         <span id="eisv-window-label" class="eyebrow" style="margin:0">Fleet trajectory · ${SOURCE_FILTER === "all" ? "all sources (mixed)" : esc(SOURCE_FILTER)} · last ${MODEL.series.length} min</span>
         <span class="spring"></span><label style="font-size:var(--text-sm);color:var(--muted)">source
           <select id="eisv-source-filter" style="margin-left:6px">${sourceOptions(MODEL.sourceLanes)}</select></label>
         <span class="src-badge ${MODEL.source}">${MODEL.source}</span></div>
       <div id="eisv-source-lanes">${sourceLanesHTML(MODEL.sourceLanes)}</div>
       <div id="eisv-heatmap">${heatmapHTML(MODEL.residents)}</div>
       <div id="eisv-trajectory"></div>
       <div class="panel" style="margin-bottom:var(--space-5)">
         <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Energy · Integrity · Coherence</h2></div>
         <div style="height:240px"><canvas id="eisv-upper"></canvas></div>
       </div>
       <div class="panel">
         <div class="panel-head" style="margin-bottom:var(--space-3)"><h2>Entropy · Valence</h2></div>
         <div style="height:200px"><canvas id="eisv-lower"></canvas></div>
       </div>`;
    bindHeatmapClicks();
    bindSourceControl();
    const sourceSelect = $("#eisv-source-filter");
    if (sourceSelect) sourceSelect.value = SOURCE_FILTER;
    renderTrajectory();
    applySelectionHighlight();
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
    const lanes = document.getElementById("eisv-source-lanes");
    if (lanes) lanes.innerHTML = sourceLanesHTML(MODEL.sourceLanes);
    const selector = document.getElementById("eisv-source-filter");
    if (selector) {
      selector.innerHTML = sourceOptions(MODEL.sourceLanes);
      selector.value = SOURCE_FILTER;
    }
    const windowLabel = document.getElementById("eisv-window-label");
    if (windowLabel) windowLabel.textContent = `Fleet trajectory · ${SOURCE_FILTER === "all" ? "all sources (mixed)" : SOURCE_FILTER} · last ${MODEL.series.length} min`;
    // Swap the heatmap in place too — its own container, so the canvases above
    // are untouched.
    const hm = document.getElementById("eisv-heatmap");
    if (hm) { hm.innerHTML = heatmapHTML(MODEL.residents); applySelectionHighlight(); }
    const badge = document.querySelector("#eisv-mount .src-badge");
    if (badge) { badge.className = "src-badge " + MODEL.source; badge.textContent = MODEL.source; }
    if (inspector && !inspector.loading) renderInspector();
  }

  async function load() {
    // Fleet trajectory (DATA.eisv) and the per-resident snapshot (DATA.residents)
    // in one batch — the heatmap reads the latter.
    const [r, res] = await Promise.all([DATA.eisv(), DATA.residents()]);
    RAW = (r.data.raw || []).slice(-RAW_MAX);
    const sourceLanes = RAW.length ? DATA.summarizeEisvSources(RAW) : (r.data.sourceLanes || []);
    if (SOURCE_FILTER !== "all" && !sourceLanes.some((lane) => lane.source === SOURCE_FILTER)) SOURCE_FILTER = "all";
    MODEL = {
      series: RAW.length ? DATA.bucketEisv(RAW, SOURCE_FILTER) : (r.data.series || []),
      coherenceEq: r.data.coherenceEq || 0.5, sourceLanes,
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
    RAW.push(msg);
    if (RAW.length > RAW_MAX) RAW = RAW.slice(-RAW_MAX);
    recomputeSeries();
    MODEL.source = "live"; // a live push by definition
    updateInPlace();
    return true;
  }
  // re-theme without refetch (called on theme toggle) — full rebuild reads new tokens
  function retheme() {
    if (MODEL.series.length && window.Chart) build();
    if (selectedId && trajPoints.length && window.Chart) buildTrajectory();
  }

  window.EISV = { load, retheme, applyEvent };
})();
