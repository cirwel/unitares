/*
 * Residents section — per-resident detail panels.
 * Consolidates the old watcher.js / sentinel.js / vigil.js / system-health.js
 * panels into one section: Watcher findings funnel, Sentinel findings by
 * severity/class + recent stream, Vigil cycles/writes + EISV, Chronicler
 * silence note, and System Health. Composes kit; reads DATA.residentPanels().
 */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const SEV = { high: "var(--danger)", medium: "var(--warn)", low: "var(--muted)", info: "var(--eisv-c)" };
  function relTime(iso) { const ms = Date.now() - Date.parse(iso); if (isNaN(ms)) return ""; const h = ms / 3.6e6; return h < 1 ? Math.round(ms / 6e4) + "m" : h < 24 ? Math.round(h) + "h" : Math.round(h / 24) + "d"; }
  function ago(sec) { return sec == null ? "—" : sec < 90 ? Math.round(sec) + "s" : sec < 5400 ? Math.round(sec / 60) + "m" : (sec / 3600).toFixed(1) + "h"; }

  function head(name, status, sub) {
    const color = status === "silent" ? "var(--warn)" : status === "dark" ? "var(--faint)" : "var(--ok)";
    return `<div class="panel-head" style="margin-bottom:var(--space-4)">
      <span class="dot-pip" style="background:${color}"></span>
      <h2 style="font-family:var(--font-display);font-size:var(--text-lg)">${name}</h2>
      <span class="spring"></span><span class="fresh">${esc(sub || "")}</span></div>`;
  }
  function stat(label, val, color) {
    return `<div><div style="font-family:var(--font-mono);font-size:var(--text-lg);color:${color || "var(--ink)"};line-height:1">${val}</div>
      <div style="font-size:var(--text-xs);color:var(--muted);text-transform:uppercase;letter-spacing:var(--tracking-label)">${label}</div></div>`;
  }
  const statRow = (items) => `<div style="display:flex;gap:var(--space-6);flex-wrap:wrap">${items.join("")}</div>`;

  function watcher(w) {
    if (!w) return "";
    const s = w.byStatus || {}, total = w.total || 0;
    const seg = (n, color, t) => n ? `<div title="${t}: ${n}" style="width:${(n / total) * 100}%;background:${color};height:100%"></div>` : "";
    const pats = (w.patterns || []).slice(0, 4).map((p) =>
      `<div style="display:flex;gap:var(--space-3);align-items:center;font-size:var(--text-sm)">
         <span class="tag" style="font-family:var(--font-mono)">${esc(p.p)}</span>
         <span style="color:var(--ok)">${p.confirmed || 0}✓</span><span style="color:var(--muted)">${p.dismissed || 0}✗</span>
         <span class="spring"></span><span class="fresh">${p.ratio == null ? "—" : "dismiss " + Math.round(p.ratio * 100) + "%"}</span></div>`).join("");
    return `<div class="panel">${head("Watcher", "healthy", "diagnostic · event-driven")}
      ${statRow([stat("findings", total), stat("confirmed", s.confirmed || 0, "var(--ok)"), stat("dismissed", s.dismissed || 0, "var(--muted)"), stat("open high", (w.openSev || {}).high || 0, "var(--danger)")])}
      <div class="track" style="height:8px;display:flex;gap:1px;margin:var(--space-4) 0 var(--space-2)">
        ${seg(s.confirmed, "var(--ok)", "confirmed")}${seg(s.surfaced, "var(--warn)", "surfaced")}${seg(s.dismissed, "var(--faint)", "dismissed")}</div>
      <div class="legend" style="margin-bottom:var(--space-4)"><span><i style="background:var(--ok)"></i>confirmed</span><span><i style="background:var(--warn)"></i>surfaced</span><span><i style="background:var(--faint)"></i>dismissed</span></div>
      <div style="display:flex;flex-direction:column;gap:var(--space-2)">${pats}</div></div>`;
  }

  function sentinel(sn) {
    if (!sn) return "";
    const classes = (sn.byClass || []).map((c) => `<span class="tag">${esc(c.c || "?")} ${c.n}</span>`).join(" ");
    const recent = (sn.recent || []).slice(0, 3).map((r) =>
      `<div style="display:flex;gap:var(--space-3);align-items:baseline;font-size:var(--text-sm);padding:var(--space-2) 0;border-top:var(--hairline) solid var(--line)">
         <span style="color:${SEV[r.severity] || "var(--muted)"};flex:none">●</span>
         ${r.vclass ? `<span class="tag" style="flex:none">${esc(r.vclass)}</span>` : ""}
         <span style="color:var(--ink-2);flex:1;min-width:0">${esc(r.message)}</span>
         <span class="fresh" style="flex:none">${relTime(r.ts)} ago</span></div>`).join("");
    return `<div class="panel">${head("Sentinel", "healthy", "analytical · fleet monitor")}
      ${statRow([stat("findings", sn.total || 0), stat("high", (sn.bySeverity || {}).high || 0, "var(--danger)"), stat("medium", (sn.bySeverity || {}).medium || 0, "var(--warn)")])}
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:var(--space-4) 0">${classes}</div>
      <div class="eyebrow" style="margin-bottom:0">recent findings</div>${recent}</div>`;
  }

  function vigil(v) {
    if (!v) return "";
    const e = v.eisv || {};
    return `<div class="panel">${head("Vigil", "healthy", "janitorial · 30min cron · last " + ago(v.lastCycleAgeS) + " ago")}
      ${statRow([stat("cycles 24h", v.cycles24h || 0), stat("writes", v.writesWindow || 0), stat("avg coh", (v.avgCoherence || 0).toFixed(2)), stat("verdict", v.lastVerdict || "—", "var(--ok)")])}
      ${e.E != null ? `<div style="margin-top:var(--space-4);display:flex;gap:var(--space-5);font-family:var(--font-mono);font-size:var(--text-sm);color:var(--ink-2)">
        <span>E ${e.E.toFixed(2)}</span><span>I ${e.I.toFixed(2)}</span><span>S ${e.S.toFixed(2)}</span><span>V ${e.V.toFixed(2)}</span></div>` : ""}</div>`;
  }

  function chronicler(c) {
    if (!c) return "";
    // Daily resident: silence within its (48h) threshold is steady-state, not an
    // alarm. Only genuinely past-threshold is "overdue".
    let timing, overdue;
    if (typeof c.silence === "number") {
      const thr = c.silenceThreshold || 86400;
      overdue = c.silence > thr;
      timing = overdue ? "overdue " + ago(c.silence - thr) : "ran " + ago(c.silence) + " ago";
    } else {
      overdue = false;
      timing = "ran " + (c.silenceH != null ? c.silenceH + "h" : "—") + " ago";
    }
    const e = c.eisv || {};
    const badge = overdue ? `<span class="tag warn">${timing}</span>` : `<span class="tag">daily · ${timing}</span>`;
    return `<div class="panel" style="${overdue ? "border-left:2px solid var(--warn)" : ""}">${head("Chronicler", overdue ? "silent" : "healthy", "longitudinal · daily")}
      <div style="color:var(--ink-2);font-size:var(--text-sm)">${badge}${overdue && c.note ? " " + esc(c.note) : ""}</div>
      ${e.E != null ? `<div style="margin-top:var(--space-3);display:flex;gap:var(--space-5);font-family:var(--font-mono);font-size:var(--text-sm);color:var(--ink-2)"><span>E ${e.E.toFixed(2)}</span><span>I ${e.I.toFixed(2)}</span><span>S ${e.S.toFixed(2)}</span><span>V ${e.V.toFixed(2)}</span></div>` : ""}</div>`;
  }

  function health(h) {
    if (!h) return "";
    const ch = h.checks || {};
    return `<div class="panel">${head("System Health", h.status === "healthy" ? "healthy" : "silent", "v" + esc(h.version || "") + " · continuity " + esc(h.continuity || "—"))}
      ${statRow([stat("checks ok", ch.healthy || 0, "var(--ok)"), stat("warnings", ch.warning || 0, (ch.warning ? "var(--warn)" : "var(--muted)")), stat("breaker trips 24h", (h.breakers || {}).governance || 0, "var(--ok)"), stat("calibration", h.calibration || "—", "var(--ok)")])}</div>`;
  }

  async function load() {
    const r = await DATA.residentPanels();
    const d = r.data || {};
    document.querySelector("#res-mount").innerHTML =
      `<div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4)">
         <span class="eyebrow" style="margin:0">Always-on fleet</span><span class="spring"></span><span class="src-badge ${r.source}">${r.source}</span></div>
       <div class="split-2" style="gap:var(--space-4)">
         ${watcher(d.watcher)}${sentinel(d.sentinel)}${vigil(d.vigil)}${health(d.health)}
       </div>
       <div style="margin-top:var(--space-4)">${chronicler(d.chronicler)}</div>`;
  }
  window.Residents = { load };
})();
