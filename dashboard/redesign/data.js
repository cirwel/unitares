/*
 * Data layer — live-or-snapshot.
 * --------------------------------------------------------
 * Each accessor tries the live governance endpoint (same helpers the
 * production dashboard uses: bearer-token authFetch for REST, callTool
 * for /v1/tools/call) and falls back to the bundled SNAPSHOT when the
 * call fails or returns nothing (e.g. opened as a file, cross-origin, or
 * server down). Views never touch fetch directly — they await these and
 * read `.source` ('live' | 'snapshot') to badge freshness.
 *
 * This is the ONE seam between "renders portably now" and "wired to live
 * when served same-origin." No view changes when the seam flips.
 */
(function () {
  "use strict";

  function token() {
    try {
      const u = new URLSearchParams(location.search).get("token");
      return u || localStorage.getItem("unitares_api_token") || null;
    } catch { return null; }
  }

  async function authFetch(path, opts) {
    opts = opts || {};
    const headers = Object.assign({}, opts.headers);
    const t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    const r = await fetch(path, Object.assign({}, opts, { headers }));
    if (!r.ok) throw new Error(path + " -> " + r.status);
    return r.json();
  }

  async function callTool(name, args) {
    const body = JSON.stringify({ name, arguments: args || {} });
    const j = await authFetch("/v1/tools/call", {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    });
    return j.result !== undefined ? j.result : j;
  }

  // wrap an accessor so any failure degrades to snapshot, tagged.
  async function withFallback(liveFn, snapFn) {
    try {
      const v = await liveFn();
      if (v == null) throw new Error("empty");
      return { source: "live", data: v };
    } catch {
      return { source: "snapshot", data: snapFn() };
    }
  }

  const S = () => window.SNAPSHOT;

  const DATA = {
    async health() {
      return withFallback(async () => {
        const h = await authFetch("/health");
        return { version: h.version, uptime: h.uptime && h.uptime.formatted, db: h.database && h.database.status };
      }, () => S().health);
    },

    async residents() {
      return withFallback(async () => {
        const j = await authFetch("/v1/residents");
        if (!j || !j.residents) return null;
        return j.residents.map((r) => ({
          name: r.label, status: r.status, coherence: r.coherence, risk: r.risk_score,
          verdict: r.verdict, eisv: r.eisv, silence: r.silence_seconds,
          silenceThreshold: r.silence_threshold_seconds, event_driven: r.event_driven === true,
        }));
      }, () => S().residents);
    },

    // Headline telemetry aggregator. One coordinated parallel batch; each card
    // is derived from its authoritative source and degrades to the snapshot
    // value if that one source is unreachable. Fleet Coherence is NOT here — it
    // is derived from the live residents in the landing view.
    //   agents/tiers  ← agent(list)            stuck       ← detect_stuck_agents
    //   discoveries   ← knowledge(stats)        calibration ← calibration(check)
    //   dialectic     ← dialectic(list)         anomalies   ← detect_anomalies
    //   systemHealth  ← /health/deep
    async stats() {
      const snap = S().stats;
      const tc = (n, a) => callTool(n, a).catch(() => null);
      const rest = (p) => authFetch(p).catch(() => null);
      return withFallback(async () => {
        const [agentsR, kgR, dlcR, stuckR, calR, anomR, healthR, tierR] = await Promise.all([
          tc("agent", { action: "list", include_metrics: false, recent_days: 30, limit: 1, status_filter: "all" }), // summary only
          tc("knowledge", { action: "stats" }),
          tc("dialectic", { action: "list", limit: 50 }),
          tc("detect_stuck_agents", {}),
          tc("calibration", { action: "check" }),
          tc("detect_anomalies", {}),
          rest("/health/deep"),
          rest("/v1/agents/tier_distribution"),
        ]);
        if (![agentsR, kgR, dlcR, stuckR, calR, anomR, healthR, tierR].some(Boolean)) return null;

        // Agent counts from the (light) summary; trust-tier distribution from the
        // full-fleet aggregate endpoint (cached tier across all identities). The
        // bars show the EARNED tiers — unknown dwarfs them ~18× at fleet scale, so
        // it's reported as context, not a bar.
        let agentsActive = snap.agentsActive, agentsTotal = snap.agentsTotal;
        if (agentsR && agentsR.summary) {
          agentsTotal = agentsR.summary.total;
          agentsActive = (agentsR.summary.by_status || {}).active;
        }
        let trustTiers = snap.trustTiers, trustEarned = null, trustFleet = null, trustUnknown = null;
        if (tierR && tierR.tiers) {
          const t = tierR.tiers;
          trustTiers = ["verified", "established", "emerging", "provisional"].map((k) => ({ tier: k, n: t[k] || 0 }));
          trustEarned = tierR.earned;
          trustFleet = tierR.total;
          trustUnknown = t.unknown || 0;
        }

        const kg = kgR ? (kgR.stats || kgR) : null;
        const dlcSessions = dlcR && Array.isArray(dlcR.sessions) ? dlcR.sessions : null;
        const hb = healthR && healthR.status_breakdown ? healthR.status_breakdown : null;

        return {
          agentsActive, agentsTotal, trustTiers, trustEarned, trustFleet, trustUnknown,
          discoveries: kg && typeof kg.total_discoveries === "number" ? kg.total_discoveries : snap.discoveries,
          discoveriesToday: null, // no honest live "today" delta; show neutral subtitle
          dialectic: dlcSessions ? dlcSessions.filter((s) => !["resolved", "failed"].includes(s.phase || s.status)).length : snap.dialectic,
          stuck: stuckR ? (stuckR.stuck_agents || []).length : snap.stuck,
          calibration: calR && typeof calR.trajectory_health === "number" ? calR.trajectory_health : snap.calibration,
          anomalies: anomR && anomR.summary ? anomR.summary.total_anomalies : snap.anomalies,
          systemHealth: healthR ? (healthR.status === "healthy" ? "OK" : healthR.status) : snap.systemHealth,
          systemHealthDetail: hb ? `${hb.healthy || 0} ok · ${hb.warning || 0} warn${hb.error ? " · " + hb.error + " err" : ""}` : null,
        };
      }, () => snap);
    },

    async agents() {
      return withFallback(async () => {
        const r = await callTool("agent", {
          action: "list", include_metrics: true, recent_days: 14, limit: 200, status_filter: "all", grouped: true,
        });
        if (!r || !r.agents) return null;
        const groups = r.agents;
        const flat = [];
        Object.keys(groups).forEach((status) => {
          (groups[status] || []).forEach((a) => {
            if (!a || typeof a !== "object") return; // skip the "... (N more items)" truncation marker
            const m = a.metrics || {};
            flat.push({
              agent_id: a.agent_id, label: a.label, status: a.lifecycle_status || a.status || status,
              tier: typeof a.trust_tier === "string" ? a.trust_tier : a.trust_tier, updates: a.total_updates || 0,
              last: a.last_update || a.created, purpose: a.purpose, tags: a.tags || [],
              event_driven: a.event_driven === true, health: a.health_status,
              redacted: a.agent_id_redacted === true, parent: a.parent_agent_id,
              superseded: a.superseded === true, lifecycleReason: a.last_lifecycle_reason,
              metrics: { coherence: m.coherence, risk: m.risk_score, verdict: m.verdict, E: m.E, I: m.I, S: m.S, V: m.V, basin: m.basin, phi: m.phi },
            });
          });
        });
        const s = r.summary || {};
        return {
          list: flat,
          summary: { total: s.total, active: (s.by_status || {}).active, archived: (s.by_status || {}).archived,
            paused: (s.by_status || {}).paused, participated: s.participated, neverParticipated: s.never_participated },
        };
      }, () => ({ list: S().agentsList, summary: S().agentsSummary }));
    },

    async discoveries(query) {
      return withFallback(async () => {
        // Entry list + KG aggregates (for the lifecycle bar + type legend) in parallel.
        const [r, statsR] = await Promise.all([
          callTool("knowledge", query
            ? { action: "search", query, include_details: true, limit: 30 }
            : { action: "search", include_details: true, limit: 30 }), // no query → recent-first
          callTool("knowledge", { action: "stats" }).catch(() => null),
        ]);
        const items = r && (r.discoveries || r.results || (Array.isArray(r) ? r : null));
        if (!items) return null;
        const list = items.map((d) => ({
          id: d.id || d.created_at || d.timestamp, type: d.type || d.discovery_type || "note",
          status: d.status || "open", by: d.by || d.agent_id || d._agent_id, tags: d.tags || [],
          summary: d.summary || "Untitled", details: d.details || d.content || d.discovery || "",
          stale: !!d.staleness_warning,
        }));
        const st = statsR ? (statsR.stats || statsR) : null;
        return {
          list,
          total: st && typeof st.total_discoveries === "number" ? st.total_discoveries : r.total,
          byType: st ? st.by_type : null,
          byStatus: st ? st.by_status : null,
        };
      }, () => {
        const d = S().discoveries;
        return { list: d.list, total: d.total, byType: d.byType, byStatus: d.byStatus };
      });
    },

    async dialectic() {
      return withFallback(async () => {
        const r = await callTool("dialectic", { action: "list", limit: 50 });
        if (!r || !r.sessions) return null;
        const sessions = r.sessions.map((s) => ({
          id: s.session_id, phase: s.phase || s.status, type: s.session_type || "review",
          paused: (s.paused_agent || s.paused_agent_id || "").slice(0, 8), reviewer: (s.reviewer || s.reviewer_agent_id || "") ? (s.reviewer || s.reviewer_agent_id).slice(0, 8) : null,
          synthesizer: s.synthesizer, topic: s.topic || s.reason || "", created: s.created || s.created_at, msgs: s.message_count || 0,
          resolution: s.resolution ? { action: s.resolution.action || s.resolution.type, reasoning: s.resolution.reasoning || s.resolution.reason, conditions: (s.resolution.conditions || []).length, rootCause: s.resolution.root_cause } : null,
        }));
        const c = { total: sessions.length, resolved: 0, active: 0, failed: 0 };
        sessions.forEach((s) => { if (["resolved"].includes(s.phase)) c.resolved++; else if (["failed", "escalated"].includes(s.phase)) c.failed++; else c.active++; });
        return { sessions, counts: c };
      }, () => ({ sessions: S().dialectic.sessions, counts: S().dialectic.counts }));
    },

    async activity() {
      return withFallback(async () => {
        const [ev, act] = await Promise.all([authFetch("/api/events?limit=40"), authFetch("/api/activity?window=60&bucket=5")]);
        if (!ev || !ev.events) return null;
        const events = ev.events.map((e) => ({
          type: e.type, severity: e.severity, agent: e.agent_name || e.agent_id, ts: e.timestamp || e.ts,
          message: e.message, vclass: e.violation_class,
        }));
        const buckets = (act && act.buckets ? act.buckets : []).map((b) => ({ p: b.proceed || 0, g: b.guide || 0, x: b.pause || 0 }));
        return { events, buckets, windowMin: (act && act.window_minutes) || 60, bucketMin: (act && act.bucket_minutes) || 5 };
      }, () => S().activity);
    },

    async eisv() {
      return withFallback(async () => {
        const r = await authFetch("/v1/eisv/recent?limit=120");
        const evs = (r && r.events) || [];
        if (!evs.length) return null;
        // fleet-average into 1-min buckets (mirrors eisv-charts __fleet__)
        const buckets = {};
        evs.forEach((e) => {
          const ts = e.timestamp || "";
          if (ts.length < 16) return;
          const k = ts.slice(11, 16);
          const m = e.eisv || {};
          (buckets[k] || (buckets[k] = [])).push({ E: m.E, I: m.I, S: m.S, V: m.V, C: e.coherence, R: e.risk });
        });
        const avg = (xs, f) => { const v = xs.map((x) => x[f]).filter((n) => typeof n === "number"); return v.length ? v.reduce((a, n) => a + n, 0) / v.length : null; };
        const series = Object.keys(buckets).sort().slice(-20).map((t) => {
          const xs = buckets[t];
          return { t, E: avg(xs, "E"), I: avg(xs, "I"), S: avg(xs, "S"), V: avg(xs, "V"), C: avg(xs, "C"), R: avg(xs, "R") };
        });
        return { series, coherenceEq: 0.5 };
      }, () => S().eisv);
    },

    async agentHistory(id, opts) {
      // EISV check-in trajectory for one agent (no snapshot fallback — empty if offline).
      // opts: { limit, mode: "recent"|"all" }. Returns { points, total, mode }.
      opts = opts || {};
      return withFallback(async () => {
        const q = "?limit=" + (opts.limit || 200) + (opts.mode === "all" ? "&mode=all" : "");
        const r = await authFetch("/v1/agents/" + encodeURIComponent(id) + "/history" + q);
        return r && Array.isArray(r.points)
          ? { points: r.points, total: r.total || r.points.length, mode: r.mode || "recent" }
          : null;
      }, () => ({ points: [], total: 0, mode: "recent" }));
    },

    async residentPanels() {
      return withFallback(async () => {
        const [w, sn, vg, h, res] = await Promise.all([
          authFetch("/v1/watcher/summary").catch(() => null),
          authFetch("/v1/sentinel/summary").catch(() => null),
          authFetch("/v1/vigil/summary").catch(() => null),
          authFetch("/health/deep").catch(() => null),
          authFetch("/v1/residents").catch(() => null),
        ]);
        if (!w && !sn && !vg && !h && !res) return null;
        const out = {};
        if (w) out.watcher = { total: w.total, byStatus: w.by_status || {}, openSev: w.by_severity_open || {},
          patterns: (w.patterns || []).map((p) => ({ p: p.pattern, confirmed: p.confirmed, dismissed: p.dismissed, surfaced: p.surfaced, ratio: p.dismiss_ratio })) };
        if (sn) out.sentinel = { total: sn.total, bySeverity: sn.by_severity || {},
          byClass: (sn.by_violation_class || []).map((c) => ({ c: c.violation_class, n: c.count })),
          recent: (sn.recent || []).map((r) => ({ ts: r.timestamp, severity: r.severity, vclass: r.violation_class, type: r.finding_type, message: r.message })) };
        if (vg && vg.stats) out.vigil = { cycles24h: vg.stats.cycles_24h, writesWindow: vg.stats.total_writes_in_window, lastVerdict: vg.stats.last_verdict,
          lastCycleAgeS: vg.stats.last_cycle_age_seconds, avgCoherence: vg.stats.avg_coherence_window,
          eisv: vg.cycles && vg.cycles[0] ? vg.cycles[0] : null };
        if (h) out.health = { status: h.status, version: h.version, checks: h.status_breakdown || {},
          breakers: { governance: (h.circuit_breakers && h.circuit_breakers.governance || {}).trips_24h || 0, redis: (h.circuit_breakers && h.circuit_breakers.redis || {}).trips_24h || 0 },
          calibration: (h.checks && h.checks.calibration || {}).status, redis: h.redis_present, continuity: h.identity_continuity_mode };
        // Chronicler has no dedicated summary endpoint — pull its live state from
        // /v1/residents (daily resident; cadence-aware rendering happens in the view).
        const c = res && res.residents && res.residents.find((r) => r.label === "Chronicler");
        out.chronicler = c
          ? { status: c.status, silence: c.silence_seconds, silenceThreshold: c.silence_threshold_seconds,
              lastCheckin: c.last_checkin_at, eisv: c.eisv, coherence: c.coherence }
          : S().residentPanels.chronicler;
        return out;
      }, () => S().residentPanels);
    },
  };

  window.DATA = DATA;
})();
