/*
 * Landing section — headline cards, latest check-ins, residents strip.
 *
 * Agent-first (2026-09-27): the page leads with what every install has —
 * agents checking in and the verdicts they get — and shows the resident strip
 * only when the deployment configures residents. Fleet Coherence left the
 * headline row: it is a mean whose between-agent spread is ~0.008, so it could
 * not move enough to say anything; the strip keeps each resident's value.
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
    const label = source === "live" || source === "snapshot" ? source : "unavailable";
    el.className = "src-badge " + label;
    el.textContent = label;
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
    // Residents are deployment configuration (UNITARES_RESIDENTS, empty by
    // default). An install that runs none gets no resident block at all.
    const block = $("resBlock");
    if (block) block.hidden = !(residents && residents.length);
    badge($("resSrc"), source);
    const summary = $("resSummary");
    if (summary) summary.textContent = residents && residents.length ? "· " + fleetSummary(residents).sub : "";
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
    // No residents configured (a fresh install, or a deployment that runs
    // none): there is nothing to await. Without this, 0 >= ceil(0/2) reads as
    // "fleet-wide" and the band announces "0 of 0 residents awaiting".
    if (!residents.length) {
      attn.hidden = true;
    } else if (silent.length) {
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

  // Seconds of the last hour the server's in-memory check-in history covers.
  // After a restart (or once its ring is full) the hour is only partly seen,
  // and a total over it must say so rather than read as a quiet hour.
  function coveredLabel(coverageStart, windowMin) {
    if (typeof coverageStart !== "number") return "";
    const covered = Date.now() / 1000 - coverageStart;
    return covered < windowMin * 60 - 30 ? ` · history covers ${fmtSil(Math.max(0, Math.round(covered)))}` : "";
  }

  function agentsCard(stats, un) {
    // Primary: agents with a check-in in the last hour, from the server's
    // check-in ring. That is the question an operator of any install asks
    // ("who is here?") and it needs no binding/lease or resident roster.
    if (AGENTS_SRC !== "unavailable" && AMODEL) {
      const cutoff = Date.now() - 3600 * 1000;
      const n = AMODEL.filter((a) => a._ms != null && a._ms >= cutoff).length;
      return { h: "Agents", id: "agents", num: n, of: un(stats.agentsTotal) ? "" : "/ " + stats.agentsTotal,
        sub: "checked in within the hour" + coveredLabel(AGENTS_COVERAGE, 60), href: "#agents",
        title: `${n} agent${n === 1 ? "" : "s"} checked in during the last hour`
          + (un(stats.agentsTotal) ? "." : `, of ${stats.agentsTotal} identities seen in the last 30 days. The Agents tab reads a 14-day window, so its total is smaller.`) };
    }
    // Fallback when the ring did not answer: the registry/presence reading.
    const hasAgentPresence = typeof stats.agentsLive === "number";
    const presenceUnknown = (stats.agentsPresenceUnknown || 0)
      + (stats.agentsPresenceUnavailable || 0);
    // No agent carries a live signal, but some have presence the server cannot
    // determine (clients that never hold a binding/lease, e.g. a host plugin
    // that only checks in). "0 live" would read as "nothing is here", so lead
    // with the registry count and say presence is unknown. One live signal is
    // enough to switch back to the live count.
    const presenceBlind = hasAgentPresence && stats.agentsLive === 0
      && presenceUnknown > 0 && typeof stats.agentsActive === "number";
    const agentHeadline = hasAgentPresence && !presenceBlind ? stats.agentsLive : stats.agentsActive;
    const agentSub = presenceBlind
      ? `registry active / total · 30d window · ${presenceUnknown} presence unknown`
      : hasAgentPresence
        ? `live binding/lease · 30d window${presenceUnknown ? ` · ${presenceUnknown} presence unknown` : ""}`
        : un(stats.agentsActive) ? "unavailable" : "registry active / total · 30d window";
    return { h: "Agents", id: "agents", num: un(agentHeadline) ? "—" : agentHeadline, of: un(stats.agentsTotal) ? "" : "/ " + stats.agentsTotal, sub: agentSub, href: "#agents",
      title: un(stats.agentsTotal) ? ""
        : presenceBlind
          ? `${agentHeadline} registry-active of ${stats.agentsTotal} identities seen in the last 30 days. None holds a live binding/lease, and presence is unknown for ${presenceUnknown}. The Agents tab reads a 14-day window, so its total is smaller.`
          : `${agentHeadline} with a live binding/lease right now, of ${stats.agentsTotal} registry identities seen in the last 30 days. The Agents tab reads a 14-day window, so its total is smaller.` };
  }

  function checkinsCard() {
    if (!CHK) return { h: "Check-ins", id: "checkins", num: "—", sub: "unavailable", href: "#activity" };
    // Pause here is a verdict PRODUCED, not an intervention delivered.
    return { h: "Check-ins", id: "checkins", num: CHK.total, href: "#activity",
      sub: `last hour · ${CHK.proceed} proceed · ${CHK.guide} guide · ${CHK.pause} pause` + coveredLabel(CHK.coverageStart, CHK.windowMin || 60),
      cls: CHK.pause ? "warn" : "",
      title: "State-writing check-ins in the last hour by the verdict produced. A pause verdict is produced, not necessarily delivered." };
  }

  function dialecticCard(stats, un) {
    if (un(stats.dialectic)) return { h: "Dialectic", num: "—", sub: "unavailable", href: "#dialectic" };
    const recent = typeof stats.dialecticRecent === "number" ? stats.dialecticRecent : 0;
    const failed = typeof stats.dialecticFailed === "number" ? stats.dialecticFailed : 0;
    // Failed reviews are the actionable part of this card, so they lead the
    // subtitle and colour it; "0 open" alone read as all-quiet while most
    // recent sessions had failed.
    const failLine = failed ? `${failed} of ${recent} recent failed (${Math.round((failed / recent) * 100)}%)` : null;
    const openLine = stats.dialectic ? "open sessions" : "none open";
    return { h: "Dialectic", num: stats.dialectic, href: "#dialectic", cls: failed ? "warn" : "",
      sub: failLine ? `${failLine} · ${openLine}` : (recent ? `${openLine} · ${recent} recent` : (stats.dialectic ? "open sessions" : "no open sessions")) };
  }

  let LAST_STATS = {};
  function renderStats(stats) {
    stats = stats || LAST_STATS || {};
    LAST_STATS = stats;
    // A null metric = its live source didn't answer this cycle. Show "—"
    // (unavailable), never a stale snapshot value passed off as current.
    const un = (v) => v == null;
    // Every card here is one an operator of ANY install can read and act on.
    // Agent attention, Automations, Calibration, Anomalies and Trust Tiers were
    // removed 2026-09-26, Fleet Coherence 2026-09-27; see the header comment and
    // dashboard/EXTENSIONS.md for deployment-specific panels.
    const cards = [
      agentsCard(stats, un),
      checkinsCard(),
      dialecticCard(stats, un),
      { h: "Discoveries", num: un(stats.discoveries) ? "—" : stats.discoveries.toLocaleString(), sub: un(stats.discoveries) ? "unavailable" : (typeof stats.discoveriesToday === "number" ? "+" + stats.discoveriesToday + " today" : "knowledge graph"), href: "#discoveries" },
      { h: "System Health", num: un(stats.systemHealth) ? "—" : stats.systemHealth, sub: un(stats.systemHealth) ? "unavailable" : (stats.systemHealthDetail || "db · ws · reaper"), cls: un(stats.systemHealth) ? "" : (stats.systemHealth === "OK" ? "up" : "down") },
    ];
    const degradeBanner = stats.degraded > 0
      ? `<div style="grid-column:1/-1;font-size:var(--text-xs);color:var(--warn);display:flex;gap:6px;align-items:center;margin-bottom:calc(-1 * var(--space-2))"><span>⚠</span><span>${stats.degraded} metric${stats.degraded > 1 ? "s" : ""} couldn't refresh just now — showing "—" instead of stale values.</span></div>`
      : "";
    $("stats").innerHTML = degradeBanner + cards.map((s) => {
      const tag = s.href ? "a" : "div"; const attr = s.href ? ` href="${s.href}" style="text-decoration:none;color:inherit"` : "";
      const dataAttr = s.id ? ` data-card="${s.id}"` : "";
      const titleAttr = s.title ? ` title="${esc(s.title)}"` : "";
      return `<${tag} class="card ${s.rule ? "accent-rule" : ""}"${attr}${dataAttr}${titleAttr}><h3>${s.h}</h3>`
        + `<div class="num">${s.num}${s.of ? `<span class="of"> ${s.of}</span>` : ""}</div>`
        + `<div class="sub ${s.cls || ""}">${s.sub}</div>`
        + (s.body ? `<div class="card-body">${s.body}</div>` : "") + `</${tag}>`;
    }).join("");
  }

  // Verdict → bucket and pill tone, matching the server's rule
  // (src/broadcaster.py verdict_bucket): proceed/approve/continue are the calm
  // case; guide is a nudge; anything else (pause, reject, risk_pause, a new
  // hard stop) is a hard verdict.
  const verdictBucket = (a) => a === "guide" ? "guide" : !a || a === "proceed" || a === "approve" || a === "continue" ? "proceed" : "pause";
  const tone = (a) => ({ proceed: "", guide: " warn", pause: " danger" })[verdictBucket(a)];
  const ago = (ms) => ms == null ? "—" : fmtSil(Math.max(0, Math.round((Date.now() - ms) / 1000))) + " ago";

  // Latest check-in of ANY agent (not only residents), from the check-in ring.
  function renderPulse() {
    const last = AMODEL && AMODEL[0];
    if (!last) {
      // Clear the detail too: after a restart (ring empty) or an outage, the
      // previous agent's risk, verdict and EISV must not stay on screen under
      // "no check-ins yet".
      $("pulseWho").textContent = AGENTS_SRC === "unavailable" ? "server not answering" : "no check-ins yet";
      $("pulseFresh").textContent = "";
      $("riskVal").textContent = "—";
      $("riskFill").style.width = "0%";
      $("pulseVerdict").className = "verdict";
      $("pulseVerdict").querySelector("span:last-child").textContent = "—";
      $("eisv").innerHTML = "";
      return;
    }
    $("pulseWho").textContent = last.name;
    $("pulseFresh").textContent = "checked in " + ago(last._ms);

    const risk = last.risk ?? 0;
    $("riskVal").textContent = num(risk);
    $("riskFill").style.width = Math.max(2, risk * 100) + "%";
    $("riskFill").style.background = risk < 0.35 ? "var(--ok)" : risk < 0.6 ? "var(--warn)" : "var(--danger)";

    const v = $("pulseVerdict");
    v.className = "verdict" + tone(last.action);
    v.querySelector("span:last-child").textContent = last.action || "—";

    const E = last.eisv;
    $("eisv").innerHTML = !E ? "" : [["E", E.E, "e", false], ["I", E.I, "i", false], ["S", E.S, "s", false], ["V", E.V, "v", true]].map(([k, val, c, signed]) => {
      const w = signed ? Math.abs(val) * 50 : val * 100;
      const left = signed ? (val < 0 ? 50 - Math.abs(val) * 50 : 50) : 0;
      return `<div class="eisv-row"><span class="k">${k}</span>`
        + `<span class="bar ${signed ? "signed" : ""}"><i class="${c}" style="left:${left}%;width:${w}%"></i></span>`
        + `<span class="val">${num(val)}</span></div>`;
    }).join("");
  }

  // The agents behind the headline: most recent check-in per agent.
  const RECENT_ROWS = 8;
  function renderRecent() {
    const el = $("recent");
    if (!el) return;
    const rows = (AMODEL || []).slice(0, RECENT_ROWS);
    if (!rows.length) { el.innerHTML = ""; return; }
    el.innerHTML = `<table class="tbl"><thead><tr><th>Agent</th><th>Verdict</th><th>Risk</th><th>Check-ins</th><th>Last</th></tr></thead><tbody>`
      + rows.map((a) => `<tr><td>${esc(a.name)}</td>`
        + `<td><span class="verdict${tone(a.action)}"><span class="pip"></span><span>${esc(a.action || "—")}</span></span></td>`
        + `<td class="mono">${num(a.risk)}</td><td class="mono">${a.checkins || "—"}</td>`
        + `<td class="mono">${ago(a._ms)}</td></tr>`).join("")
      + `</tbody></table>`
      + (AMODEL.length > RECENT_ROWS ? `<p style="margin-top:var(--space-2);font-size:var(--text-xs);color:var(--muted)">${AMODEL.length - RECENT_ROWS} more checked in since the server's check-in history began · <a href="#agents" style="color:var(--accent)">all agents</a></p>` : "");
  }

  // Agent model: /v1/eisv/agents rows plus an absolute `_ms`, so "ago"
  // accrues between fetches and a pushed check-in can move an agent to the top.
  let AMODEL = null, AGENTS_SRC = "snapshot", AGENTS_COVERAGE = null, CHK = null;
  function seedAgents(r) {
    AGENTS_SRC = r ? r.source : "unavailable";
    const d = (r && r.data) || {};
    AGENTS_COVERAGE = typeof d.coverageStart === "number" ? d.coverageStart : null;
    AMODEL = (d.agents || []).map((a) => Object.assign({}, a, { _ms: a.ts ? Date.parse(a.ts) : null }));
  }
  function seedCheckins(r) {
    CHK = r && r.data ? Object.assign({}, r.data) : null;
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
  // Apply one pushed eisv_update directly — no refetch. Every agent's
  // check-in moves it to the top of the feed and counts toward the check-ins
  // card; a resident's also updates the strip (matched by agent_name == label,
  // the same rule the server uses). Returns true: the view is current.
  function applyEvent(msg) {
    if (!msg || msg.type !== "eisv_update" || !msg.agent_id) return false;
    const act = (msg.decision && (msg.decision.sub_action || msg.decision.action)) || null;
    if (AMODEL) {
      const prev = AMODEL.find((a) => a.id === msg.agent_id);
      const row = Object.assign(prev || { id: msg.agent_id, checkins: 0 }, {
        name: msg.agent_name || (prev && prev.name) || msg.agent_id.slice(0, 8),
        eisv: msg.eisv || (prev && prev.eisv) || null,
        coherence: typeof msg.coherence === "number" ? msg.coherence : prev && prev.coherence,
        risk: typeof msg.risk === "number" ? msg.risk : prev && prev.risk,
        action: act || (prev && prev.action) || null,
        _ms: Date.now(),
      });
      row.checkins = (row.checkins || 0) + 1;
      AMODEL = [row].concat(AMODEL.filter((a) => a !== row));
    }
    // Count it now; refreshStats re-reads the server's hour on its cadence, so
    // pushed check-ins age out of "last hour" instead of accumulating.
    if (CHK) { CHK[verdictBucket(act)] += 1; CHK.total += 1; }
    const r = msg.agent_name && RMODEL.find((x) => x.name === msg.agent_name);
    if (r) {
      if (msg.eisv) r.eisv = msg.eisv;
      if (typeof msg.coherence === "number") r.coherence = msg.coherence;
      if (typeof msg.risk === "number") r.risk = msg.risk;
      if (act) r.verdict = act;
      r._lastSeenMs = Date.now(); // just checked in: not silent
      if (r.status === "silent") r.status = "healthy"; // server vocabulary only
      renderResidents(viewResidents(), lastSource);
    }
    renderPulse();
    renderRecent();
    renderStats();
    return true;
  }

  // Re-render from the models so "ago" and silence visibly accrue during quiet
  // periods (driven by app.html on a slow tick while the Overview is visible).
  function tickSilence() {
    if (RMODEL.length && $("residents")) renderResidents(viewResidents(), lastSource);
    renderPulse();
    renderRecent();
  }

  let lastHealthSource = "snapshot";
  function applyHealth(health) {
    lastHealthSource = health.source;
    if (health.data) {
      const h = health.data;
      $("serverStat").innerHTML = `v<b>${h.version}</b> · up <b>${h.uptime}</b> · db <b>${h.db}</b>`;
    } else {
      $("serverStat").textContent = "server not answering";
    }
  }
  function footnote(anyLive) {
    $("foot").innerHTML = anyLive
      ? "Redesign · served live · design system in <code>tokens.css</code> + <code>kit.css</code>."
      : !DATA.snapshotFallback
        ? "Server not answering — retrying. Nothing below is from a snapshot."
      : "Redesign reference · rendering bundled snapshot (open served same-origin for live data) · "
        + "design system in <code>tokens.css</code> + <code>kit.css</code>. Toggle theme to reskin via one token swap.";
  }

  // Optional accessors: a stubbed or older data layer without them renders the
  // registry fallback rather than failing the page.
  const recentAgents = () => (DATA.recentAgents ? DATA.recentAgents() : Promise.resolve(null));
  const checkinActivity = () => (DATA.checkinActivity ? DATA.checkinActivity() : Promise.resolve(null));

  // Full first render — light (agents/residents/health) + heavy (stats) together.
  async function render() {
    const [health, residents, stats, agents, checkins] = await Promise.all([
      DATA.health(), DATA.residents(), DATA.stats(), recentAgents(), checkinActivity()]);
    seedResidents(residents.data, residents.source);
    seedAgents(agents);
    seedCheckins(checkins);
    applyHealth(health);
    renderStats(stats.data);
    renderPulse();
    renderRecent();
    renderResidents(viewResidents(), residents.source);
    footnote([residents, stats, health, agents].some((r) => r && r.source === "live"));
  }

  // Light refresh (fast cadence) — who is checking in, and is the server up.
  async function refresh() {
    const [health, residents, agents, checkins] = await Promise.all([
      DATA.health(), DATA.residents(), recentAgents(), checkinActivity()]);
    seedResidents(residents.data, residents.source);
    seedAgents(agents);
    seedCheckins(checkins);
    applyHealth(health);
    renderResidents(viewResidents(), residents.source);
    renderPulse();
    renderRecent();
    renderStats();
  }

  // Heavy refresh (slow cadence) — the headline batch.
  async function refreshStats() {
    // refresh() re-reads residents and health, but it runs only on stream
    // events or while the stream is down. With the stream open and quiet (a
    // fresh install, nothing checking in), one failed read of either was
    // never retried and its fallback stayed on screen. Retry here, on the
    // stats cadence, until both answer live.
    if (lastSource !== "live" || lastHealthSource !== "live" || AGENTS_SRC !== "live") await refresh();
    // The "last hour" cards are live-updated by pushed check-ins, which only
    // add. Re-read both rings here so check-ins older than an hour drop out
    // while the stream stays open. Both reads are in-memory on the server.
    const [stats, agents, checkins] = await Promise.all([DATA.stats(), recentAgents(), checkinActivity()]);
    if (agents) seedAgents(agents);
    if (checkins) seedCheckins(checkins);
    renderStats(stats.data);
    renderPulse();
    renderRecent();
  }

  window.Landing = { render, refresh, refreshStats, applyEvent, tickSilence };
})();
