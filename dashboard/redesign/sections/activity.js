/*
 * Activity section — histogram + event stream.
 * Built from old timeline.js oracle: a proceed/guide/pause activity
 * histogram over the window, plus a filterable event timeline (icon by
 * type, severity/verdict colour, violation-class badge, agent, time,
 * message). Composes kit; reads DATA.activity() (live-or-snapshot).
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // type → icon (mirrors timeline.js glyph vocabulary)
  const ICON = {
    checkin: "●", verdict: "■", lifecycle: "⚑", identity: "◆", knowledge: "✎",
    circuit_breaker: "⚡", agent_new: "+", sentinel_finding: "○", sentinel_alarm_finding: "⚡", event: "○",
  };
  const SEV_COLOR = { critical: "var(--danger)", high: "var(--danger)", medium: "var(--warn)", moderate: "var(--warn)", warning: "var(--warn)", low: "var(--muted)", info: "var(--eisv-c)" };
  const importantSev = (s) => ["critical", "high", "medium", "moderate", "warning"].includes(s);

  function relTime(iso) {
    const ms = Date.now() - Date.parse(iso); if (isNaN(ms)) return "";
    const m = ms / 6e4, h = m / 60, d = h / 24;
    if (m < 60) return Math.max(1, Math.round(m)) + "m ago";
    if (h < 24) return Math.round(h) + "h ago";
    return Math.round(d) + "d ago";
  }
  const clock = (iso) => { const t = Date.parse(iso); return isNaN(t) ? "" : new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); };

  let MODEL = { events: [], buckets: [], windowMin: 60, bucketMin: 5, source: "snapshot" };
  let filter = "all";

  function histogram() {
    const b = MODEL.buckets;
    if (!b.length) return "";
    const max = Math.max(1, ...b.map((x) => x.p + x.g + x.x));
    const bars = b.map((x) => {
      const seg = (n, color) => n ? `<div style="height:${(n / max) * 100}%;background:${color}"></div>` : "";
      return `<div style="flex:1;display:flex;flex-direction:column-reverse;height:100%;gap:1px" title="${x.p} proceed · ${x.g} guide · ${x.x} pause">
        ${seg(x.p, "var(--ok)")}${seg(x.g, "var(--warn)")}${seg(x.x, "var(--danger)")}</div>`;
    }).join("");
    const total = b.reduce((a, x) => a + x.p + x.g + x.x, 0);
    return `<div class="panel" style="padding:var(--space-5);margin-bottom:var(--space-5)">
      <div style="display:flex;align-items:baseline;gap:var(--space-3);margin-bottom:var(--space-3)">
        <span class="eyebrow" style="margin:0">Check-in activity</span>
        <span class="fresh">last ${MODEL.windowMin}m · ${MODEL.bucketMin}m buckets · ${total} check-ins</span>
        <span class="spring"></span>
        <span class="legend" style="font-size:var(--text-xs)"><span><i style="background:var(--ok)"></i>proceed</span><span><i style="background:var(--warn)"></i>guide</span><span><i style="background:var(--danger)"></i>pause</span></span>
      </div>
      <div style="display:flex;gap:3px;align-items:flex-end;height:90px">${bars}</div>
    </div>`;
  }

  function row(e) {
    const color = SEV_COLOR[e.severity] || "var(--muted)";
    const icon = ICON[e.type] || ICON.event;
    const vclass = e.vclass ? `<span class="tag" title="violation class ${esc(e.vclass)}">${esc(e.vclass)}</span>` : "";
    return `<div style="display:flex;gap:var(--space-3);align-items:baseline;padding:var(--space-2) 0;border-bottom:var(--hairline) solid var(--line)">
      <span style="color:${color};width:14px;flex:none;text-align:center">${icon}</span>
      <span class="fresh" style="width:64px;flex:none" title="${esc(e.ts)}">${clock(e.ts)}</span>
      <span style="font-weight:500;color:var(--ink);width:170px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.agent)}">${esc(e.agent || "—")}</span>
      ${vclass}
      <span style="color:var(--ink-2);flex:1;min-width:0">${esc(e.message || e.type)}</span>
      <span class="fresh" style="flex:none">${relTime(e.ts)}</span>
    </div>`;
  }

  function render() {
    let rows = MODEL.events.slice();
    if (filter === "important") rows = rows.filter((e) => importantSev(e.severity));
    else if (filter !== "all") rows = rows.filter((e) => e.type === filter);
    rows.sort((a, b) => Date.parse(b.ts || 0) - Date.parse(a.ts || 0));

    const types = Array.from(new Set(MODEL.events.map((e) => e.type)));
    const chips = [["all", "all"], ["important", "important"]].concat(types.map((t) => [t, t.replace(/_/g, " ")]))
      .map(([v, t]) => `<button class="theme-toggle act-f" data-f="${v}" style="${v === filter ? "border-color:var(--accent);color:var(--accent)" : ""}">${esc(t)}</button>`).join("");

    $("#act-mount").innerHTML =
      histogram() +
      `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:var(--space-3)">
         ${chips}<span class="spring"></span><span class="src-badge ${MODEL.source}">${MODEL.source}</span></div>
       <div class="panel" style="padding:var(--space-4) var(--space-5)">
         ${rows.length ? rows.slice(0, 50).map(row).join("") : `<p class="empty">No events in this view.</p>`}
       </div>`;
    document.querySelectorAll(".act-f").forEach((b) => b.onclick = () => { filter = b.dataset.f; render(); });
  }

  async function load() {
    const r = await DATA.activity();
    MODEL = { events: r.data.events || [], buckets: r.data.buckets || [], windowMin: r.data.windowMin || 60, bucketMin: r.data.bucketMin || 5, source: r.source };
    render();
  }
  window.Activity = { load };
})();
