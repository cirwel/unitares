/*
 * Agents section — table, filters, badges, cohort split.
 * Rebuilt from the oracle (old agents.js) state model: status, trust
 * tier, lineage/superseded, stuck/inactive/stale, event-driven, redaction,
 * and the participated / never-participated partition. Composes kit
 * primitives; reads DATA.agents() (live-or-snapshot).
 */
(function () {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const TIER = { verified: 3, established: 2, emerging: 1, unknown: 0 };
  const num = (x, d = 2) => typeof x === "number" ? x.toFixed(d) : "—";

  let MODEL = { list: [], summary: {}, source: "snapshot", nowMs: 0 };
  let pageSize = 20;
  let selectedId = null;
  let histChart = null;
  let histMode = "recent"; // "recent" (raw events) | "all" (full lifespan, sampled)
  const histCache = {};

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

  // E/I/S/V bars + coh/risk/φ. `note` (optional) flags fallback data ("last check-in").
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
    const basin = m.basin ? `<span class="tag" style="color:${BASIN_COLOR[m.basin] || "var(--muted)"};border-color:color-mix(in srgb, ${BASIN_COLOR[m.basin] || "var(--line-2)"} 40%, var(--line-2))">${m.basin} basin</span>` : "";
    const tags = (a.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ");
    const idField = (label, val) => `<div style="display:flex;justify-content:space-between;gap:var(--space-4);padding:4px 0;border-bottom:var(--hairline) solid var(--line);font-size:var(--text-sm)"><span style="color:var(--muted)">${label}</span><span class="mono" style="color:var(--ink-2);text-align:right;word-break:break-all">${esc(val)}</span></div>`;
    return `<div class="panel" id="ag-detail" style="margin-bottom:var(--space-4);border-color:var(--line-2)">
      <div class="panel-head" style="margin-bottom:var(--space-4)">
        <span class="dot-pip" style="background:${a.status === "paused" ? "var(--warn)" : st.level === "dead" ? "var(--faint)" : "var(--ok)"}"></span>
        <h2 style="font-family:var(--font-display)">${a.label ? esc(a.label) : "anon"}</h2>
        ${tierBadge(a.tier)} ${basin}
        <span class="verdict ${verdictClass(m.verdict) === "ok" ? "" : verdictClass(m.verdict) === "warn" ? "warn" : "danger"}"><span class="pip"></span><span>${esc(m.verdict || "—")}</span></span>
        <span class="spring"></span>
        <button class="theme-toggle" id="ag-detail-close">✕ close</button>
      </div>
      <div class="split-2" style="gap:var(--space-6)">
        <div id="ag-state">${stateBlock(m, "")}</div>
        <div>
          <div class="eyebrow" style="margin-bottom:var(--space-3)">Identity</div>
          ${idField("id", a.agent_id || "—")}
          ${idField("status", a.status || "—")}
          ${idField("tier", a.tier || "—")}
          ${idField("updates", (a.updates || 0).toLocaleString())}
          ${idField("last seen", st.label)}
          ${a.parent ? idField("lineage parent", a.parent) : ""}
          ${a.superseded ? idField("superseded", a.lifecycleReason || "yes") : ""}
          ${a.event_driven ? idField("liveness", "event-driven") : ""}
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
      entry = { pts: (d.points || []).filter(Boolean), total: d.total || 0 };
      histCache[ck] = entry;
    }
    const pts = entry.pts, total = entry.total;
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
      meta.innerHTML = "· " + pts.length + (histMode === "all" ? " sampled" : "") + " check-ins" + ofTotal + span
        + (deep ? ` &nbsp; ${seg("recent", "recent")}${seg("all", "full lifespan")}` : "");
      meta.querySelectorAll(".hmode").forEach((b) => { b.onclick = () => { if (b.dataset.hmode !== histMode) { histMode = b.dataset.hmode; renderHistory(selectedId); } }; });
    }
    // Fall back the State bars to the agent's most recent recorded check-in when
    // the live list-metrics are null — clearly labelled, so it never reads as
    // current. pts is oldest→newest, so the last point is the latest check-in.
    const liveAgent = MODEL.list.find((x) => x.agent_id === id);
    if (!liveAgent || !liveAgent.metrics || typeof liveAgent.metrics.E !== "number") {
      const lp = pts[pts.length - 1], sb = document.getElementById("ag-state");
      if (lp && sb) sb.innerHTML = stateBlock(
        { E: lp.E, I: lp.I, S: lp.S, V: lp.V, coherence: lp.coherence, risk: lp.risk },
        "· last check-in " + staleness(lp.t, MODEL.nowMs).label);
    }
    const cv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const labels = pts.map((p) => wideSpan ? (p.t || "").slice(5, 10) : (p.t || "").slice(11, 16));
    // Event-based: each check-in is a discrete, hover-trackable point; straight
    // segments (no tension) so the line reflects actual check-ins, not a smoothed
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

  function verdictClass(v) {
    if (["proceed", "approve", "safe"].includes(v)) return "ok";
    if (["caution", "guide"].includes(v)) return "warn";
    return "danger";
  }

  function tierBadge(tier) {
    const t = TIER[tier] ?? 0;
    return `<span class="tag" title="Trust tier ${t}: ${tier}">T${t}</span>`;
  }

  function rowBadges(a, st) {
    const out = [];
    if (a.event_driven) out.push(`<span class="tag" title="event-driven resident — silence is not a liveness signal">event</span>`);
    else if (st.level === "stale" || st.level === "dead") out.push(`<span class="tag warn">inactive</span>`);
    if (a.superseded) out.push(`<span class="tag warn" title="${a.lifecycleReason || "superseded"}">superseded</span>`);
    if (a.parent) out.push(`<span class="tag" title="lineage parent ${a.parent}">↑ lineage</span>`);
    else if (a.redacted) out.push(`<span class="tag" title="identifiers redacted">redacted</span>`);
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
         <select id="ag-status" class="theme-toggle">${["all", "active", "paused", "archived"].map((s) => `<option ${s === statusF ? "selected" : ""}>${s}</option>`).join("")}</select>
         <select id="ag-sort" class="theme-toggle">${[["recent", "newest"], ["name", "name"], ["coherence", "coherence"], ["risk", "risk"], ["updates", "updates"]].map(([v, t]) => `<option value="${v}" ${v === sortF ? "selected" : ""}>${t}</option>`).join("")}</select>
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

    const participated = rows.filter((a) => (a.updates || 0) >= 1);
    const never = rows.filter((a) => (a.updates || 0) === 0);
    const shown = participated.slice(0, pageSize);

    const tr = (a) => {
      const st = staleness(a.last, MODEL.nowMs);
      const name = a.label || `<span style="color:var(--muted)">anon · ${(a.agent_id || "—").slice(0, 8)}</span>`;
      const pip = a.status === "paused" ? "var(--warn)" : a.status === "archived" ? "var(--faint)"
        : st.level === "dead" ? "var(--faint)" : "var(--ok)";
      const sel = a.agent_id === selectedId ? ' style="background:var(--surface-2);cursor:pointer" ' : ' style="cursor:pointer" ';
      return `<tr class="ag-row" data-id="${a.agent_id || ""}"${sel}>
        <td><span class="dot-pip" style="background:${pip}"></span></td>
        <td><div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
            <span style="font-weight:500;color:var(--ink)">${name}</span> ${tierBadge(a.tier)} ${rowBadges(a, st)}
          </div>${a.purpose ? `<div style="font-size:var(--text-xs);color:var(--muted);margin-top:2px">${a.purpose}</div>` : ""}</td>
        <td><span class="tag ${verdictClass(a.metrics.verdict)}">${a.metrics.verdict || "—"}</span></td>
        <td class="mono">${num(a.metrics.coherence)}</td>
        <td class="mono">${num(a.metrics.risk)}</td>
        <td class="mono">${(a.updates || 0).toLocaleString()}</td>
        <td class="mono" style="color:var(--muted)">${st.label}</td>
      </tr>`;
    };
    const head = `<thead><tr>
      <th></th><th>Agent</th><th>Verdict</th><th>Coh</th><th>Risk</th><th>Updates</th><th>Last seen</th>
    </tr></thead>`;
    const sm = MODEL.summary || {};
    const moreBtn = participated.length > pageSize
      ? `<div style="text-align:center;margin-top:var(--space-4)"><button class="theme-toggle" id="ag-more">Show ${Math.min(20, participated.length - pageSize)} more (${shown.length} of ${participated.length})</button></div>` : "";
    const neverGroup = never.length || sm.neverParticipated
      ? `<details style="margin-top:var(--space-5)"><summary style="cursor:pointer;color:var(--muted);font-size:var(--text-sm)">
           Never checked in — ${sm.neverParticipated ?? never.length} <span style="color:var(--faint)">· onboarded, no observations yet</span></summary>
         ${never.length ? `<table class="tbl" style="margin-top:var(--space-3)">${head}<tbody>${never.slice(0, 30).map(tr).join("")}</tbody></table>`
            : `<p class="empty">Not in this snapshot subset — ${sm.neverParticipated} fleet-wide.</p>`}</details>` : "";

    mount.innerHTML =
      `<div style="display:flex;gap:var(--space-5);margin-bottom:var(--space-3);font-size:var(--text-xs);color:var(--muted)">
         <span><b style="color:var(--ink)">${sm.total ?? rows.length}</b> total</span>
         <span><b style="color:var(--ink)">${sm.active ?? "—"}</b> active</span>
         <span><b style="color:var(--ink)">${sm.participated ?? participated.length}</b> participated</span>
         <span><b style="color:var(--ink)">${sm.archived ?? 0}</b> archived</span>
         <span class="src-badge ${MODEL.source}">${MODEL.source}</span>
       </div>
       ${shown.length ? `<table class="tbl">${head}<tbody>${shown.map(tr).join("")}</tbody></table>` : `<p class="empty">No agents match the current filters.</p>`}
       ${moreBtn}${neverGroup}`;
    wireResults();
  }
  function cmp_recent(a, b) { return Date.parse(b.last || 0) - Date.parse(a.last || 0); }

  function wireResults() {
    const more = $("#ag-more"); if (more) more.onclick = () => { pageSize += 20; renderResults(); };
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
    render();
    const d = document.getElementById("ag-detail");
    if (d && d.scrollIntoView) d.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function load() {
    const r = await DATA.agents();
    MODEL = {
      list: r.data.list || [], summary: r.data.summary || {}, source: r.source,
      nowMs: r.source === "live" ? Date.now() : Date.parse((window.SNAPSHOT && window.SNAPSHOT.capturedAt) || 0) || Date.now(),
    };
    render();
  }

  window.Agents = { load, select };
})();
