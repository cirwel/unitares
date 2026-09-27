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

  function renderStats(stats, residents, source) {
    const fleet = fleetSummary(residents);
    // A null metric = its live source didn't answer this cycle. Show "—"
    // (unavailable), never a stale snapshot value passed off as current.
    const un = (v) => v == null;
    // Every card here is one an operator of ANY install can read and act on.
    // Agent attention, Automations, Calibration, Anomalies and Trust Tiers were
    // removed 2026-09-26: they surfaced one deployment's instruments (its job
    // census, its calibration research, a capped anomaly sample, identity
    // churn) as if they were product state. An operator who wants them back
    // adds them as a dashboard extension (dashboard/EXTENSIONS.md).
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
    const cards = [
      // Class DERIVED, not hardcoded. This was `cls: "up"` from the original
      // redesign scaffold — the only card of nine that did not compute its own
      // state — so it painted green unconditionally. Live on 2026-08-28 it read
      // green while its own subtitle said "1 not checking in" and the attention
      // band beside it said "Doctor past check-in threshold".
      //
      // Neutral (""), never green, is the honest default here: the number is a
      // fleet mean of a metric whose between-agent sd is ~0.008, so it cannot
      // move enough to earn a health colour, and eisv.js states the standing
      // policy — "a neutral surface rather than converting observations into
      // red/green verdicts". What CAN be stated is cadence, which the subtitle
      // already computes: amber when a resident has stopped checking in.
      { h: "Fleet Coherence", id: "fleetcoh", num: num(fleet.coh), sub: fleet.sub,
        cls: fleet.part.down.length ? "down" : "", rule: true, href: "#eisv" },
      // Name the denominator's window: this card reads a 30-day registry
      // window and the Agents tab a 14-day one — two honest totals that read
      // as a contradiction when unlabelled.
      { h: "Agents", num: un(agentHeadline) ? "—" : agentHeadline, of: un(stats.agentsTotal) ? "" : "/ " + stats.agentsTotal, sub: agentSub, href: "#agents",
        title: un(stats.agentsTotal) ? ""
          : presenceBlind
            ? `${agentHeadline} registry-active of ${stats.agentsTotal} identities seen in the last 30 days. None holds a live binding/lease, and presence is unknown for ${presenceUnknown}. The Agents tab reads a 14-day window, so its total is smaller.`
            : `${agentHeadline} with a live binding/lease right now, of ${stats.agentsTotal} registry identities seen in the last 30 days. The Agents tab reads a 14-day window, so its total is smaller.` },
      { h: "Discoveries", num: un(stats.discoveries) ? "—" : stats.discoveries.toLocaleString(), sub: un(stats.discoveries) ? "unavailable" : (typeof stats.discoveriesToday === "number" ? "+" + stats.discoveriesToday + " today" : "knowledge graph"), href: "#discoveries" },
      { h: "Dialectic", num: un(stats.dialectic) ? "—" : stats.dialectic, sub: un(stats.dialectic) ? "unavailable"
          : (stats.dialectic ? "open sessions"
            : typeof stats.dialecticRecent === "number" && stats.dialecticRecent
              ? `none open · ${typeof stats.dialecticFailed === "number" && stats.dialecticFailed ? `${stats.dialecticFailed} of ${stats.dialecticRecent} recent failed` : `${stats.dialecticRecent} recent`}`
              : "no open sessions"), href: "#dialectic" },
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
    if (subEl) {
      subEl.textContent = fleet.sub; // same string renderStats produces
      // and the same class rule — otherwise the 10s refresh updates the words
      // while leaving the colour frozen at whatever the first render set.
      subEl.className = "sub " + (fleet.part.down.length ? "down" : "");
    }
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

  // Full first render — light (residents/pulse/health) + heavy (stats) together.
  async function render() {
    const [health, residents, stats] = await Promise.all([DATA.health(), DATA.residents(), DATA.stats()]);
    seedResidents(residents.data, residents.source);
    const view = viewResidents();
    applyHealth(health);
    renderResidents(view, residents.source);
    renderStats(stats.data, view, stats.source);
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

  // Heavy refresh (slow cadence) — the headline batch; reuse the resident
  // model for fleet coherence rather than refetching it.
  async function refreshStats() {
    // refresh() re-reads residents and health, but it runs only on stream
    // events or while the stream is down. With the stream open and quiet (a
    // fresh install, nothing checking in), one failed read of either was
    // never retried and its fallback stayed on screen. Retry here, on the
    // stats cadence, until both answer live.
    if (lastSource !== "live" || lastHealthSource !== "live") await refresh();
    const stats = await DATA.stats();
    if (!RMODEL.length) { const residents = await DATA.residents(); seedResidents(residents.data, residents.source); }
    renderStats(stats.data, viewResidents(), lastSource);
  }

  window.Landing = { render, refresh, refreshStats, applyEvent, tickSilence };
})();
