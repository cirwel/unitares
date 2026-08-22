/*
 * Residents section — per-resident detail panels.
 * Consolidates the old watcher.js / sentinel.js / vigil.js / system-health.js
 * panels into one section: Watcher findings funnel, Sentinel findings by
 * severity/class + recent stream, and a uniform card for each of the six
 * residents the Overview fleet strip lists, so a quiet one never reads as
 * absent. System Health closes the pane full-width.
 *
 * Every card is anchored on that resident's /v1/residents row — status pip,
 * cadence-aware timing, coherence, verdict, check-in count, risk, EISV and
 * recent KG writes. Watcher, Sentinel and Vigil keep their own summary
 * endpoints for findings/cycle detail, but no longer take liveness from a
 * hardcoded "healthy" literal, and Vigil no longer reports the broadcaster
 * ring's nulls as real zeroes. Composes kit; reads DATA.residentPanels().
 */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const SEV = { high: "var(--danger)", medium: "var(--warn)", low: "var(--muted)", info: "var(--eisv-c)" };
  function relTime(iso) { const ms = Date.now() - Date.parse(iso); if (isNaN(ms)) return ""; const h = ms / 3.6e6; return h < 1 ? Math.round(ms / 6e4) + "m" : h < 24 ? Math.round(h) + "h" : Math.round(h / 24) + "d"; }
  function ago(sec) { return sec == null ? "—" : sec < 90 ? Math.round(sec) + "s" : sec < 5400 ? Math.round(sec / 60) + "m" : (sec / 3600).toFixed(1) + "h"; }

  function head(name, status, sub) {
    // One predicate (DATA.residentLiveness). `status` here is the server's own
    // vocabulary — healthy | silent | paused | archived | unknown; "dark" was a
    // client-only invention the server never emits. Every panel now passes a
    // real status off its /v1/residents row; no caller passes a literal.
    // "unknown" gets its own muted pip: residentLiveness only calls
    // silent/paused/archived "down", so an unknown resident would otherwise
    // read green — the same false-reassurance the hardcoded literal gave.
    const down = DATA.residentLiveness({ status, coherence: 0 }) === "down";
    const color = status === "silent" ? "var(--warn)"
      : down ? "var(--faint)"
      : status === "unknown" ? "var(--muted)"
      : "var(--ok)";
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

  // ---- shared resident-row helpers -----------------------------------------
  // Every panel now carries its /v1/residents row (`.resident` on the three
  // that have their own summary endpoint, the row itself for the other three),
  // so liveness, cadence and check-in count are read the same way everywhere.
  const isOverdue = (r) => !!(r && typeof r.silence === "number" && r.silence > (r.silenceThreshold || 3600));
  // The server already emits "silent" past threshold; recomputing here keeps a
  // stale row from reading green while its own numbers say otherwise.
  const statusOf = (r) => (r ? (isOverdue(r) ? "silent" : (r.status || "unknown")) : "unknown");
  // Silence inside a resident's own threshold is steady-state, not an alarm —
  // Chronicler is daily and Watcher is event-driven with a 48h threshold.
  function timingOf(r, fallback) {
    if (!r || typeof r.silence !== "number") return fallback || "—";
    const thr = r.silenceThreshold || 3600;
    return r.silence > thr ? "overdue " + ago(r.silence - thr) : "ran " + ago(r.silence) + " ago";
  }
  const num = (v, d) => (v == null ? "—" : v.toFixed(d == null ? 2 : d));
  const count = (v) => (v == null ? "—" : v.toLocaleString());

  const eisvLine = (e) => e && e.E != null
    ? `<div style="margin-top:var(--space-4);display:flex;gap:var(--space-5);font-family:var(--font-mono);font-size:var(--text-sm);color:var(--ink-2)">
         <span>E ${e.E.toFixed(2)}</span><span>I ${e.I.toFixed(2)}</span><span>S ${e.S.toFixed(2)}</span><span>V ${e.V.toFixed(2)}</span></div>` : "";

  // Recent KG writes — the one thing a quiet resident leaves behind. Vigil's
  // groundskeeper deltas and Chronicler's daily rollups ARE their visible
  // output, so a card that drops them reads as an idle resident.
  function writesList(recent, limit) {
    const rows = (recent || []).slice(0, limit || 3);
    if (!rows.length) return "";
    return `<div class="eyebrow" style="margin:var(--space-4) 0 0">recent writes</div>` + rows.map((w) =>
      `<div style="display:flex;gap:var(--space-3);align-items:baseline;font-size:var(--text-sm);padding:var(--space-2) 0;border-top:var(--hairline) solid var(--line)">
         ${w.type ? `<span class="tag" style="flex:none">${esc(w.type)}</span>` : ""}
         <span style="color:var(--ink-2);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(w.summary)}">${esc(w.summary)}</span>
         <span class="fresh" style="flex:none">${relTime(w.timestamp)} ago</span></div>`).join("");
  }
  // Coherence / verdict / check-ins / risk — the four fields every resident row
  // carries, so no card is reduced to a name and a timestamp.
  const coreStats = (r) => statRow([
    stat("coherence", num(r.coherence)),
    stat("verdict", r.verdict || "—", r.verdict ? "var(--ok)" : "var(--muted)"),
    stat("check-ins", count(r.updates)),
    stat("risk", num(r.risk), "var(--muted)"),
  ]);

  function watcher(w) {
    if (!w) return "";
    const r = w.resident || null;
    const s = w.byStatus || {}, total = w.total || 0;
    const seg = (n, color, t) => n ? `<div title="${t}: ${n}" style="width:${(n / total) * 100}%;background:${color};height:100%"></div>` : "";
    const pats = (w.patterns || []).slice(0, 4).map((p) =>
      `<div style="display:flex;gap:var(--space-3);align-items:center;font-size:var(--text-sm)">
         <span class="tag" style="font-family:var(--font-mono)">${esc(p.p)}</span>
         <span style="color:var(--ok)">${p.confirmed || 0}✓</span><span style="color:var(--muted)">${p.dismissed || 0}✗</span>
         <span class="spring"></span><span class="fresh">${p.ratio == null ? "—" : "dismiss " + Math.round(p.ratio * 100) + "%"}</span></div>`).join("");
    // aged_out is a real terminal status the funnel used to drop, so the bar
    // under-filled by that share and the segments didn't sum to `findings`.
    return `<div class="panel">${head("Watcher", statusOf(r), "diagnostic · event-driven · " + timingOf(r))}
      ${statRow([stat("findings", total), stat("confirmed", s.confirmed || 0, "var(--ok)"), stat("dismissed", s.dismissed || 0, "var(--muted)"), stat("aged out", s.aged_out || 0, "var(--faint)"), stat("open high", (w.openSev || {}).high || 0, "var(--danger)")])}
      <div class="track" style="height:8px;display:flex;gap:1px;margin:var(--space-4) 0 var(--space-2)">
        ${seg(s.confirmed, "var(--ok)", "confirmed")}${seg(s.surfaced, "var(--warn)", "surfaced")}${seg(s.dismissed, "var(--faint)", "dismissed")}${seg(s.aged_out, "var(--line)", "aged out")}</div>
      <div class="legend" style="margin-bottom:var(--space-4)"><span><i style="background:var(--ok)"></i>confirmed</span><span><i style="background:var(--warn)"></i>surfaced</span><span><i style="background:var(--faint)"></i>dismissed</span><span><i style="background:var(--line)"></i>aged out</span></div>
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
    return `<div class="panel">${head("Sentinel", statusOf(sn.resident || null), "analytical · fleet monitor · " + timingOf(sn.resident))}
      ${statRow([stat("findings", sn.total || 0), stat("high", (sn.bySeverity || {}).high || 0, "var(--danger)"), stat("medium", (sn.bySeverity || {}).medium || 0, "var(--warn)")])}
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:var(--space-4) 0">${classes}</div>
      <div class="eyebrow" style="margin-bottom:0">recent findings</div>${recent}</div>`;
  }

  // /v1/vigil/summary derives its cycle stats from the broadcaster's in-memory
  // event ring, but Vigil checks in through the in-process agent_state path,
  // which never reaches that ring. So cycles24h, avgCoherence and lastVerdict
  // come back null while Vigil is running perfectly well, and the old panel
  // rendered that as "0 cycles", a fabricated "0.00 avg coh" (null || 0) and
  // "— verdict" — a healthy resident shown as a dead one. Read those fields
  // from the durable /v1/residents row and keep the ring-derived cycle count
  // only when the ring actually has cycles. `writesWindow` is dropped: it was
  // the endpoint's own `limit`, not a count, so it read a constant 30.
  function vigil(v) {
    if (!v) return "";
    const r = v.resident || {};
    const coh = r.coherence != null ? r.coherence : v.avgCoherence;
    const verdict = r.verdict || v.lastVerdict;
    const e = (r.eisv && r.eisv.E != null) ? r.eisv : (v.eisv || {});
    const stats = [
      stat("coherence", num(coh)),
      stat("verdict", verdict || "—", verdict ? "var(--ok)" : "var(--muted)"),
      stat("check-ins", count(r.updates)),
      stat("risk", num(r.risk), "var(--muted)"),
    ];
    if (v.cycles24h) stats.push(stat("cycles 24h", v.cycles24h));
    const fallbackTiming = v.lastCycleAgeS != null ? "last cycle " + ago(v.lastCycleAgeS) + " ago" : "—";
    return `<div class="panel" style="${isOverdue(r) ? "border-left:2px solid var(--warn)" : ""}">
      ${head("Vigil", statusOf(v.resident || null), "janitorial · 30min cron · " + timingOf(r, fallbackTiming))}
      ${statRow(stats)}${eisvLine(e)}${writesList(r.recent)}</div>`;
  }

  // Check-in card for residents whose whole dashboard story is their
  // /v1/residents row (Steward, Lumen): liveness vs their own cadence
  // threshold, coherence/writes/risk, EISV line. Same overdue discipline as
  // Chronicler — silence within threshold is steady-state, not an alarm.
  function residentCard(name, desc, r) {
    if (!r) return "";
    return `<div class="panel" style="${isOverdue(r) ? "border-left:2px solid var(--warn)" : ""}">
      ${head(name, statusOf(r), desc + " · " + timingOf(r))}
      ${coreStats(r)}${eisvLine(r.eisv)}${writesList(r.recent)}</div>`;
  }

  // Map a per-check status string to a pip color. Anything other than the
  // known-good/known-soft states reads as a hard failure.
  function healthColor(st) {
    return st === "healthy" ? "var(--ok)"
      : st === "warning" ? "var(--warn)"
      : st === "deprecated" ? "var(--muted)"
      : "var(--danger)"; // error / unavailable / unknown
  }
  // Pick the single most operator-useful field a check exposes, so each row
  // says something beyond its name. Order = most-to-least diagnostic.
  function healthDetail(c) {
    if (!c || typeof c !== "object") return "";
    if (c.latency_ms != null) return Math.round(c.latency_ms) + "ms";
    if (c.note) return String(c.note);
    if (c.init_error) return String(c.init_error);
    if (c.pending_updates) return c.pending_updates + " pending";
    if (c.mode) return String(c.mode);
    if (c.backend) return String(c.backend);
    return "";
  }

  function health(h) {
    if (!h) return "";
    const ch = h.checks || {};
    const items = h.items || {};
    const op = h.operator || {};
    // Sort non-healthy checks to the top so a degraded one is never buried.
    const rank = (st) => (st === "healthy" ? 2 : st === "deprecated" ? 1 : 0);
    const rows = Object.keys(items).sort((a, b) => rank(items[a] && items[a].status) - rank(items[b] && items[b].status))
      .map((name) => {
        const c = items[name] || {}, st = c.status || "unknown", det = healthDetail(c);
        // The detail span is the flexible middle element: it fills the space
        // (pushing the status label right, in place of a spring) and truncates
        // with an ellipsis when a check's note is long, so a verbose note like
        // identity_continuity's can't overflow the card. Full text on hover.
        return `<div style="display:flex;gap:var(--space-3);align-items:baseline;font-size:var(--text-sm);padding:var(--space-1) 0">
          <span class="dot-pip" style="background:${healthColor(st)};flex:none;align-self:center"></span>
          <span style="font-family:var(--font-mono);color:var(--ink-2);flex:none">${esc(name)}</span>
          <span class="fresh"${det ? ` title="${esc(det)}"` : ""} style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right">${esc(det)}</span>
          <span style="color:${healthColor(st)};flex:none;text-transform:uppercase;font-size:var(--text-xs);letter-spacing:var(--tracking-label)">${esc(st)}</span></div>`;
      }).join("");
    // Operator banner: only when something is actually failing/degraded.
    const fails = (op.failing_checks || []).concat(op.degraded_checks || []);
    const banner = fails.length
      ? `<div style="margin:var(--space-3) 0;padding:var(--space-2) var(--space-3);border-left:2px solid var(--warn);background:var(--surface-2);font-size:var(--text-sm);color:var(--ink-2)">
           <b>${fails.length}</b> need${fails.length === 1 ? "s" : ""} attention: <span style="font-family:var(--font-mono)">${esc(fails.join(", "))}</span>${op.first_action && op.first_action !== "No action needed." ? ` — ${esc(op.first_action)}` : ""}</div>`
      : "";
    const allOk = h.status === "healthy" && !fails.length;
    return `<div class="panel">${head("System Health", allOk ? "healthy" : "silent", "v" + esc(h.version || "") + " · continuity " + esc(h.continuity || "—"))}
      ${statRow([stat("checks ok", ch.healthy || 0, "var(--ok)"), stat("warnings", ch.warning || 0, (ch.warning ? "var(--warn)" : "var(--muted)")), stat("errors", ch.error || 0, (ch.error ? "var(--danger)" : "var(--muted)")), stat("breaker trips 24h", (h.breakers || {}).governance || 0, "var(--ok)")])}
      ${banner}
      ${rows ? `<div style="margin-top:var(--space-3)">${rows}</div>` : ""}</div>`;
  }

  async function load() {
    const r = await DATA.residentPanels();
    const d = r.data || {};
    document.querySelector("#res-mount").innerHTML =
      `<div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4)">
         <span class="eyebrow" style="margin:0">Always-on fleet</span><span class="spring"></span><span class="src-badge ${r.source}">${r.source}</span></div>
       <div class="split-2" style="gap:var(--space-4)">
         ${watcher(d.watcher)}${sentinel(d.sentinel)}${vigil(d.vigil)}
         ${residentCard("Steward", "custodial · 5min in-process cycle · Pi→Mac EISV sync", d.steward)}
         ${residentCard("Chronicler", "longitudinal · daily", d.chronicler)}
         ${residentCard("Lumen", "embodied · Raspberry Pi", d.lumen)}
       </div>
       <div style="margin-top:var(--space-4)">${health(d.health)}</div>`;
  }
  window.Residents = { load };
})();
