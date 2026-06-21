/*
 * Landing section — residents strip + stats grid + Pulse.
 * Composes kit primitives, reads the data layer (live-or-snapshot),
 * badges its own freshness. No fetch here; no styles here.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmtSil = (s) => s == null ? "—" : s < 90 ? s + "s" : s < 5400 ? Math.round(s / 60) + "m" : (s / 3600).toFixed(1) + "h";
  const num = (x, d = 2) => typeof x === "number" ? x.toFixed(d) : "—";

  function badge(el, source) {
    el.className = "src-badge " + source;
    el.textContent = source === "live" ? "live" : "snapshot";
  }

  // Cadence-aware timing: a scheduled/sparse resident within its check-in
  // threshold should read "ran Xh ago" (calm), not "silent Xh" (alarming).
  // Only past-threshold is genuinely overdue.
  function resTiming(r) {
    if (r.event_driven) return { txt: "event-driven", overdue: false };
    if (r.silence == null) return { txt: "—", overdue: false };
    const thr = r.silenceThreshold || 3600;
    if (r.silence > thr) return { txt: "overdue " + fmtSil(r.silence - thr), overdue: true };
    const daily = thr >= 82800; // ~23h+ threshold ⇒ a daily resident
    return { txt: (daily ? "daily · ran " : "ran ") + fmtSil(r.silence) + " ago", overdue: false };
  }

  function renderResidents(residents, source) {
    badge($("resSrc"), source);
    $("residents").innerHTML = residents.map((r) => {
      const t = resTiming(r);
      const cls = t.overdue ? "attention" : r.status === "dark" ? "dark" : "";
      const meta = r.coherence == null ? "no EISV" : "coh " + num(r.coherence);
      return `<span class="res ${cls}"><span class="pip"></span>`
        + `<span class="name">${r.name}</span>`
        + `<span class="meta">${meta} · ${t.txt}</span></span>`;
    }).join("");

    // Attention band — distinguish a real alarm (silent past threshold) from a
    // calm fleet-wide reconnect window (no EISV after a restart is steady-state,
    // not a problem; residents report on their own cadence).
    const silent = [], noEisv = [];
    residents.forEach((r) => {
      const thr = r.silenceThreshold || 3600;
      if (r.silence != null && r.silence > thr) silent.push(r.name);
      else if (r.coherence == null && r.status !== "silent") noEisv.push(r.name);
    });
    const attn = $("attn");
    const names = (a) => a.map((n) => `<b>${n}</b>`).join(" · ");
    const fleetWide = noEisv.length >= Math.ceil(residents.length / 2);
    if (silent.length) {
      attn.hidden = false; attn.className = "attn-band";
      let msg = `${names(silent)} past check-in threshold`;
      if (noEisv.length && !fleetWide) msg += ` · ${noEisv.length} awaiting first check-in`;
      attn.innerHTML = `<span class="glyph">⚠</span><span>${msg}.</span>`;
    } else if (fleetWide) {
      attn.hidden = false; attn.className = "attn-band calm";
      attn.innerHTML = `<span class="glyph">↻</span><span><b>${noEisv.length} of ${residents.length}</b> residents awaiting first check-in — they report on their own cadence.</span>`;
    } else if (noEisv.length) {
      attn.hidden = false; attn.className = "attn-band calm";
      attn.innerHTML = `<span class="glyph">·</span><span>${names(noEisv)} reporting no EISV yet.</span>`;
    } else { attn.hidden = true; }
  }

  function renderStats(stats, residents, source, auto) {
    const live = residents.filter((r) => r.coherence != null);
    const fleetCoh = live.length ? (live.reduce((a, r) => a + r.coherence, 0) / live.length) : null;
    // Automation Health — awareness only ("do I need to care?"); the map lives in /automations.
    const asum = (auto && auto.summary) || {};
    const aKind = asum.by_kind || {};
    const aAtt = (asum.needs_attention || []).length;
    const aStale = !!(auto && auto.stale);
    // Ungated = nothing verifies it (the role-reversal risk) — surface it here.
    const aUngated = ((auto && auto.automations) || []).filter((it) => (it.notes || []).some((n) => n === "gate:ungated")).length;
    const aWarn = aAtt > 0 || aStale || aUngated > 0;
    const autoSub = `${aAtt} attention · ${aUngated} ungated · ${aKind.dogfood || 0} dogfood · ${aKind.ablation || 0} ablation${aStale ? " · stale" : ""}`;
    // A null metric = its live source didn't answer this cycle. Show "—"
    // (unavailable), never a stale snapshot value passed off as current.
    const un = (v) => v == null;
    // Cards that map to a section are links (href); the rest (Calibration,
    // Anomalies — pure stats with no detail view) stay plain, so the clickable
    // affordance is honest rather than implied on everything.
    const cards = [
      { h: "Fleet Coherence", num: num(fleetCoh), sub: `${live.length} of ${residents.length} residents reporting`, cls: "up", rule: true, href: "#residents" },
      { h: "Agents", num: un(stats.agentsActive) ? "—" : stats.agentsActive, of: un(stats.agentsTotal) ? "" : "/ " + stats.agentsTotal, sub: un(stats.agentsActive) ? "unavailable" : "active / total", href: "#agents" },
      { h: "Stuck", num: un(stats.stuck) ? "—" : stats.stuck, sub: un(stats.stuck) ? "unavailable" : (stats.stuck ? "needs attention" : "none flagged"), cls: un(stats.stuck) ? "" : (stats.stuck ? "down" : "up"), href: "#agents" },
      { h: "Automations", num: asum.total || 0, sub: autoSub, cls: aWarn ? "down" : "up", href: "#automations" },
      { h: "Discoveries", num: un(stats.discoveries) ? "—" : stats.discoveries.toLocaleString(), sub: un(stats.discoveries) ? "unavailable" : (typeof stats.discoveriesToday === "number" ? "+" + stats.discoveriesToday + " today" : "knowledge graph"), href: "#discoveries" },
      { h: "Dialectic", num: un(stats.dialectic) ? "—" : stats.dialectic, sub: un(stats.dialectic) ? "unavailable" : (stats.dialectic ? "open sessions" : "no open sessions"), href: "#dialectic" },
      { h: "System Health", num: un(stats.systemHealth) ? "—" : stats.systemHealth, sub: un(stats.systemHealth) ? "unavailable" : (stats.systemHealthDetail || "db · ws · reaper"), cls: un(stats.systemHealth) ? "" : (stats.systemHealth === "OK" ? "up" : "down"), href: "#residents" },
      { h: "Calibration", num: num(stats.calibration), sub: un(stats.calibration) ? "unavailable" : "trajectory health", cls: stats.calibration >= 0.8 ? "up" : "" },
      { h: "Anomalies", num: un(stats.anomalies) ? "—" : stats.anomalies, sub: un(stats.anomalies) ? "unavailable" : (stats.anomalies ? stats.anomalies + " active" : "clear"), cls: un(stats.anomalies) ? "" : (stats.anomalies ? "down" : "up") },
    ];
    const degradeBanner = stats.degraded > 0
      ? `<div style="grid-column:1/-1;font-size:var(--text-xs);color:var(--warn);display:flex;gap:6px;align-items:center;margin-bottom:calc(-1 * var(--space-2))"><span>⚠</span><span>${stats.degraded} metric${stats.degraded > 1 ? "s" : ""} couldn't refresh just now — showing "—" instead of stale values.</span></div>`
      : "";
    // 5 real trust tiers (earned → forming → unearned), each its own colour.
    const TIER_COLOR = { verified: "var(--ok)", established: "var(--eisv-c)", emerging: "var(--eisv-s)", provisional: "var(--warn)", unknown: "var(--faint)" };
    const tiers = stats.trustTiers || [];
    const max = Math.max(1, ...tiers.map((t) => t.n));
    const tierBars = tiers.map((t) =>
      `<div title="${t.tier}: ${t.n}" style="flex:1;border-radius:3px 3px 0 0;background:${TIER_COLOR[t.tier] || "var(--faint)"};height:${Math.round((t.n / max) * 100)}%"></div>`).join("");
    const tierLegend = tiers.map((t) =>
      `<span><i style="background:${TIER_COLOR[t.tier] || "var(--faint)"}"></i>${t.tier} ${t.n}</span>`).join("");
    const tierScope = typeof stats.trustEarned === "number"
      ? `${stats.trustEarned.toLocaleString()} earned of ${stats.trustFleet.toLocaleString()} · ${(stats.trustUnknown || 0).toLocaleString()} unknown`
      : "";

    const trustBody = stats.trustTiers
      ? `<div class="tiers">${tierBars}</div><div class="legend" style="margin-top:.5rem;flex-wrap:wrap">${tierLegend}</div>`
      : `<div class="sub" style="color:var(--muted)">unavailable</div>`;
    $("stats").innerHTML = degradeBanner + cards.map((s) => {
      const tag = s.href ? "a" : "div"; const attr = s.href ? ` href="${s.href}" style="text-decoration:none;color:inherit"` : "";
      return `<${tag} class="card ${s.rule ? "accent-rule" : ""}"${attr}><h3>${s.h}</h3>`
        + `<div class="num">${s.num}${s.of ? `<span class="of"> ${s.of}</span>` : ""}</div>`
        + `<div class="sub ${s.cls || ""}">${s.sub}</div></${tag}>`;
    }).join("")
      + `<div class="card wide"><h3>Trust Tiers ${tierScope ? `<span style="text-transform:none;letter-spacing:0;color:var(--faint);font-weight:400">· ${tierScope}</span>` : ""}</h3>${trustBody}</div>`;
  }

  function renderPulse(residents) {
    // last check-in = smallest silence among reporting residents
    const reporting = residents.filter((r) => r.eisv);
    const last = reporting.sort((a, b) => (a.silence ?? 1e9) - (b.silence ?? 1e9))[0];
    if (!last) return;
    $("pulseWho").textContent = last.name;
    $("pulseFresh").textContent = "checked in " + fmtSil(last.silence) + " ago";

    const risk = last.risk ?? 0;
    $("riskVal").textContent = num(risk);
    $("riskFill").style.width = Math.max(2, risk * 100) + "%";
    const fill = $("riskFill");
    fill.style.background = risk < 0.35 ? "var(--ok)" : risk < 0.6 ? "var(--warn)" : "var(--danger)";

    const v = $("pulseVerdict");
    const verd = last.verdict || "—";
    v.className = "verdict" + (verd === "proceed" ? "" : risk >= 0.7 ? " danger" : " warn");
    v.querySelector("span:last-child").textContent = verd;

    const E = last.eisv;
    const rows = [["E", E.E, "e", false], ["I", E.I, "i", false], ["S", E.S, "s", false], ["V", E.V, "v", true]];
    $("eisv").innerHTML = rows.map(([k, val, c, signed]) => {
      const w = signed ? Math.abs(val) * 50 : val * 100;
      const left = signed ? (val < 0 ? 50 - Math.abs(val) * 50 : 50) : 0;
      return `<div class="eisv-row"><span class="k">${k}</span>`
        + `<span class="bar ${signed ? "signed" : ""}"><i class="${c}" style="left:${left}%;width:${w}%"></i></span>`
        + `<span class="val">${num(val)}</span></div>`;
    }).join("");
  }

  let lastResidents = null;

  function applyHealth(health) {
    if (health.data) {
      const h = health.data;
      $("serverStat").innerHTML = `v<b>${h.version}</b> · up <b>${h.uptime}</b> · db <b>${h.db}</b>`;
    }
  }
  function footnote(anyLive) {
    $("foot").innerHTML = anyLive
      ? "Redesign · served live · design system in <code>tokens.css</code> + <code>kit.css</code>."
      : "Redesign reference · rendering bundled snapshot (open served same-origin for live data) · "
        + "design system in <code>tokens.css</code> + <code>kit.css</code>. Toggle theme to reskin via one token swap.";
  }

  // Full first render — light (residents/pulse/health) + heavy (stats) together.
  async function render() {
    const [health, residents, stats, auto] = await Promise.all([DATA.health(), DATA.residents(), DATA.stats(), DATA.automations()]);
    lastResidents = residents;
    applyHealth(health);
    renderResidents(residents.data, residents.source);
    renderStats(stats.data, residents.data, stats.source, auto.data);
    renderPulse(residents.data);
    footnote([residents, stats, health].some((r) => r.source === "live"));
  }

  // Light refresh (fast cadence) — the "is the fleet alive" glance only.
  async function refresh() {
    const [health, residents] = await Promise.all([DATA.health(), DATA.residents()]);
    lastResidents = residents;
    applyHealth(health);
    renderResidents(residents.data, residents.source);
    renderPulse(residents.data);
  }

  // Heavy refresh (slow cadence) — the 7-tool headline batch; reuse last residents
  // for fleet coherence rather than refetching them.
  async function refreshStats() {
    const [stats, auto] = await Promise.all([DATA.stats(), DATA.automations()]);
    const residents = lastResidents || (lastResidents = await DATA.residents());
    renderStats(stats.data, residents.data, stats.source, auto.data);
  }

  window.Landing = { render, refresh, refreshStats };
})();
