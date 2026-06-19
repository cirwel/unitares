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
    const list = MODEL.list.slice();
    const q = ($("#ag-search") && $("#ag-search").value || "").toLowerCase().trim();
    const statusF = $("#ag-status") ? $("#ag-status").value : "all";
    const sortF = $("#ag-sort") ? $("#ag-sort").value : "recent";
    const prodOnly = $("#ag-prod") ? $("#ag-prod").checked : false;

    let rows = list.filter((a) => {
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
      return `<tr>
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

    $("#ag-mount").innerHTML =
      `<div style="display:flex;gap:var(--space-3);flex-wrap:wrap;align-items:center;margin-bottom:var(--space-4)">
         <input id="ag-search" placeholder="search name · id · purpose · tag" value="${q.replace(/"/g, "&quot;")}"
           style="flex:1;min-width:200px;padding:var(--space-2) var(--space-3);font-family:var(--font-sans);font-size:var(--text-sm);background:var(--surface);color:var(--ink);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-sm)" />
         <select id="ag-status" class="theme-toggle">${["all", "active", "paused", "archived"].map((s) => `<option ${s === statusF ? "selected" : ""}>${s}</option>`).join("")}</select>
         <select id="ag-sort" class="theme-toggle">${[["recent", "newest"], ["name", "name"], ["coherence", "coherence"], ["risk", "risk"], ["updates", "updates"]].map(([v, t]) => `<option value="${v}" ${v === sortF ? "selected" : ""}>${t}</option>`).join("")}</select>
         <label style="font-size:var(--text-xs);color:var(--muted);display:flex;gap:6px;align-items:center"><input type="checkbox" id="ag-prod" ${prodOnly ? "checked" : ""}/> prod only</label>
       </div>
       <div style="display:flex;gap:var(--space-5);margin-bottom:var(--space-3);font-size:var(--text-xs);color:var(--muted)">
         <span><b style="color:var(--ink)">${sm.total ?? rows.length}</b> total</span>
         <span><b style="color:var(--ink)">${sm.active ?? "—"}</b> active</span>
         <span><b style="color:var(--ink)">${sm.participated ?? participated.length}</b> participated</span>
         <span><b style="color:var(--ink)">${sm.archived ?? 0}</b> archived</span>
         <span class="src-badge ${MODEL.source}">${MODEL.source}</span>
       </div>
       ${shown.length ? `<table class="tbl">${head}<tbody>${shown.map(tr).join("")}</tbody></table>` : `<p class="empty">No agents match the current filters.</p>`}
       ${moreBtn}${neverGroup}`;

    wire();
  }
  function cmp_recent(a, b) { return Date.parse(b.last || 0) - Date.parse(a.last || 0); }

  function wire() {
    const s = $("#ag-search"); if (s) s.oninput = () => { pageSize = 20; render(); };
    ["#ag-status", "#ag-sort", "#ag-prod"].forEach((id) => { const el = $(id); if (el) el.onchange = () => { pageSize = 20; render(); }; });
    const more = $("#ag-more"); if (more) more.onclick = () => { pageSize += 20; render(); };
  }

  async function load() {
    const r = await DATA.agents();
    MODEL = {
      list: r.data.list || [], summary: r.data.summary || {}, source: r.source,
      nowMs: r.source === "live" ? Date.now() : Date.parse((window.SNAPSHOT && window.SNAPSHOT.capturedAt) || 0) || Date.now(),
    };
    render();
  }

  window.Agents = { load };
})();
