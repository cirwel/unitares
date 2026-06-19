/*
 * Discoveries section — knowledge-graph entries.
 * Rebuilt from old discoveries.js: lifecycle bar + type legend (clickable
 * filter) + search/type/time filters + cards (type badge, agent, date,
 * summary w/ highlight, tags, expandable details, staleness). Composes kit;
 * reads DATA.discoveries() (live-or-snapshot).
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);

  const TYPE_ORDER = ["insight", "improvement", "bug_found", "pattern", "question", "answer", "analysis", "note", "exploration"];
  const STATUS_COLOR = { open: "var(--ok)", resolved: "var(--eisv-c)", archived: "var(--faint)", superseded: "var(--warn)", closed: "var(--muted)" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const label = (t) => t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  function relTime(iso) {
    const ms = Date.now() - Date.parse(iso);
    if (isNaN(ms)) return "";
    const h = ms / 3.6e6, d = h / 24;
    if (h < 1) return Math.max(1, Math.round(ms / 6e4)) + "m ago";
    if (h < 24) return Math.round(h) + "h ago";
    if (d < 60) return Math.round(d) + "d ago";
    return Math.round(d / 30) + "mo ago";
  }
  function dateLabel(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return String(iso || "").slice(0, 10);
    return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function highlight(text, q) {
    text = esc(text);
    if (!q) return text;
    try { return text.replace(new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig"), "<mark>$1</mark>"); }
    catch (_) { return text; }
  }

  let MODEL = { list: [], byType: {}, byStatus: {}, total: 0, source: "snapshot" };
  let typeFilter = "all", timeFilter = "all";

  function card(d, q) {
    const tags = (d.tags || []).slice(0, 5).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ");
    const details = (d.details || "").trim();
    const stale = d.stale ? `<span class="tag warn" title="aged / still open — verify before acting">stale</span>` : "";
    return `<div class="panel" style="padding:var(--space-5)">
      <div style="display:flex;gap:var(--space-3);align-items:center;flex-wrap:wrap;margin-bottom:var(--space-2)">
        <span class="tag" style="border-color:var(--line-2)">${label(d.type)}</span>
        <span class="tag" style="color:${STATUS_COLOR[d.status] || "var(--muted)"}">${esc(d.status)}</span>
        ${stale}
        <span class="spring"></span>
        <span class="fresh">${esc(d.by || "—")} · ${dateLabel(d.id)} <span style="color:var(--faint)">(${relTime(d.id)})</span></span>
      </div>
      <div style="color:var(--ink);font-size:var(--text-base);line-height:var(--leading-body);margin-bottom:${details || tags ? "var(--space-3)" : "0"}">${highlight(d.summary, q)}</div>
      ${tags ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:${details ? "var(--space-3)" : "0"}">${tags}</div>` : ""}
      ${details ? `<details><summary style="cursor:pointer;color:var(--muted);font-size:var(--text-sm)">details</summary>
        <div style="margin-top:var(--space-2);color:var(--ink-2);font-size:var(--text-sm);white-space:pre-wrap">${esc(details)}</div></details>` : ""}
    </div>`;
  }

  function render() {
    const q = ($("#dsc-search") && $("#dsc-search").value || "").toLowerCase().trim();
    let rows = MODEL.list.slice();
    if (typeFilter !== "all") rows = rows.filter((d) => (d.type || "note") === typeFilter);
    if (timeFilter !== "all") {
      const cut = Date.now() - { "24h": 864e5, "7d": 6048e5, "30d": 2592e6 }[timeFilter];
      rows = rows.filter((d) => Date.parse(d.id) >= cut);
    }
    if (q) rows = rows.filter((d) => ((d.summary || "") + " " + (d.details || "") + " " + (d.tags || []).join(" ")).toLowerCase().includes(q));
    rows.sort((a, b) => Date.parse(b.id || 0) - Date.parse(a.id || 0));

    // lifecycle bar
    const bs = MODEL.byStatus || {};
    const totalS = Object.values(bs).reduce((a, n) => a + n, 0) || 1;
    const barOrder = ["open", "resolved", "archived", "superseded", "closed"];
    const bar = barOrder.filter((k) => bs[k]).map((k) =>
      `<div title="${k}: ${bs[k]}" style="width:${(bs[k] / totalS) * 100}%;background:${STATUS_COLOR[k]};height:100%"></div>`).join("");
    const barLegend = barOrder.filter((k) => bs[k]).map((k) =>
      `<span><i style="background:${STATUS_COLOR[k]}"></i>${k} ${bs[k]}</span>`).join("");

    // type legend
    const bt = MODEL.byType || {};
    const types = Object.keys(bt).sort((a, b) => (TYPE_ORDER.indexOf(a) + 1 || 99) - (TYPE_ORDER.indexOf(b) + 1 || 99));
    const legend = ["all"].concat(types).map((t) => {
      const n = t === "all" ? MODEL.total : bt[t];
      const on = t === typeFilter;
      return `<button class="theme-toggle dsc-type" data-type="${t}" style="${on ? "border-color:var(--accent);color:var(--accent)" : ""}">${label(t)} <span style="color:var(--faint)">${n ?? ""}</span></button>`;
    }).join("");

    $("#dsc-mount").innerHTML =
      `${Object.keys(bs).length ? `<div style="margin-bottom:var(--space-4)">
         <div class="track" style="height:10px;display:flex;gap:1px">${bar}</div>
         <div class="legend" style="margin-top:var(--space-2)">${barLegend} <span class="src-badge ${MODEL.source}" style="margin-left:auto">${MODEL.source}</span></div>
       </div>` : ""}
       <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:var(--space-4)">${legend}</div>
       <div style="display:flex;gap:var(--space-3);flex-wrap:wrap;margin-bottom:var(--space-4)">
         <input id="dsc-search" placeholder="search summary · details · tags" value="${esc(q)}"
           style="flex:1;min-width:200px;padding:var(--space-2) var(--space-3);font-family:var(--font-sans);font-size:var(--text-sm);background:var(--surface);color:var(--ink);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-sm)" />
         <select id="dsc-time" class="theme-toggle">${[["all", "all time"], ["24h", "24h"], ["7d", "7 days"], ["30d", "30 days"]].map(([v, t]) => `<option value="${v}" ${v === timeFilter ? "selected" : ""}>${t}</option>`).join("")}</select>
       </div>
       <div style="display:flex;flex-direction:column;gap:var(--space-3)">
         ${rows.length ? rows.map((d) => card(d, q)).join("") : `<p class="empty">No matches. Clear filters or change search.</p>`}
       </div>`;
    wire();
  }

  function wire() {
    const s = $("#dsc-search"); if (s) { s.oninput = render; if (s.value) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }
    const t = $("#dsc-time"); if (t) t.onchange = () => { timeFilter = t.value; render(); };
    document.querySelectorAll(".dsc-type").forEach((b) => b.onclick = () => { typeFilter = b.dataset.type; render(); });
  }

  async function load() {
    const r = await DATA.discoveries();
    MODEL = { list: r.data.list || [], byType: r.data.byType || {}, byStatus: r.data.byStatus || {}, total: r.data.total || (r.data.list || []).length, source: r.source };
    render();
  }
  window.Discoveries = { load };
})();
