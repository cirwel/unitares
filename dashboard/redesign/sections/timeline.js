/*
 * Timeline section — one row per agent across a time window.
 * Marks on each row: check-ins (shape by policy action; hook-recorded ones
 * hollow), graded outcomes (✓/✕), knowledge writes, subagents started
 * (lineage), lifecycle changes, plus bands for pauses and dialectic reviews.
 * Selecting a mark shows its record. Reads DATA.timeline() (/v1/timeline).
 *
 * Rows are bounded: agents ordered by most recent event, the first
 * ROW_LIMIT shown until "show all", with a text filter on label or id.
 * The skeleton (controls, filter input) renders once; refreshes update the
 * body in place so a typed filter is never clobbered.
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const ROW_LIMIT = 25;
  const WINDOWS = [1, 6, 24];
  // Refetching on the live tick is cheap for short windows (the 6h read is
  // ~65 ms). A 24h read is several hundred KB, so that window loads on
  // demand only.
  const AUTO_MAX_HOURS = 6;

  const state = { hours: 6, includeSubstrate: true, showAll: false, filter: "", sel: null };
  let MODEL = null;
  let built = false;
  let BY_KEY = {};

  const two = (n) => (n < 10 ? "0" : "") + n;
  function utc(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return "—";
    const d = new Date(t);
    return `${d.getUTCFullYear()}-${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}:${two(d.getUTCSeconds())} UTC`;
  }
  const hhmm = (ms) => { const d = new Date(ms); return `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`; };
  const shortId = (s) => String(s || "").slice(0, 8) || "—";

  // The check-in's policy action, normalised to the three shapes drawn.
  function actionClass(a) {
    const v = String(a || "").toLowerCase();
    if (v === "guide") return "guide";
    if (v === "pause" || v === "reject") return "pause";
    return "proceed";
  }

  const KIND_TITLE = {
    checkin: (e) => e.authored_by === "agent" ? `Check-in · ${e.action || "unknown"}` : "Hook observation",
    outcome: (e) => `Outcome · ${e.is_bad ? "failed" : "passed"}`,
    knowledge: () => "Finding shared",
    lineage: () => "Started a subagent",
    lifecycle: (e) => String(e.event_type || "lifecycle").replace(/^lifecycle_/, "").replace(/_/g, " "),
    dialectic: () => "Dialectic review",
    pause: () => "Paused",
  };
  const title = (e) => (KIND_TITLE[e.kind] || (() => e.kind))(e);

  function windowOf(model) {
    const w = model.window || {};
    let end = Date.parse(w.end);
    if (isNaN(end)) end = Date.now();
    let start = Date.parse(w.start);
    if (isNaN(start)) start = end - (model.hours || state.hours) * 3600e3;
    return { start, end };
  }

  // Group events into agent rows, place each mark, nest subagents under a
  // visible parent, and order rows by most recent event.
  function buildRows(model) {
    const { start, end } = windowOf(model);
    const span = Math.max(1, end - start);
    const pct = (iso) => Math.max(0, Math.min(100, ((Date.parse(iso) - start) / span) * 100));
    const meta = {};
    (model.agents || []).forEach((a) => { meta[a.agent_id] = a; });
    const rows = {};
    const rowOf = (id) => rows[id] || (rows[id] = { id, marks: [], bands: [], last: 0, parent: null, checkins: 0 });
    // Keys come from the event itself, so a selection survives a refresh
    // that adds or drops other events.
    BY_KEY = {};
    const keep = (e) => {
      const key = [e.kind, e.agent_id, e.ts, e.event_id || e.outcome_id || e.session_id || e.authored_by || ""].join("|");
      BY_KEY[key] = e;
      return key;
    };

    const paused = {};
    (model.events || []).forEach((e) => {
      if (!e || !e.agent_id || isNaN(Date.parse(e.ts))) return;
      const t = Date.parse(e.ts);
      if (e.kind === "lineage" && e.parent_agent_id) {
        rowOf(e.agent_id).parent = e.parent_agent_id;
        const p = rowOf(e.parent_agent_id);
        p.marks.push({ key: keep(e), cls: "lineage", left: pct(e.ts), e });
        p.last = Math.max(p.last, t);
        return;
      }
      const row = rowOf(e.agent_id);
      row.last = Math.max(row.last, t);
      if (e.kind === "dialectic") {
        const to = e.end && !isNaN(Date.parse(e.end)) ? pct(e.end) : 100;
        row.bands.push({ key: keep(e), cls: "dialectic", left: pct(e.ts), width: Math.max(0.4, to - pct(e.ts)), e });
        return;
      }
      if (e.kind === "lifecycle") {
        if (e.event_type === "lifecycle_paused") paused[e.agent_id] = e;
        if (e.event_type === "lifecycle_resumed") {
          const from = paused[e.agent_id];
          const band = { kind: "pause", agent_id: e.agent_id, ts: from ? from.ts : new Date(start).toISOString(), end: e.ts, reason: from ? from.reason : null, lifted_by: e.reason };
          row.bands.push({ key: keep(band), cls: "pause", left: pct(band.ts), width: Math.max(0.4, pct(e.ts) - pct(band.ts)), e: band });
          delete paused[e.agent_id];
        }
      }
      let cls = e.kind;
      let glyph = "";
      if (e.kind === "checkin") {
        cls += e.authored_by === "agent" ? " " + actionClass(e.action) : " substrate";
        if (e.authored_by === "agent") row.checkins++;
      } else if (e.kind === "outcome") {
        cls += e.is_bad ? " bad" : "";
        glyph = e.is_bad ? "✕" : "✓";
      }
      row.marks.push({ key: keep(e), cls, glyph, left: pct(e.ts), e });
    });
    Object.keys(paused).forEach((id) => {
      const from = paused[id];
      const band = { kind: "pause", agent_id: id, ts: from.ts, end: null, reason: from.reason, lifted_by: null };
      rows[id].bands.push({ key: keep(band), cls: "pause", left: pct(from.ts), width: Math.max(0.4, 100 - pct(from.ts)), e: band });
    });

    Object.values(rows).forEach((r) => {
      const m = meta[r.id] || {};
      r.label = m.label || shortId(r.id);
      r.meta = m;
      if (!r.parent && m.parent_agent_id) r.parent = m.parent_agent_id;
    });
    const byRecency = Object.values(rows).sort((a, b) => b.last - a.last);
    const ordered = [];
    const placed = new Set();
    const place = (r, depth) => {
      if (placed.has(r.id)) return;
      placed.add(r.id);
      ordered.push(Object.assign(r, { child: depth > 0 }));
      byRecency.filter((c) => c.parent === r.id).forEach((c) => place(c, depth + 1));
    };
    byRecency.forEach((r) => { if (!r.parent || !rows[r.parent]) place(r, 0); });
    byRecency.forEach((r) => place(r, 0));
    return { rows: ordered, start, end, labels: Object.fromEntries(Object.values(rows).map((r) => [r.id, r.label])) };
  }

  function visibleRows(all) {
    const q = state.filter.trim().toLowerCase();
    const matched = q ? all.filter((r) => r.label.toLowerCase().includes(q) || r.id.toLowerCase().includes(q)) : all;
    return { matched, shown: state.showAll ? matched : matched.slice(0, ROW_LIMIT) };
  }

  function rowHtml(r, labels) {
    const sub = r.child && r.parent
      ? `↳ subagent of ${esc(labels[r.parent] || shortId(r.parent))}`
      : [r.meta.model_type, r.meta.trust_tier, `${r.checkins} check-in${r.checkins === 1 ? "" : "s"}`].filter(Boolean).map(esc).join(" · ");
    const bands = r.bands.map((b) => `<button type="button" class="tl-band ${b.cls}${state.sel === b.key ? " sel" : ""}" style="left:${b.left.toFixed(3)}%;width:${b.width.toFixed(3)}%" data-k="${esc(b.key)}" aria-label="${esc(title(b.e))}, ${esc(r.label)}"></button>`).join("");
    const marks = r.marks.map((m) => {
      const label = `${title(m.e)}, ${r.label}, ${utc(m.e.ts)}`;
      return `<button type="button" class="tl-mk ${m.cls}${state.sel === m.key ? " sel" : ""}" style="left:${m.left.toFixed(3)}%" data-k="${esc(m.key)}" aria-label="${esc(label)}" title="${esc(label)}"><span class="tl-dot">${m.glyph}</span></button>`;
    }).join("");
    return `<div class="tl-row${r.child ? " child" : ""}">
      <div class="tl-lab"><div class="tl-name" title="${esc(r.id)}">${esc(r.label)}</div><div class="tl-sub">${sub}</div></div>
      <div class="tl-track">${bands}${marks}</div></div>`;
  }

  function detailHtml(e, labels) {
    if (!e) return `<p class="empty">Select a mark to see its record.</p>`;
    const name = (id) => id ? `${labels[id] || shortId(id)}` : "—";
    const rows = [["Agent", name(e.agent_id)]];
    let note = "";
    if (e.kind === "pause") {
      rows.push(["From", utc(e.ts)], ["To", e.end ? utc(e.end) : "still paused at the end of the window"], ["Reason", e.reason || "—"], ["Lifted by", e.lifted_by || "—"]);
      note = "Governed writes were held for the whole band.";
    } else if (e.kind === "dialectic") {
      rows.push(["Opened", utc(e.ts)], ["Closed", e.end ? utc(e.end) : "still open"], ["Reviewer", name(e.reviewer_agent_id)], ["Status", e.status || "—"], ["Phase", e.phase || "—"]);
    } else {
      rows.push(["Time", utc(e.ts)]);
      if (e.kind === "checkin") {
        const own = e.authored_by === "agent";
        rows.push(["Written by", own ? "The agent (sync_state)" : "A hook, not the agent"], ["Action", e.action || "—"], ["Verdict", e.verdict || "—"],
          ["Risk", typeof e.risk_score === "number" ? e.risk_score.toFixed(2) : "—"]);
        if (own) {
          rows.push(["Report", typeof e.report_excerpt === "string" && e.report_excerpt ? e.report_excerpt : "Report text not retained for this check-in."]);
        } else {
          note = "Recorded automatically. It is evidence about the agent, not a claim the agent made.";
        }
      } else if (e.kind === "outcome") {
        rows.push(["Type", e.outcome_type || "—"], ["Result", e.is_bad ? "failed" : "passed"],
          ["Reported confidence", typeof e.reported_confidence === "number" ? e.reported_confidence.toFixed(2) : "none reported"],
          ["Verified by", String(e.verification_source || "—").replace(/_/g, " ")]);
        if (typeof e.reported_confidence === "number") {
          note = e.is_bad
            ? `The agent reported ${e.reported_confidence.toFixed(2)} confidence and the outcome failed.`
            : "The outcome confirmed the agent's report.";
        }
      } else if (e.kind === "knowledge") {
        rows.push(["Summary", e.summary || "—"], ["Type", e.discovery_type || "—"]);
      } else if (e.kind === "lineage") {
        rows.push(["Subagent", name(e.agent_id)], ["Started by", name(e.parent_agent_id)]);
        rows[0] = ["Agent", name(e.parent_agent_id)];
      } else if (e.kind === "lifecycle") {
        rows.push(["Event", String(e.event_type || "—").replace(/_/g, " ")], ["Reason", e.reason || "—"]);
      }
    }
    return `<div class="panel-head" style="margin-bottom:var(--space-3)"><h2>${esc(title(e))}</h2></div>
      ${note ? `<p style="color:var(--ink-2);margin-bottom:var(--space-3)">${esc(note)}</p>` : ""}
      <dl class="tl-detail">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`;
  }

  function skeleton() {
    const mount = $("#tl-mount");
    mount.innerHTML = `
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:var(--space-3)">
        <span id="tl-wins" style="display:flex;gap:6px"></span>
        <button type="button" class="theme-toggle" id="tl-sub" aria-pressed="true">hook observations</button>
        <label class="fresh" for="tl-filter" style="margin-left:var(--space-2)">filter</label>
        <input id="tl-filter" type="search" placeholder="agent label or id" autocomplete="off"
          style="font:inherit;font-size:var(--text-sm);padding:var(--space-1) var(--space-3);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-pill);background:var(--surface);color:var(--ink);min-width:200px">
        <span class="spring"></span><span id="tl-src" class="src-badge"></span>
      </div>
      <div id="tl-counts" class="fresh" style="margin-bottom:var(--space-2)"></div>
      <div id="tl-note"></div>
      <div class="panel" style="padding:var(--space-2) 0 var(--space-3)"><div class="tl-scroll"><div class="tl-grid" id="tl-grid"></div></div>
        <div id="tl-more" style="padding:var(--space-3) var(--space-5) 0"></div></div>
      <div class="tl-legend" style="margin:var(--space-3) 0 var(--space-5)">
        <span><span class="tl-mk checkin proceed"><span class="tl-dot"></span></span>proceed</span>
        <span><span class="tl-mk checkin guide"><span class="tl-dot"></span></span>guide</span>
        <span><span class="tl-mk checkin pause"><span class="tl-dot"></span></span>pause</span>
        <span><span class="tl-mk checkin substrate"><span class="tl-dot"></span></span>hook observation</span>
        <span><span class="tl-mk outcome"><span class="tl-dot">✓</span></span>outcome passed</span>
        <span><span class="tl-mk outcome bad"><span class="tl-dot">✕</span></span>outcome failed</span>
        <span><span class="tl-mk knowledge"><span class="tl-dot"></span></span>finding shared</span>
        <span><span class="tl-mk lineage"><span class="tl-dot"></span></span>started a subagent</span>
        <span><span class="tl-mk lifecycle"><span class="tl-dot"></span></span>lifecycle</span>
        <span><span class="tl-band pause"></span>paused</span>
        <span><span class="tl-band dialectic"></span>dialectic review</span>
      </div>
      <div class="panel" id="tl-detail" style="padding:var(--space-5)"></div>`;
    $("#tl-filter").addEventListener("input", (ev) => { state.filter = ev.target.value; body(); });
    $("#tl-sub").onclick = () => { state.includeSubstrate = !state.includeSubstrate; state.sel = null; load(); };
    $("#tl-grid").addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-k]");
      if (!b) return;
      state.sel = b.dataset.k;
      body();
    });
    built = true;
  }

  function body() {
    if (!MODEL) return;
    $("#tl-wins").innerHTML = WINDOWS.map((h) => `<button type="button" class="theme-toggle tl-win" data-h="${h}" aria-pressed="${h === state.hours}" style="${h === state.hours ? "border-color:var(--accent);color:var(--accent)" : ""}">${h}h</button>`).join("");
    document.querySelectorAll(".tl-win").forEach((b) => b.onclick = () => { state.hours = Number(b.dataset.h); state.sel = null; load(); });
    const subBtn = $("#tl-sub");
    subBtn.setAttribute("aria-pressed", String(state.includeSubstrate));
    subBtn.style.cssText = state.includeSubstrate ? "border-color:var(--accent);color:var(--accent)" : "";
    const src = $("#tl-src");
    src.className = "src-badge " + MODEL.source;
    src.textContent = MODEL.source;

    const grid = $("#tl-grid");
    const more = $("#tl-more");
    const note = $("#tl-note");
    if (MODEL.source === "unavailable") {
      $("#tl-counts").textContent = "";
      note.innerHTML = "";
      grid.innerHTML = `<p class="empty" style="padding:var(--space-4) var(--space-5)">Server not answering — timeline unavailable.</p>`;
      more.innerHTML = "";
      $("#tl-detail").innerHTML = detailHtml(null, {});
      return;
    }
    const view = buildRows(MODEL);
    const { matched, shown } = visibleRows(view.rows);
    const c = MODEL.counts || {};
    $("#tl-counts").textContent = `Last ${MODEL.hours}h · ${view.rows.length} agent${view.rows.length === 1 ? "" : "s"} · ${c.checkin || 0} check-ins · ${c.outcome || 0} outcomes · ${c.dialectic || 0} dialectic sessions · times UTC`;
    note.innerHTML = (MODEL.truncated || []).length
      ? `<div class="attn-band calm" style="margin-bottom:var(--space-3)"><span class="glyph">○</span><span>Only the newest ${esc(MODEL.limitPerKind || "")} rows of ${MODEL.truncated.map(esc).join(", ")} fit this window; earlier ones are not drawn. Choose a shorter window to see them all.</span></div>`
      : "";
    const ticks = [0, 1, 2, 3, 4, 5, 6].map((i) => {
      const at = view.start + (i / 6) * (view.end - view.start);
      return `<span class="tl-tick" style="left:${((i / 6) * 100).toFixed(3)}%">${i === 6 ? "now" : hhmm(at)}</span>`;
    }).join("");
    const axis = `<div class="tl-row tl-axis" style="border-top:0"><div class="tl-lab"><span class="eyebrow" style="margin:0">Agent</span></div><div class="tl-track">${ticks}</div></div>`;
    grid.innerHTML = view.rows.length
      ? axis + (shown.length ? shown.map((r) => rowHtml(r, view.labels)).join("") : `<p class="empty" style="padding:var(--space-4) var(--space-5)">No agent matches “${esc(state.filter)}”.</p>`)
      : `<p class="empty" style="padding:var(--space-4) var(--space-5)">No agent events in the last ${MODEL.hours}h.</p>`;
    more.innerHTML = matched.length > ROW_LIMIT
      ? `<button type="button" class="theme-toggle" id="tl-all">${state.showAll ? `show the ${ROW_LIMIT} most recent` : `show all ${matched.length} agents`}</button> <span class="fresh">${shown.length} of ${matched.length} shown, most recent first</span>`
      : "";
    const all = $("#tl-all");
    if (all) all.onclick = () => { state.showAll = !state.showAll; body(); };
    $("#tl-detail").innerHTML = detailHtml(BY_KEY[state.sel], view.labels);
  }

  async function load() {
    const r = await DATA.timeline({ hours: state.hours, includeSubstrate: state.includeSubstrate });
    MODEL = Object.assign({}, r.data, { source: r.source });
    if (!built || !$("#tl-filter")) skeleton();
    body();
  }

  // The live tick: refetch short windows only (see AUTO_MAX_HOURS).
  function refresh() {
    if (state.hours > AUTO_MAX_HOURS) return Promise.resolve();
    return load();
  }

  window.Timeline = { load, refresh };
})();
