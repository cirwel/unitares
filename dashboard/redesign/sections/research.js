/*
 * Research section - agent-network research-run registry.
 * Reads DATA.researchRuns() and renders the checklist that tells whether a run
 * has the minimum research shape: scenario, topology, population, metrics,
 * exogenous grounding, and artifacts.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  let MODEL = { runs: [], stats: {}, warnings: [], source: "snapshot" };
  let q = "", statusF = "all", groundingF = "all", areaF = "all";

  function relTime(iso) {
    if (!iso) return "-";
    const ms = Date.now() - Date.parse(iso);
    if (isNaN(ms)) return esc(iso);
    const h = ms / 3.6e6;
    return h < 1 ? Math.round(ms / 6e4) + "m ago" : h < 48 ? Math.round(h) + "h ago" : Math.round(h / 24) + "d ago";
  }

  function tag(text, cls) {
    return `<span class="tag ${cls || ""}">${esc(text)}</span>`;
  }

  function groundingClass(value) {
    if (value === "anchored") return "ok";
    if (value === "missing") return "danger";
    return "warn";
  }

  function checklistCell(run) {
    const c = run.rigor_checklist || {};
    const keys = ["scenario", "topology", "population", "metrics", "exogenous_grounding", "artifacts"];
    return `<div style="display:flex;gap:4px;flex-wrap:wrap;max-width:280px">`
      + keys.map((k) => tag(k.replace("exogenous_", ""), c[k] ? "ok" : "warn")).join("")
      + `</div>`;
  }

  function visible() {
    const needle = q.trim().toLowerCase();
    return MODEL.runs.filter((r) => {
      if (statusF !== "all" && r.status !== statusF) return false;
      if (groundingF !== "all" && r.grounding_status !== groundingF) return false;
      if (areaF !== "all" && !(r.research_areas || []).includes(areaF)) return false;
      if (needle) {
        const hay = [
          r.run_id, r.title, r.status, r.scenario_id, r.scenario_name,
          r.topology_kind, r.grounding_status, (r.research_areas || []).join(" "),
          (r.tags || []).join(" "),
        ].join(" ").toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }

  function render() {
    const stats = MODEL.stats || {};
    const rows = visible();
    const statuses = ["all"].concat(Object.keys(stats.by_status || {}).sort());
    const groundings = ["all"].concat(Object.keys(stats.by_grounding || {}).sort());
    const areas = ["all"].concat(Object.keys(stats.by_research_area || {}).sort());
    const sel = (id, value, items) => `<select id="${id}" class="theme-toggle">${items.map((x) => `<option value="${esc(x)}" ${x === value ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`;
    const warn = (MODEL.warnings || []).map((w) =>
      `<div class="attn-band calm" style="margin-bottom:var(--space-2)"><span class="glyph">-</span><span>${esc(w)}</span></div>`
    ).join("");

    $("research-mount").innerHTML =
      warn
      + `<div style="display:flex;align-items:center;gap:var(--space-4);flex-wrap:wrap;margin-bottom:var(--space-4);font-size:var(--text-xs);color:var(--muted)">
           <span><b style="color:var(--ink)">${stats.total || MODEL.runs.length}</b> runs</span>
           <span><b style="color:var(--ok)">${stats.rigor_complete || 0}</b> complete</span>
           <span><b style="color:var(--warn)">${stats.rigor_incomplete || 0}</b> incomplete</span>
           <span class="src-badge ${MODEL.source}">${MODEL.source}</span>
         </div>`
      + `<div style="display:flex;gap:var(--space-3);flex-wrap:wrap;align-items:center;margin-bottom:var(--space-4)">
           <input id="research-q" placeholder="search run / scenario / tag" value="${esc(q)}"
             style="flex:1;min-width:200px;padding:var(--space-2) var(--space-3);font-family:var(--font-sans);font-size:var(--text-sm);background:var(--surface);color:var(--ink);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-sm)" />
           ${sel("research-status", statusF, statuses)}
           ${sel("research-grounding", groundingF, groundings)}
           ${sel("research-area", areaF, areas)}
           <button id="research-refresh" class="theme-toggle" title="Refresh">refresh</button>
         </div>`
      + (rows.length ? `<table class="tbl"><thead><tr>
            <th>Run</th><th>Status</th><th>Scenario</th><th>Topology</th><th>Grounding</th><th>Checklist</th><th>Updated</th></tr></thead><tbody>`
        + rows.map((r) => `<tr>
              <td><div style="font-weight:500;color:var(--ink)">${esc(r.title || r.run_id)}</div>
                  <div style="font-family:var(--font-mono);font-size:var(--text-xs);color:var(--faint);margin-top:2px">${esc(r.run_id)}</div>
                  <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">${(r.research_areas || []).slice(0, 4).map((a) => tag(a)).join("")}${(r.tags || []).slice(0, 3).map((t) => tag(t)).join("")}</div></td>
              <td>${tag(r.status || "-", r.status === "completed" ? "ok" : r.status === "aborted" ? "danger" : "warn")}</td>
              <td><div style="color:var(--ink-2)">${esc(r.scenario_name || r.scenario_id || "-")}</div>
                  ${r.scenario_id ? `<div style="font-size:var(--text-xs);color:var(--faint)">${esc(r.scenario_id)}</div>` : ""}</td>
              <td>${tag(r.topology_kind || "-")}${r.population_count != null ? `<div style="font-size:var(--text-xs);color:var(--muted);margin-top:3px">${r.population_count} agents/classes</div>` : ""}</td>
              <td>${tag(r.grounding_status || "missing", groundingClass(r.grounding_status))}</td>
              <td>${checklistCell(r)}</td>
              <td style="font-size:var(--text-sm);color:var(--muted)" title="${esc(r.updated_at)}">${relTime(r.updated_at)}</td>
            </tr>`).join("")
        + `</tbody></table>`
        : `<p class="empty">No research runs match the current filters.</p>`)
      + `<div style="margin-top:var(--space-3);font-size:var(--text-xs);color:var(--faint)">showing ${rows.length} of ${MODEL.runs.length}</div>`;
    wire();
  }

  function wire() {
    const search = $("research-q");
    if (search) {
      search.oninput = () => {
        const pos = search.selectionStart;
        q = search.value;
        render();
        const next = $("research-q");
        if (next) { next.focus(); if (pos != null) next.setSelectionRange(pos, pos); }
      };
    }
    const status = $("research-status"); if (status) status.onchange = () => { statusF = status.value; render(); };
    const grounding = $("research-grounding"); if (grounding) grounding.onchange = () => { groundingF = grounding.value; render(); };
    const area = $("research-area"); if (area) area.onchange = () => { areaF = area.value; render(); };
    const refresh = $("research-refresh"); if (refresh) refresh.onclick = load;
  }

  async function load() {
    const r = await DATA.researchRuns();
    const d = r.data || {};
    MODEL = {
      runs: d.runs || [],
      stats: d.stats || {},
      warnings: d.warnings || [],
      source: r.source,
    };
    render();
  }

  window.Research = { load };
})();
