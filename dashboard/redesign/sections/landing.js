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
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ── ONE liveness partition, used by every reducer on this page ─────────────
  // Previously six different predicates answered "is this resident alive?" —
  // `coherence != null`, `r.eisv`, `silence > threshold`, `status === "dark"` —
  // and they could disagree. They all route through DATA.residentLiveness now.
  //
  // `reporting` is also the Fleet Coherence denominator: that headline IS an
  // EISV mean, so its denominator MUST be the EISV predicate or the card lies
  // about its own arithmetic. The fix for "N of M reporting" reading as
  // liveness is the LABEL plus surfacing the middle — not swapping the maths.
  function partition(residents) {
    const p = { reporting: [], "alive-no-eisv": [], down: [] };
    (residents || []).forEach((r) => { (p[DATA.residentLiveness(r)] || p.down).push(r); });
    return p;
  }
  // Byte-identical subtitle from both renderers (full rebuild + in-place
  // update), or the card visibly flickers between two wordings.
  //
  // The subtitle must (a) describe the predicate that actually produced the
  // denominator and (b) account for EVERY resident. "N of M reporting EISV"
  // failed both: `reporting` also requires being IN CADENCE, so a resident past
  // its check-in threshold was excluded even though it still carries a (stale)
  // coherence — which the strip immediately below prints. The card said "5 of 6
  // reporting EISV" while all six rows showed a coh value, and only one of the
  // two excluded buckets was ever named, so the numbers did not add up. Both
  // excluded buckets are named now; the maths is unchanged, and deliberately
  // so — a mean over residents that stopped checking in is a stale mean.
  function fleetSummary(residents) {
    const p = partition(residents);
    const live = p.reporting;
    const coh = live.length ? live.reduce((a, r) => a + r.coherence, 0) / live.length : null;
    const sub = `${live.length} of ${(residents || []).length} in cadence with EISV`
      + (p["alive-no-eisv"].length ? ` · ${p["alive-no-eisv"].length} in cadence, no EISV` : "")
      + (p.down.length ? ` · ${p.down.length} not checking in` : "");
    return { part: p, coh, sub };
  }

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
    const part = partition(residents);
    // "dark" survives only as a CSS class here — it is not a status the server
    // ever emits (grep '"dark"' src/ → 0 hits).
    $("residents").innerHTML = residents.map((r) => {
      const t = resTiming(r);
      const cls = t.overdue ? "attention" : DATA.residentLiveness(r) === "down" ? "dark" : "";
      const meta = r.coherence == null ? "no EISV" : "coh " + num(r.coherence);
      return `<span class="res ${cls}"><span class="pip"></span>`
        + `<span class="name">${r.name}</span>`
        + `<span class="meta">${meta} · ${t.txt}</span></span>`;
    }).join("");

    // Attention band — distinguish a real alarm (silent past threshold) from a
    // calm fleet-wide reconnect window (no EISV after a restart is steady-state,
    // not a problem; residents report on their own cadence). Derived from the
    // same partition, so it cannot disagree with the strip above it.
    const silent = [], noEisv = [];
    residents.forEach((r) => {
      const thr = r.silenceThreshold || 3600;
      if (r.silence != null && r.silence > thr) silent.push(r.name);
      else if (part["alive-no-eisv"].indexOf(r) !== -1) noEisv.push(r.name);
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
    const fleet = fleetSummary(residents);
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
    // Stuck card body: NAME the flagged agents, each a link to that one agent's
    // detail. The old whole-card `href="#agents"` landed the operator on an
    // unfiltered 100-row table with no indication which agents were meant —
    // the count said "4 · needs attention" and the click went nowhere useful.
    // Per-agent beats a filter: `stuck` is orthogonal to the status filter (all
    // four live stuck agents are status=active), and agent(list) is hard-capped
    // at 100 of ~457 server-side, so a client-side filter could render an empty
    // table under a non-zero count. A per-agent link degrades honestly instead.
    // Degradation window: a server that predates the stuck-entry enrichment
    // returns neither a name nor a joinable handle. Render the reason WITHOUT a
    // link rather than a link that goes nowhere — a dead link is the bug being
    // fixed. Self-resolves once the server change deploys.
    const stuckList = stats.stuckList || [];
    const stuckBody = stuckList.map((s) => {
      const inner = `<span class="name">${esc(s.name || "agent not identified")}</span>`
        + `<span class="reason">${esc(s.reason)}${s.soft ? " · soft" : ""}</span>`;
      if (!s.id) {
        return `<span class="stuck-row plain" title="${esc(s.details)}${s.name ? "" : "\n(server did not report an identifier for this detection)"}">${inner}</span>`;
      }
      return `<a href="#agents" class="stuck-row" data-stuck-id="${esc(s.id)}" title="${esc(s.details)}">${inner}</a>`;
    }).join("")
      + (typeof stats.stuck === "number" && stats.stuck > stuckList.length
        ? `<a href="#agents" class="stuck-more">+${stats.stuck - stuckList.length} more</a>` : "");
    const cards = [
      { h: "Fleet Coherence", id: "fleetcoh", num: num(fleet.coh), sub: fleet.sub, cls: "up", rule: true, href: "#residents" },
      { h: "Agents", num: un(stats.agentsActive) ? "—" : stats.agentsActive, of: un(stats.agentsTotal) ? "" : "/ " + stats.agentsTotal, sub: un(stats.agentsActive) ? "unavailable" : "active / total", href: "#agents" },
      { h: "Stuck", num: un(stats.stuck) ? "—" : stats.stuck, sub: un(stats.stuck) ? "unavailable" : (stats.stuck ? "needs attention" : "none flagged"), cls: un(stats.stuck) ? "" : (stats.stuck ? "down" : "up"),
        body: stuckBody, href: stuckBody ? null : "#agents" },
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
    // Tier colours live in tokens.css (--tier-*) so this and the agents-table
    // badge share ONE vocabulary and both themes come free. The whitelist is
    // also the guard on the var() interpolation — a server-supplied tier name
    // never reaches the style attribute.
    const TIER_NAMES = ["verified", "established", "emerging", "provisional", "unknown"];
    const tierVar = (k) => TIER_NAMES.indexOf(k) !== -1 ? `var(--tier-${k})` : "var(--tier-unknown)";
    const tiers = stats.trustTiers || [];
    const max = Math.max(1, ...tiers.map((t) => t.n));
    // Horizontal + LINEAR. The card is 2 columns wide and the old bar box was a
    // hard 34px tall, so four ~125px-wide bars rendered the three small tiers at
    // 1.0–1.7px — unreadable and effectively un-hoverable, and Math.round on the
    // PERCENTAGE collapsed established (5.3%) and provisional (4.7%) to the same
    // height. Width is the axis with room (~370px track). Same linear scale, so
    // proportion stays honest — a log scale would render established at half of
    // emerging when it is 5% of it. Each row is self-labelling, which absorbs
    // the separate legend that used to duplicate these numbers underneath.
    // min-width 3px only when n > 0, so "there are none" still reads as none;
    // worst-case distortion 3/370 = 0.8%, vs 12% for a 4px floor on 34px.
    const tierRows = tiers.map((t) => {
      const pct = (t.n / max) * 100;
      return `<div class="tier-row" title="${esc(t.tier)}: ${t.n.toLocaleString()}">`
        + `<span class="tier-name">${esc(t.tier)}</span>`
        + `<span class="tier-track"><i style="width:${pct}%;min-width:${t.n > 0 ? 3 : 0}px;background:${tierVar(t.tier)}"></i></span>`
        + `<span class="tier-n">${t.n.toLocaleString()}</span></div>`;
    }).join("");
    const tierScope = typeof stats.trustEarned === "number"
      ? `${stats.trustEarned.toLocaleString()} earned of ${stats.trustFleet.toLocaleString()} · ${(stats.trustUnknown || 0).toLocaleString()} unknown`
      : "";

    const trustBody = stats.trustTiers
      ? `<div class="tier-rows">${tierRows}</div>`
      : `<div class="sub" style="color:var(--muted)">unavailable</div>`;
    $("stats").innerHTML = degradeBanner + cards.map((s) => {
      const tag = s.href ? "a" : "div"; const attr = s.href ? ` href="${s.href}" style="text-decoration:none;color:inherit"` : "";
      const dataAttr = s.id ? ` data-card="${s.id}"` : "";
      return `<${tag} class="card ${s.rule ? "accent-rule" : ""}"${attr}${dataAttr}><h3>${s.h}</h3>`
        + `<div class="num">${s.num}${s.of ? `<span class="of"> ${s.of}</span>` : ""}</div>`
        + `<div class="sub ${s.cls || ""}">${s.sub}</div>`
        + (s.body ? `<div class="card-body">${s.body}</div>` : "") + `</${tag}>`;
    }).join("")
      + `<div class="card wide"><h3>Trust Tiers ${tierScope ? `<span style="text-transform:none;letter-spacing:0;color:var(--faint);font-weight:400">· ${tierScope}</span>` : ""}</h3>${trustBody}</div>`;
  }

  function renderPulse(residents) {
    // last check-in = smallest silence among reporting residents. Same
    // partition as everything else on this page (was a fourth predicate,
    // `r.eisv`); Pulse additionally needs the eisv payload it renders.
    const reporting = partition(residents).reporting.filter((r) => r.eisv);
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

  // In-memory resident model. Each entry is the DATA.residents() shape plus an
  // absolute `_lastSeenMs` (when it last checked in), so silence is computed at
  // render time rather than frozen at fetch time — it ticks up live and snaps to
  // 0 when a resident checks in. Seeded from the REST fetch; mutated by pushed
  // eisv_update events (see applyEvent) so the strip updates without a refetch.
  let RMODEL = [];
  let lastSource = "snapshot";

  function seedResidents(list, source) {
    const now = Date.now();
    lastSource = source;
    RMODEL = (list || []).map((r) => Object.assign({}, r, {
      _lastSeenMs: typeof r.silence === "number" ? now - r.silence * 1000 : null,
    }));
  }
  // Render-ready snapshot with live silence derived from _lastSeenMs.
  function viewResidents() {
    const now = Date.now();
    return RMODEL.map((r) => Object.assign({}, r, {
      silence: r._lastSeenMs != null ? Math.round((now - r._lastSeenMs) / 1000) : r.silence,
    }));
  }
  // Recompute the Fleet Coherence card in place (a derived aggregate, so it
  // shifts as residents report) without rebuilding the whole stats grid.
  function updateFleetCoherence(residents) {
    const el = document.querySelector('[data-card="fleetcoh"]');
    if (!el) return;
    const fleet = fleetSummary(residents);
    const numEl = el.querySelector(".num"), subEl = el.querySelector(".sub");
    if (numEl) numEl.textContent = num(fleet.coh);
    if (subEl) subEl.textContent = fleet.sub; // same string renderStats produces
  }

  // Apply one pushed eisv_update to the residents strip directly — no refetch.
  // Returns true only when the event belongs to a known resident (matched by
  // agent_name == label, the same rule the server uses); other agents' check-ins
  // return false so the caller falls back to the doorbell refresh.
  function applyEvent(msg) {
    if (!msg || msg.type !== "eisv_update" || !msg.agent_name) return false;
    const r = RMODEL.find((x) => x.name === msg.agent_name);
    if (!r) return false;
    if (msg.eisv) r.eisv = msg.eisv;
    if (typeof msg.coherence === "number") r.coherence = msg.coherence;
    if (typeof msg.risk === "number") r.risk = msg.risk;
    const act = msg.decision && msg.decision.action;
    if (act) r.verdict = act;
    r._lastSeenMs = Date.now(); // just checked in: not silent
    if (r.status === "silent") r.status = "healthy"; // server vocabulary only
    const view = viewResidents();
    renderResidents(view, lastSource);
    renderPulse(view);
    updateFleetCoherence(view);
    return true;
  }

  // Re-render the strip from the model so silence visibly accrues during quiet
  // periods (driven by app.html on a slow tick while the Overview is visible).
  function tickSilence() {
    if (!RMODEL.length || !$("residents")) return;
    const view = viewResidents();
    renderResidents(view, lastSource);
    renderPulse(view);
  }

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
    seedResidents(residents.data, residents.source);
    const view = viewResidents();
    applyHealth(health);
    renderResidents(view, residents.source);
    renderStats(stats.data, view, stats.source, auto.data);
    renderPulse(view);
    footnote([residents, stats, health].some((r) => r.source === "live"));
  }

  // Light refresh (fast cadence) — the "is the fleet alive" glance only.
  async function refresh() {
    const [health, residents] = await Promise.all([DATA.health(), DATA.residents()]);
    seedResidents(residents.data, residents.source);
    const view = viewResidents();
    applyHealth(health);
    renderResidents(view, residents.source);
    renderPulse(view);
  }

  // Heavy refresh (slow cadence) — the 7-tool headline batch; reuse the resident
  // model for fleet coherence rather than refetching it.
  async function refreshStats() {
    const [stats, auto] = await Promise.all([DATA.stats(), DATA.automations()]);
    if (!RMODEL.length) { const residents = await DATA.residents(); seedResidents(residents.data, residents.source); }
    renderStats(stats.data, viewResidents(), lastSource, auto.data);
  }

  // Stuck-row drill-down. Bound ONCE and delegated: renderStats replaces the
  // whole #stats innerHTML every 30s, so per-render onclick handlers would be
  // destroyed and rebound on every tick. (#stats holds no <select>/<input>, so
  // the full rebuild is otherwise safe — this design adds none.)
  const statsEl = $("stats");
  if (statsEl) {
    statsEl.addEventListener("click", (e) => {
      const row = e.target.closest && e.target.closest("[data-stuck-id]");
      if (!row) return;
      e.preventDefault();
      const id = row.dataset.stuckId;
      // Hash FIRST so the pane is visible, then focus — Agents.focus is safe
      // whether or not the section has loaded, and consumes itself once.
      if (location.hash !== "#agents") location.hash = "#agents";
      if (window.Agents && window.Agents.focus) window.Agents.focus(id);
    });
  }

  window.Landing = { render, refresh, refreshStats, applyEvent, tickSilence };
})();
