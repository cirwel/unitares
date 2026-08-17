/*
 * Agents section — table, filters, badges, cohort split.
 * Rebuilt from the oracle (old agents.js) state model: status, trust
 * tier, lineage/superseded, stuck/observation age, explicit presence,
 * event-driven semantics, redaction, and the observed/unobserved partition.
 * primitives; reads DATA.agents() (live-or-snapshot).
 */
(function () {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  // Ordinals per src/trajectory_identity.py + src/identity/trust_tier_routing.py.
  // provisional and emerging BOTH sit at ordinal 1 — provisional is a lineage
  // gate that pre-empts every other verdict, not a rung on the earning ladder —
  // so a `T{n}` badge is STRUCTURALLY unable to tell them apart, and rendered
  // them identically. The badge is keyed on the NAME; the ordinal moves to the
  // tooltip. The whitelist is also the guard on the var(--tier-*) interpolation.
  const TIER = {
    verified:    { n: 3, why: "long-running, behaviourally consistent" },
    established: { n: 2, why: "consistent across 50+ observations" },
    emerging:    { n: 1, why: "identity still forming" },
    provisional: { n: 1, why: "lineage unconfirmed — gated, cannot promote until lineage is confirmed" },
    unknown:     { n: 0, why: "no trajectory data" },
  };
  const num = (x, d = 2) => typeof x === "number" ? x.toFixed(d) : "—";

  let MODEL = { list: [], summary: {}, source: "snapshot", nowMs: 0 };
  let pageSize = 20;
  let selectedId = null;
  let histChart = null;
  let histMode = "recent"; // "recent" (raw events) | "all" (full lifespan, sampled)
  const histCache = {};
  // Deep-link intent from the Overview's Stuck card. Consume-once: applied on
  // navigation, never re-asserted (see focus()).
  let pendingFocus = null;
  let focusNote = null;

  const BASIN_COLOR = { high: "var(--ok)", boundary: "var(--warn)", low: "var(--danger)" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // One EISV bar row. E/I/S in [0,1]; V signed [-1,1] (centre-anchored).
  function eisvRow(k, val, cls, signed) {
    if (typeof val !== "number") return `<div class="eisv-row"><span class="k">${k}</span><span class="bar"></span><span class="val">—</span></div>`;
    const w = signed ? Math.abs(val) * 50 : Math.max(0, Math.min(1, val)) * 100;
    const left = signed ? (val < 0 ? 50 - Math.abs(val) * 50 : 50) : 0;
    return `<div class="eisv-row"><span class="k">${k}</span>`
      + `<span class="bar ${signed ? "signed" : ""}"><i class="${cls}" style="left:${left}%;width:${w}%"></i></span>`
      + `<span class="val">${num(val)}</span></div>`;
  }

  // E/I/S/V bars + coh/risk/φ. `note` identifies the observation source.
  function stateBlock(m, note) {
    m = m || {};
    return `<div class="eyebrow" style="margin-bottom:var(--space-3)">State${note ? ` <span style="text-transform:none;letter-spacing:0;color:var(--faint);font-weight:400">${note}</span>` : ""}</div>
      <div class="eisv" style="margin-bottom:var(--space-4)">
        ${eisvRow("E", m.E, "e", false)}${eisvRow("I", m.I, "i", false)}
        ${eisvRow("S", m.S, "s", false)}${eisvRow("V", m.V, "v", true)}
      </div>
      <div style="display:flex;gap:var(--space-5);font-family:var(--font-mono);font-size:var(--text-sm);color:var(--ink-2)">
        <span>coh ${num(m.coherence)}</span><span>risk ${num(m.risk)}</span>${typeof m.phi === "number" ? `<span>φ ${num(m.phi)}</span>` : ""}
      </div>`;
  }

  function detailPanel(a) {
    const m = a.metrics || {};
    const st = staleness(a.last, MODEL.nowMs);
    const presence = presenceInfo(a);
    const stateNote = m.source === "persisted_state"
      ? `· persisted ${staleness(m.recordedAt || a.last, MODEL.nowMs).label}`
        + (m.rollingMetricsAvailable === false ? " · rolling metrics unavailable" : "")
      : m.source === "live_monitor" ? "· in-memory monitor" : "";
    const basin = m.basin ? `<span class="tag" style="color:${BASIN_COLOR[m.basin] || "var(--muted)"};border-color:color-mix(in srgb, ${BASIN_COLOR[m.basin] || "var(--line-2)"} 40%, var(--line-2))">${m.basin} basin</span>` : "";
    const tags = (a.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ");
    const idField = (label, val) => `<div style="display:flex;justify-content:space-between;gap:var(--space-4);padding:4px 0;border-bottom:var(--hairline) solid var(--line);font-size:var(--text-sm)"><span style="color:var(--muted)">${label}</span><span class="mono" style="color:var(--ink-2);text-align:right;word-break:break-all">${esc(val)}</span></div>`;
    return `<div class="panel" id="ag-detail" style="margin-bottom:var(--space-4);border-color:var(--line-2)">
      <div class="panel-head" style="margin-bottom:var(--space-4)">
        <span class="dot-pip" style="background:${presence.color}"></span>
        <h2 style="font-family:var(--font-display)">${a.label ? esc(a.label) : "anon"}</h2>
        ${tierBadge(a.tier, true)} ${basin}
        <span class="verdict ${verdictClass(m.verdict) === "ok" ? "" : verdictClass(m.verdict) === "warn" ? "warn" : "danger"}"><span class="pip"></span><span>${esc(m.verdict || "—")}</span></span>
        <span class="spring"></span>
        <button class="theme-toggle" id="ag-detail-close">✕ close</button>
      </div>
      ${a.stuckReason ? `<div class="attn-band" style="margin-bottom:var(--space-4)"><span class="glyph">⚠</span><span>`
        + `${a.stuckSoft ? "Possible cadence silence" : "Flagged stuck"} — <b>${esc(a.stuckReason)}</b>${a.stuckDetails ? `. ${esc(a.stuckDetails)}` : ""}`
        + `</span></div>` : ""}
      <div class="split-2" style="gap:var(--space-6)">
        <div id="ag-state">${stateBlock(m, stateNote)}</div>
        <div>
          <div class="eyebrow" style="margin-bottom:var(--space-3)">Identity</div>
          ${idField("id", a.agent_id || "—")}
          ${idField("registry lifecycle", a.status || "—")}
          ${idField("runtime presence", presence.detail)}
          ${idField("tier", a.tier || "—")}
          ${idField("state rows", (a.updates || 0).toLocaleString())}
          ${idField("last state observation", (a.updates || 0) > 0 ? st.label : "none")}
          ${a.parent ? idField("lineage parent", a.parent) : ""}
          ${a.superseded ? idField("superseded", a.lifecycleReason || "yes") : ""}
          ${a.event_driven ? idField("cadence model", "event-driven") : ""}
        </div>
      </div>
      <div style="margin-top:var(--space-5)">
        <div class="eyebrow" style="margin-bottom:var(--space-3)">EISV trajectory <span id="ag-hist-meta" style="text-transform:none;letter-spacing:0;color:var(--faint);font-weight:400"></span></div>
        <div style="height:170px"><canvas id="ag-hist"></canvas></div>
      </div>
      ${a.purpose ? `<div style="margin-top:var(--space-4);font-size:var(--text-sm);color:var(--ink-2)">${esc(a.purpose)}</div>` : ""}
      ${tags ? `<div style="margin-top:var(--space-3);display:flex;gap:6px;flex-wrap:wrap">${tags}</div>` : ""}
    </div>`;
  }

  // EISV trajectory chart for the open agent (Chart.js, theme-aware token colours).
  async function renderHistory(id) {
    if (histChart) { histChart.destroy(); histChart = null; }
    const canvas = document.getElementById("ag-hist");
    const meta = document.getElementById("ag-hist-meta");
    if (!canvas || !window.Chart) return;
    const ck = id + ":" + histMode;
    let entry = histCache[ck];
    if (!entry) { // fetch once per (agent, mode); re-renders (search/filter) reuse the cache
      const r = await DATA.agentHistory(id, { limit: 200, mode: histMode });
      if (selectedId !== id || !document.getElementById("ag-hist")) return; // selection changed mid-fetch
      const d = r.data || {};
      entry = { pts: (d.points || []).filter(Boolean), total: d.total || 0,
        observationSummary: d.observationSummary || null };
      histCache[ck] = entry;
    }
    const pts = entry.pts, total = entry.total, observationSummary = entry.observationSummary || {};
    if (!pts.length) { if (meta) meta.textContent = "· no recorded history yet"; return; }
    // Context-aware framing: how much of the agent's life is shown, and over what
    // span. The recent⇄full toggle only appears when there's more history than the
    // recent window holds — a sparse ephemeral session just shows its whole life.
    const spanMs = pts.length > 1 ? (Date.parse(pts[pts.length - 1].t) - Date.parse(pts[0].t)) : 0;
    const wideSpan = spanMs > 1.5 * 864e5; // > ~1.5 days ⇒ label by date, not clock
    const fmtSpan = (ms) => { const h = ms / 3.6e6; return h < 1 ? Math.round(ms / 6e4) + "m" : h < 48 ? h.toFixed(0) + "h" : (h / 24).toFixed(0) + "d"; };
    if (meta) {
      const span = spanMs ? " · spans " + fmtSpan(spanMs) : "";
      const ofTotal = total > pts.length ? " of " + total.toLocaleString() : "";
      const deep = total > pts.length || histMode === "all"; // more history than recent holds
      const seg = (m, label) => `<button data-hmode="${m}" class="hmode${histMode === m ? " on" : ""}" style="font:inherit;cursor:pointer;background:none;border:none;padding:0 4px;color:${histMode === m ? "var(--ink-2)" : "var(--faint)"};text-decoration:${histMode === m ? "underline" : "none"}">${label}</button>`;
      const reports = observationSummary.agent_reports;
      const substrate = observationSummary.substrate_rows;
      const envelopes = observationSummary.telemetry_envelopes;
      const stateRows = observationSummary.state_rows ?? total;
      const provenance = typeof reports === "number" && typeof substrate === "number"
        ? ` · ${reports} authored · ${substrate} automatic` : "";
      const telemetry = typeof envelopes !== "number" ? ""
        : envelopes === 0 ? " · telemetry legacy/missing"
          : ` · ${envelopes}/${stateRows} telemetry`;
      meta.innerHTML = "· " + pts.length + (histMode === "all" ? " sampled" : "") + " state observations" + ofTotal + span + provenance + telemetry
        + (deep ? ` &nbsp; ${seg("recent", "recent")}${seg("all", "full lifespan")}` : "");
      meta.querySelectorAll(".hmode").forEach((b) => { b.onclick = () => { if (b.dataset.hmode !== histMode) { histMode = b.dataset.hmode; renderHistory(selectedId); } }; });
    }
    // Fall back the State bars to the agent's most recent state observation when
    // the live list-metrics are null — clearly labelled, so it never reads as
    // current. pts is oldest→newest, so the last point is the latest observation.
    const liveAgent = MODEL.list.find((x) => x.agent_id === id);
    if (!liveAgent || !liveAgent.metrics || typeof liveAgent.metrics.E !== "number") {
      const lp = pts[pts.length - 1], sb = document.getElementById("ag-state");
      if (lp && sb) sb.innerHTML = stateBlock(
        { E: lp.E, I: lp.I, S: lp.S, V: lp.V, coherence: lp.coherence, risk: lp.risk },
        "· last state observation " + staleness(lp.t, MODEL.nowMs).label);
    }
    const cv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const labels = pts.map((p) => wideSpan ? (p.t || "").slice(5, 10) : (p.t || "").slice(11, 16));
    // Event-based: each state observation is a discrete, hover-trackable point;
    // straight segments (no tension) reflect actual rows, not a smoothed
    // interpolation. Markers shrink as the series gets denser.
    const pr = pts.length > 140 ? 1.3 : pts.length > 70 ? 1.8 : 2.6;
    const ds = (label, key, color, dash) => ({
      label, data: pts.map((p) => p[key]), borderColor: color, backgroundColor: color,
      pointBackgroundColor: color, pointBorderColor: color,
      borderWidth: 1.3, borderDash: dash || [], pointRadius: pr, pointHoverRadius: 5, tension: 0,
    });
    const grid = cv("--line"), tick = cv("--muted");
    histChart = new window.Chart(canvas, {
      type: "line",
      data: { labels, datasets: [
        ds("E", "E", cv("--eisv-e")), ds("I", "I", cv("--eisv-i")),
        ds("S", "S", cv("--eisv-s")), ds("V", "V", cv("--eisv-v")),
        ds("coherence", "coherence", cv("--eisv-c"), [4, 3]),
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 200 },
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: true, position: "bottom", labels: { color: tick, font: { family: "Inter", size: 10 }, boxWidth: 9, boxHeight: 9, usePointStyle: true } },
          tooltip: { backgroundColor: cv("--surface"), borderColor: cv("--line-2"), borderWidth: 1, titleColor: cv("--ink"), bodyColor: tick, titleFont: { family: "Geist Mono" }, bodyFont: { family: "Geist Mono", size: 10 },
            callbacks: { title: (its) => { const p = pts[its[0] && its[0].dataIndex]; return p && p.t ? new Date(p.t).toLocaleString() : ""; } } } },
        scales: {
          x: { grid: { color: grid, drawTicks: false }, ticks: { color: tick, font: { family: "Geist Mono", size: 9 }, maxRotation: 0, autoSkipPadding: 24 } },
          y: { min: -0.6, max: 1, grid: { color: grid }, ticks: { color: tick, font: { family: "Geist Mono", size: 9 }, callback: (v) => v.toFixed(1) } },
        },
      },
    });
  }

  function staleness(lastIso, nowMs) {
    if (!lastIso) return { level: "unknown", label: "—" };
    const age = nowMs - Date.parse(lastIso);
    const m = age / 60000, h = m / 60, d = h / 24;
    if (m < 10) return { level: "fresh", label: "just now" };
    if (m < 60) return { level: "recent", label: Math.round(m) + "m ago" };
    if (h < 24) return { level: "stale", label: Math.round(h) + "h ago" };
    return { level: "dead", label: Math.round(d) + "d ago" };
  }

  function presenceInfo(a) {
    if (a.leaseOverdue) {
      return { status: "unknown", label: "overdue", detail: "unknown · resident lease overdue", color: "var(--warn)" };
    }
    if (a.leaseAnchored) {
      return { status: "live", label: "live", detail: "live · resident lease", color: "var(--ok)" };
    }
    const p = a.presence || {};
    if (p.status === "live") {
      const names = {
        process_binding: "process binding",
        resident_lease: "resident lease",
        agent_presence_lease: "agent presence lease",
        surface_lease: "active surface lease",
      };
      const evidence = (p.signals || []).map((s) => names[s] || s).join(" + ") || "explicit signal";
      return { status: "live", label: "live", detail: `live · ${evidence}`, color: "var(--ok)" };
    }
    if (p.status === "unknown") {
      const suffix = a.event_driven ? " · event-driven" : "";
      return { status: "unknown", label: "unknown", detail: `unknown · no live binding/lease${suffix}`, color: "var(--faint)" };
    }
    return { status: "unavailable", label: "unavailable", detail: "unavailable · presence lookup failed", color: "var(--faint)" };
  }

  function verdictClass(v) {
    if (["proceed", "approve", "safe"].includes(v)) return "ok";
    if (["caution", "guide"].includes(v)) return "warn";
    return "danger";
  }

  // `always` = detail panel (state every agent's tier, even unknown). Rows omit
  // unknown/absent: ~77 of 100 live rows carry no earned tier, and a badge on
  // all of them buries the ~23 that mean something.
  function tierBadge(tier, always) {
    const key = Object.prototype.hasOwnProperty.call(TIER, tier) ? tier : null;
    if (!key || key === "unknown") {
      if (!always) return "";
      const known = key === "unknown";
      return `<span class="tag tier" style="--tier:var(--tier-unknown)"`
        + ` title="Trust tier 0: ${known ? "unknown — " + TIER.unknown.why : "not computed — server returned no trust_tier"}">`
        + `<i></i>${known ? "unknown" : "no tier"}</span>`;
    }
    const t = TIER[key];
    return `<span class="tag tier" style="--tier:var(--tier-${key})"`
      + ` title="Trust tier ${t.n}: ${key} — ${t.why}"><i></i>${key}</span>`;
  }

  function rowBadges(a, st) {
    const out = [];
    // leaseOverdue before leaseAnchored: a lease-anchored resident whose SERVER
    // status says it is past its check-in threshold used to render as calmly
    // alive here (synthesised `last` → staleness "fresh" → green pip → "lease
    // heartbeat") while the Overview flagged it. Same predicate both places now.
    if (a.leaseOverdue) out.push(`<span class="tag warn" title="lease-anchored resident past its check-in threshold (server status: ${esc(a.leaseStatus || "down")})">overdue</span>`);
    else if (a.leaseAnchored) out.push(`<span class="tag" title="in-process resident — liveness from its lease-plane heartbeat, not check-in rows">lease heartbeat</span>`);
    else if (a.event_driven) out.push(`<span class="tag" title="event-driven resident — silence is not a liveness signal">event</span>`);
    // A stuck reason is a SPECIFIC claim; it replaces generic observation age
    // rather than sitting beside it. They are different concepts and stay
    // separate: stale observations say nothing about process liveness; `stuck`
    // means the agent was in a state implying it should have spoken and didn't.
    else if (a.stuckReason) out.push(`<span class="tag warn" title="${esc(a.stuckDetails || "")}">${esc(a.stuckReason)}${a.stuckSoft ? " · soft" : ""}</span>`);
    else if ((a.updates || 0) > 0 && (st.level === "stale" || st.level === "dead")) out.push(`<span class="tag warn">stale observation</span>`);
    if (a.superseded) out.push(`<span class="tag warn" title="${a.lifecycleReason || "superseded"}">superseded</span>`);
    if (a.parent) out.push(`<span class="tag" title="lineage parent ${a.parent}">↑ lineage</span>`);
    // No per-row `redacted` chip: redaction applies to nearly every non-verified
    // row, so as a chip it is uniform noise. The fact survives as a title on the
    // agent name (see tr()).
    return out.join(" ");
  }

  function render() {
    // Controls keep the user's RAW search text (case preserved); filtering reads
    // it lowercased in renderResults(). The results live in their own container,
    // so typing/filtering re-renders only the rows — the search box keeps focus,
    // cursor and text, and an open detail's chart isn't torn down per keystroke.
    const q = (($("#ag-search") && $("#ag-search").value) || "");
    const statusF = $("#ag-status") ? $("#ag-status").value : "all";
    const sortF = $("#ag-sort") ? $("#ag-sort").value : "recent";
    const prodOnly = $("#ag-prod") ? $("#ag-prod").checked : false;

    const selected = selectedId && MODEL.list.find((a) => a.agent_id === selectedId);
    $("#ag-mount").innerHTML =
      (selected ? detailPanel(selected) : "")
      + `<div style="display:flex;gap:var(--space-3);flex-wrap:wrap;align-items:center;margin-bottom:var(--space-4)">
         <input id="ag-search" placeholder="search name · id · purpose · tag" value="${q.replace(/"/g, "&quot;")}"
           style="flex:1;min-width:200px;padding:var(--space-2) var(--space-3);font-family:var(--font-sans);font-size:var(--text-sm);background:var(--surface);color:var(--ink);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-sm)" />
         <select id="ag-status" class="theme-toggle">${[["all", "all lifecycles"], ["active", "lifecycle: active"], ["paused", "lifecycle: paused"], ["archived", "lifecycle: archived"]].map(([v, t]) => `<option value="${v}" ${v === statusF ? "selected" : ""}>${t}</option>`).join("")}</select>
         <select id="ag-sort" class="theme-toggle">${[["recent", "newest"], ["name", "name"], ["coherence", "coherence"], ["risk", "risk"], ["updates", "state rows"]].map(([v, t]) => `<option value="${v}" ${v === sortF ? "selected" : ""}>${t}</option>`).join("")}</select>
         <label style="font-size:var(--text-xs);color:var(--muted);display:flex;gap:6px;align-items:center"><input type="checkbox" id="ag-prod" ${prodOnly ? "checked" : ""}/> prod only</label>
       </div>
       <div id="ag-results"></div>`;

    wire();
    renderResults();
    if (selected) renderHistory(selectedId);
    else if (histChart) { histChart.destroy(); histChart = null; }
  }

  // Rows + summary only — re-rendered on each keystroke/filter without touching
  // the controls (search keeps focus/cursor) or the open detail panel/chart.
  function renderResults() {
    const mount = $("#ag-results"); if (!mount) return;
    const q = (($("#ag-search") && $("#ag-search").value) || "").toLowerCase().trim();
    const statusF = $("#ag-status") ? $("#ag-status").value : "all";
    const sortF = $("#ag-sort") ? $("#ag-sort").value : "recent";
    const prodOnly = $("#ag-prod") ? $("#ag-prod").checked : false;

    let rows = MODEL.list.slice().filter((a) => {
      if (statusF !== "all" && a.status !== statusF) return false;
      if (prodOnly && (a.tags || []).some((t) => /test|experimental|ephemeral/.test(t))) return false;
      if (q) {
        const hay = ((a.label || "") + " " + a.agent_id + " " + (a.purpose || "") + " " + (a.tags || []).join(" ")).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    const cmp = {
      recent: (a, b) => Date.parse(b.last || 0) - Date.parse(a.last || 0),
      name: (a, b) => (a.label || a.agent_id || "").localeCompare(b.label || b.agent_id || ""),
      coherence: (a, b) => (b.metrics.coherence ?? -1) - (a.metrics.coherence ?? -1),
      risk: (a, b) => (b.metrics.risk ?? -1) - (a.metrics.risk ?? -1),
      updates: (a, b) => (b.updates || 0) - (a.updates || 0),
    }[sortF] || cmp_recent;
    rows.sort(cmp);

    const observed = rows.filter((a) => (a.updates || 0) >= 1);
    const unobserved = rows.filter((a) => (a.updates || 0) === 0);
    const shown = observed.slice(0, pageSize);
    // A selected agent's row must be IN the table. The Overview's Stuck card
    // deep-links here, and a flagged agent tends to be old BY CONSTRUCTION —
    // `cadence_silence` means it stopped speaking — so under the default
    // `recent` sort most detections land past the first page (measured
    // 2026-07-31: 4 of 5 at sorted indices 24/28/29/33, pageSize 20). The card
    // said "needs attention" and the destination showed nothing corroborating
    // it. Pin the selection instead of growing the page, so the row keeps its
    // true sort position everywhere else.
    let pinned = false;
    if (selectedId && !shown.some((a) => a.agent_id === selectedId)) {
      const pin = observed.find((a) => a.agent_id === selectedId);
      if (pin) { shown.unshift(pin); pinned = true; }
    }

    const tr = (a, isPin) => {
      const st = staleness(a.last, MODEL.nowMs);
      const presence = presenceInfo(a);
      const name = a.label || `<span style="color:var(--muted)">anon · ${(a.agent_id || "—").slice(0, 8)}</span>`;
      const sel = a.agent_id === selectedId ? ' style="background:var(--surface-2);cursor:pointer" ' : ' style="cursor:pointer" ';
      // A pinned row is out of sort position — say so rather than letting it
      // read as "the most recent agent".
      const pinTag = isPin ? ` <span class="tag" title="pinned to the top because it is open below — its real position is further down this sort">pinned</span>` : "";
      return `<tr class="ag-row" data-id="${a.agent_id || ""}"${sel}>
        <td><span class="dot-pip" style="background:${presence.color}"></span></td>
        <td><div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
            <span style="font-weight:500;color:var(--ink)"${a.redacted ? ' title="identifiers redacted server-side"' : ""}>${name}</span> ${tierBadge(a.tier)} ${rowBadges(a, st)}${pinTag}
          </div>${a.purpose ? `<div style="font-size:var(--text-xs);color:var(--muted);margin-top:2px">${a.purpose}</div>` : ""}</td>
        <td><span class="tag ${verdictClass(a.metrics.verdict)}">${a.metrics.verdict || "—"}</span></td>
        <td class="mono">${num(a.metrics.coherence)}</td>
        <td class="mono">${num(a.metrics.risk)}</td>
        <td class="mono">${(a.updates || 0).toLocaleString()}</td>
        <td><span class="tag" title="${esc(presence.detail)}">${esc(presence.label)}</span></td>
        <td class="mono" style="color:var(--muted)">${(a.updates || 0) > 0 ? st.label : "none"}</td>
      </tr>`;
    };
    const head = `<thead><tr>
      <th></th><th>Agent</th><th title="Governance policy verdict at the last check-in — not derived from the raw risk number, so high risk beside 'safe' is expected">Verdict</th><th>Coh</th><th title="Raw risk telemetry (0–1): instrumentation, not a judgment. Verdicts come from policy, so this column deliberately is not colour-coded">Risk</th><th>State rows</th><th>Presence</th><th>Last observation</th>
    </tr></thead>`;
    const sm = MODEL.summary || {};
    const unknownPresence = typeof sm.presenceUnknown === "number" || typeof sm.presenceUnavailable === "number"
      ? (sm.presenceUnknown || 0) + (sm.presenceUnavailable || 0) : null;
    const moreBtn = observed.length > pageSize
      ? `<div style="text-align:center;margin-top:var(--space-4)"><button class="theme-toggle" id="ag-more">Show ${Math.min(20, observed.length - pageSize)} more (${shown.length} of ${observed.length})</button></div>` : "";
    const unobservedGroup = unobserved.length || sm.unobserved
      ? `<details style="margin-top:var(--space-5)"><summary style="cursor:pointer;color:var(--muted);font-size:var(--text-sm)">
           No state observations — ${sm.unobserved ?? unobserved.length} <span style="color:var(--faint)">· onboarded, no measured rows yet</span></summary>
         ${unobserved.length ? `<table class="tbl" style="margin-top:var(--space-3)">${head}<tbody>${unobserved.slice(0, 30).map(tr).join("")}</tbody></table>`
            : `<p class="empty">Not in this snapshot subset — ${sm.unobserved} fleet-wide.</p>`}</details>` : "";

    const note = focusNote
      ? `<div class="attn-band" style="margin-bottom:var(--space-3)"><span class="glyph">·</span><span>`
        + `<b>${esc(focusNote)}</b> is flagged stuck but is not in the loaded window`
        + `${sm.total ? ` (showing ${MODEL.list.length} of ${sm.total})` : ""} — search for it above.</span></div>`
      : "";
    // This pane is NOT in app.html's RELOAD map and lazyLoad guards on
    // loaded[id], so load() runs exactly once per page load — the table and its
    // stuck flags are a point-in-time read that can sit for an entire session.
    // The source badge alone said "live", which reads as live-UPDATING. Stamp
    // the read time and give it the manual refresh Automations/Metrics have
    // (auto-refresh is wrong here: render() rebuilds the search box and both
    // selects and tears down an open detail's chart).
    const asOf = MODEL.fetchedAt
      ? new Date(MODEL.fetchedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
    const stuckNote = MODEL.stuckOmitted
      ? `<span style="color:var(--warn)" title="${esc(MODEL.stuckOmitted)}">⚠ no stuck reasons</span>` : "";
    mount.innerHTML = note +
      `<div style="display:flex;gap:var(--space-5);margin-bottom:var(--space-3);font-size:var(--text-xs);color:var(--muted);align-items:center;flex-wrap:wrap">
         <span title="Registry identities seen in the last 14 days. The Overview's Agents card reads a 30-day window, so its total is larger."><b style="color:var(--ink)">${sm.total ?? rows.length}</b> total</span>
         <span title="Registry lifecycle state; not a process-liveness claim"><b style="color:var(--ink)">${sm.active ?? "—"}</b> registry active</span>
         <span title="Currently held session binding or lease heartbeat — a right-now liveness signal. Recently-active rows sort to the top, so the first page can be all-live while most of the fleet has no signal."><b style="color:var(--ink)">${sm.live ?? "—"}</b> live signal</span>
         <span title="No liveness signal either way — most one-shot sessions end without deregistering"><b style="color:var(--ink)">${unknownPresence ?? "—"}</b> presence unknown</span>
         <span title="Has at least one measured state row (EISV observation)"><b style="color:var(--ink)">${sm.observed ?? observed.length}</b> observed</span>
         <span title="Registry lifecycle: archived"><b style="color:var(--ink)">${sm.archived ?? 0}</b> archived</span>
         <span class="src-badge ${MODEL.source}">${MODEL.source}</span>
         <span style="color:var(--faint)" title="This view does not auto-refresh — it is a point-in-time read.">read ${asOf}</span>
         <button id="ag-refresh" class="theme-toggle" title="Re-read the fleet">↻</button>
         ${stuckNote}
       </div>
       ${shown.length ? `<table class="tbl">${head}<tbody>${shown.map((a, i) => tr(a, pinned && i === 0)).join("")}</tbody></table>` : `<p class="empty">No agents match the current filters.</p>`}
       ${moreBtn}${unobservedGroup}`;
    wireResults();
  }
  function cmp_recent(a, b) { return Date.parse(b.last || 0) - Date.parse(a.last || 0); }

  function wireResults() {
    const more = $("#ag-more"); if (more) more.onclick = () => { pageSize += 20; renderResults(); };
    const rf = $("#ag-refresh"); if (rf) rf.onclick = () => { load(); };
    document.querySelectorAll("#ag-results .ag-row").forEach((row) => { row.onclick = () => select(row.dataset.id); });
  }

  function wire() {
    const s = $("#ag-search"); if (s) s.oninput = () => { pageSize = 20; renderResults(); };
    ["#ag-status", "#ag-sort", "#ag-prod"].forEach((id) => { const el = $(id); if (el) el.onchange = () => { pageSize = 20; renderResults(); }; });
    const close = $("#ag-detail-close"); if (close) close.onclick = () => { selectedId = null; render(); };
  }

  // Open an agent's detail (also callable for deep-link/verification).
  function select(id) {
    if (id && id !== selectedId) histMode = "recent"; // new agent → default to recent events
    selectedId = (id && id === selectedId) ? null : id; // click again to close
    focusNote = null;
    render();
    const d = document.getElementById("ag-detail");
    if (d && d.scrollIntoView) d.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ── deep-link focus (Overview Stuck card → one agent) ──────────────────────
  // Applying intent inside load() alone would be broken by construction:
  // app.html's lazyLoad guards on loaded[id], so load() runs EXACTLY ONCE per
  // page load. A second click from the Overview would silently no-op. So focus()
  // works both before and after the section has loaded, and clears itself —
  // "apply once on navigation", never "re-assert on every render".
  function focus(id) {
    if (!id) return;
    pendingFocus = id;
    // Defer a tick so a hash-driven pane switch (and its lazyLoad) lands first
    // and scrollIntoView has a laid-out target.
    setTimeout(flushFocus, 0);
  }
  function flushFocus() {
    const id = pendingFocus;
    if (!id) return;
    if (!MODEL.list.length) return; // not loaded yet — load() flushes after render
    pendingFocus = null;
    const hit = MODEL.list.find((a) => a.agent_id === id);
    if (hit) { select(id); return; }
    // Honest failure. agent(action=list) is truncated to 100 rows server-side
    // (serialization.py) out of ~457, so a flagged agent can genuinely be
    // outside the loaded window. Say so rather than opening nothing.
    focusNote = id;
    renderResults();
  }

  async function load() {
    const r = await DATA.agents();
    MODEL = {
      list: r.data.list || [], summary: r.data.summary || {}, source: r.source,
      nowMs: r.source === "live" ? Date.now() : Date.parse((window.SNAPSHOT && window.SNAPSHOT.capturedAt) || 0) || Date.now(),
      fetchedAt: Date.now(), // wall-clock read time — this pane does not auto-refresh
    };
    // Lease-anchored residents can have zero state rows BY DESIGN — their
    // liveness is the lease-plane heartbeat. Keep that presence timestamp
    // separate from `last`, which means last STATE observation in this pane.
    try {
      const fresh = (await DATA.residentFreshness()).data || {};
      MODEL.list.forEach((a) => {
        const f = a.label && fresh[a.label];
        if (f && (a.updates || 0) === 0 && typeof f.silence === "number") {
          // `f.status` used to be stored and never read: an overdue resident
          // still got leaseAnchored=true and a synthesised fresh `last`. Route
          // through the one predicate so this pane and the Overview agree.
          const liveness = DATA.residentLiveness(f);
          a.leaseAnchored = liveness !== "down";
          a.leaseOverdue = liveness === "down";
          a.leaseStatus = f.status;
          a.presence = {
            status: liveness === "down" ? "unknown" : "live",
            signals: ["resident_lease"],
            observedAt: new Date(MODEL.nowMs - f.silence * 1000).toISOString(),
          };
        }
      });
    } catch { /* freshness is an enhancement — the pane renders without it */ }
    // Stuck reasons, joined on the SAME redacted handle the list emits. The
    // registry UUID detect_stuck_agents keys on is never visible to this client.
    //
    // PROVENANCE GUARD. A stuck reason is a governance ACCUSATION against a
    // named agent, and the two sides of this join cross the live/snapshot seam
    // independently. detect_stuck_agents walks the whole fleet, so it is the
    // call in this pane most likely to fail ALONE (stats() wraps the same call
    // in .catch(() => null) for exactly that reason) — and DATA.stuckAgents()
    // then falls back to the BUNDLED snapshot list. This pane has ONE source
    // badge and it reflects DATA.agents(), so a partial failure used to stamp
    // fixture findings onto live rows under a "live" badge: a healthy agent
    // wearing a fabricated `critical_margin_timeout`. Enrich only when both
    // sides came from the same world, and say so when they didn't.
    try {
      const sr = await DATA.stuckAgents();
      // Omitted, not silently absent: the table drops back to its generic
      // staleness tags and the summary bar says the reasons are missing.
      MODEL.stuckOmitted = sr.source !== MODEL.source
        ? `stuck reasons withheld — the detection call fell back to the bundled snapshot while this table is ${MODEL.source}`
        : null;
      if (!MODEL.stuckOmitted) {
        const byId = {};
        (sr.data || []).forEach((s) => { if (s.id) byId[s.id] = s; });
        MODEL.list.forEach((a) => {
          const s = byId[a.agent_id];
          if (s) { a.stuckReason = s.reason; a.stuckDetails = s.details; a.stuckSoft = s.soft === true; }
        });
      }
    } catch { MODEL.stuckOmitted = "stuck reasons unavailable — the detection call did not answer"; }
    render();
    flushFocus(); // a deep-link that arrived before this section existed
  }

  window.Agents = { load, select, focus };
})();
