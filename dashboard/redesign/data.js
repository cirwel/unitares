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

  // Scheduled probe families, by paused-agent label. The daily canary is
  // synthetic traffic and now requires a terminal `resolved` verdict before
  // its JSONL result is green. Counting probes alongside organic sessions
  // would still distort review volume and outcome rates.
  // canary_dialectic_* and RP*/AgreeRateProbe are different families testing
  // different things; neither is organic traffic.
  function isProbe(label) {
    if (!label) return false;
    return /^(canary_dialectic|RP\d|RateProbe|AgreeRateProbe)/i.test(label);
  }

  // Normalise a dialectic `resolution` into something renderable, or null.
  // Two shapes have to be rejected rather than shown as an empty disclosure:
  // the literal string "{}" (sessions resolved between 2026-06-28 and
  // 2026-08-10 stored a double-encoded jsonb string, so the server unwraps one
  // layer and hands back a string), and an object carrying no usable field.
  function resolutionOf(res) {
    if (!res || typeof res !== "object" || Array.isArray(res)) return null;
    const out = {
      action: res.action || res.type,
      reasoning: res.reasoning || res.reason,
      conditions: (res.conditions || []).length,
      rootCause: res.root_cause,
    };
    return out.action || out.reasoning || out.rootCause || out.conditions ? out : null;
  }

  // Operator write credential (X-Unitares-Operator). Provision once via
  // ?operator_token=… — persisted to localStorage and scrubbed from the URL
  // (same handoff pattern the classic dashboard used, #643).
  function operatorToken() {
    try {
      const params = new URLSearchParams(location.search);
      const fromUrl = params.get("operator_token");
      if (fromUrl) {
        localStorage.setItem("unitares_operator_token", fromUrl);
        params.delete("operator_token");
        const qs = params.toString();
        history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
      }
      return localStorage.getItem("unitares_operator_token") || null;
    } catch { return null; }
  }

  async function authFetch(path, opts) {
    opts = opts || {};
    const headers = Object.assign({}, opts.headers);
    const t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    const r = await fetch(path, Object.assign({}, opts, {
      credentials: "same-origin",
      headers,
    }));
    if (r.status === 401 && !t) {
      location.assign("/auth/signin");
      throw new Error(path + " -> 401 (sign-in required)");
    }
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

  // Wrap an accessor so any failure degrades to its fallback, tagged with what
  // the fallback actually is: "snapshot" when the bundled snapshot backs it
  // (offline/preview renders, see SNAPSHOT_FALLBACK below), otherwise
  // "unavailable" — the producer did not answer, which is not the same claim
  // as a snapshot that recorded nothing.
  async function withFallback(liveFn, snapFn) {
    try {
      const v = await liveFn();
      if (v == null) throw new Error("empty");
      return { source: "live", data: v };
    } catch {
      return { source: SNAPSHOT_FALLBACK ? "snapshot" : "unavailable", data: snapFn() };
    }
  }

  // Never throws. snapshot.js can legitimately be absent (it is auth-gated and
  // a <script src> carries no bearer token), and a fallback that raises turns a
  // recoverable "no offline copy" into a dead page.
  //
  // The snapshot is a capture of ONE deployment's fleet, bundled for offline
  // and design renders. On a page served by a UNITARES server, a failed live
  // call means THIS server did not answer (a restart, a blip), and another
  // deployment's residents, EISV and version must not stand in for it: on
  // 2026-09-26 a fresh install's Overview showed the bundled fleet's residents
  // as its own after one failed /v1/residents during a restart. So the
  // snapshot backs the fallback only when there is no server to ask — the page
  // opened from a file — or when a design preview asks for it with ?snapshot=1.
  // Everywhere else the fallback yields nothing and the views render
  // "unavailable", the same path a bearer-only operator already takes.
  const SNAPSHOT_FALLBACK = location.protocol === "file:"
    || new URLSearchParams(location.search).get("snapshot") === "1";
  const S = () => (SNAPSHOT_FALLBACK && window.SNAPSHOT) || {};

  function eisvMeasurementSource(event) {
    const telemetry = (event && (event.eisv_telemetry || event.telemetry)) || {};
    return telemetry.measurement_source || telemetry.behavioral_source ||
      telemetry.submitted_source || telemetry.primary_source ||
      (event && event.metrics && event.metrics.primary_eisv_source) || "unknown";
  }

  // Fleet-average raw eisv_update events into 1-min buckets (last 20), with an
  // optional source filter. "all" remains available for continuity but is
  // labeled mixed in the view; a named lane never averages across instruments.
  function bucketEisv(evs, source) {
    const buckets = {};
    (evs || []).forEach((e) => {
      if (source && source !== "all" && eisvMeasurementSource(e) !== source) return;
      const ts = e.timestamp || "";
      if (ts.length < 16) return;
      const k = ts.slice(11, 16);
      const m = e.eisv || e || {};
      (buckets[k] || (buckets[k] = [])).push({ E: m.E, I: m.I, S: m.S, V: m.V, C: e.coherence, R: e.risk });
    });
    const avg = (xs, f) => { const v = xs.map((x) => x[f]).filter((n) => typeof n === "number"); return v.length ? v.reduce((a, n) => a + n, 0) / v.length : null; };
    return Object.keys(buckets).sort().slice(-20).map((t) => {
      const xs = buckets[t];
      return { t, E: avg(xs, "E"), I: avg(xs, "I"), S: avg(xs, "S"), V: avg(xs, "V"), C: avg(xs, "C"), R: avg(xs, "R") };
    });
  }

  // Event-weighted summaries grouped by the consumed measurement source. This
  // table is intentionally source-separated: no value in one lane incorporates
  // a physical/behavioral/fallback observation from another lane.
  function summarizeEisvSources(evs) {
    const lanes = {};
    (evs || []).forEach((event) => {
      const source = eisvMeasurementSource(event);
      const telemetry = event.eisv_telemetry || event.telemetry || {};
      const m = event.eisv || event || {};
      const lane = lanes[source] || (lanes[source] = {
        source, events: 0, values: { E: [], I: [], S: [], V: [] },
        confidence: [], missingObservations: 0, unknownProvenance: 0, missingInputs: new Set(),
        enforcementRequested: 0, enforcementApplied: 0, latest: null,
      });
      lane.events += 1;
      ["E", "I", "S", "V"].forEach((key) => {
        if (typeof m[key] === "number" && Number.isFinite(m[key])) lane.values[key].push(m[key]);
      });
      if (typeof telemetry.behavioral_confidence === "number" && Number.isFinite(telemetry.behavioral_confidence)) lane.confidence.push(telemetry.behavioral_confidence);
      const hasMissingInputs = Array.isArray(telemetry.missing_inputs);
      if (!hasMissingInputs) lane.unknownProvenance += 1;
      const missing = hasMissingInputs ? telemetry.missing_inputs : [];
      if (missing.length) lane.missingObservations += 1;
      missing.forEach((name) => lane.missingInputs.add(name));
      if (telemetry.enforcement_requested === true) lane.enforcementRequested += 1;
      if (telemetry.enforcement_applied === true) lane.enforcementApplied += 1;
      const timestamp = event.timestamp || event.t;
      if (timestamp && Number.isFinite(Date.parse(timestamp)) && (!lane.latest || Date.parse(timestamp) > Date.parse(lane.latest))) lane.latest = timestamp;
    });
    const mean = (values) => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    return Object.values(lanes).map((lane) => ({
      source: lane.source,
      events: lane.events,
      E: mean(lane.values.E), I: mean(lane.values.I),
      S: mean(lane.values.S), V: mean(lane.values.V),
      confidence: mean(lane.confidence),
      missingObservations: lane.missingObservations,
      unknownProvenance: lane.unknownProvenance,
      missingInputs: Array.from(lane.missingInputs).sort(),
      enforcementRequested: lane.enforcementRequested,
      enforcementApplied: lane.enforcementApplied,
      latest: lane.latest,
    })).sort((a, b) => b.events - a.events || a.source.localeCompare(b.source));
  }

  // Normalise one detect_stuck_agents entry for the views. `public_agent_id` is
  // the SAME redacted handle agent(action=list) emits (the client never sees the
  // registry UUID), so it is the only usable join key back to an agent row.
  function mapStuck(s) {
    return {
      id: s.public_agent_id || null, name: s.agent_name || s.public_agent_id || null,
      reason: s.reason, soft: s.soft === true, details: s.details || "",
    };
  }

  const DATA = {
    // Whether a failed live read may fall back to the bundled snapshot (see S).
    snapshotFallback: SNAPSHOT_FALLBACK,
    bucketEisv,
    eisvMeasurementSource,
    summarizeEisvSources,

    // ── THE resident-liveness predicate ──────────────────────────────────────
    // One question, one answer. `status` is the server's authoritative rollup
    // (src/http_api.py http_residents) — cadence-relative, computed from the
    // NEWER of agent_metadata.last_update and the EISV event, so two panels
    // cannot disagree after a broadcaster gap.
    //
    // Three-way ON PURPOSE. "alive but no EISV recoverable" is a real state the
    // server already represents (status="healthy" with coherence=null); it must
    // not read as dead, and it must not count toward an EISV mean. Collapsing it
    // either way is what made the Overview say "5 of 6 reporting" while all six
    // residents were alive.
    //
    // A plain function, not an accessor: no fetch, no seam to cross, no
    // snapshot mock. Callers pass the shape DATA.residents() /
    // DATA.residentFreshness() already return.
    residentLiveness(r) {
      if (!r) return "down";
      if (["silent", "paused", "archived"].indexOf(r.status) !== -1) return "down";
      return r.coherence != null ? "reporting" : "alive-no-eisv";
    },

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
          id: r.agent_id, name: r.label, status: r.status, coherence: r.coherence, risk: r.risk_score,
          verdict: r.verdict, eisv: r.eisv, silence: r.silence_seconds,
          silenceThreshold: r.silence_threshold_seconds, event_driven: r.event_driven === true,
        }));
      }, () => S().residents);
    },

    // Headline telemetry aggregator. One coordinated parallel batch; each card
    // is derived from its authoritative source and degrades to the snapshot
    // value if that one source is unreachable. Fleet Coherence is NOT here — it
    // is derived from the live residents in the landing view.
    //   agents        ← agent(list)             dialectic    ← dialectic(list)
    //   discoveries   ← knowledge(stats)        systemHealth ← /health/deep
    // Stuck, calibration, anomaly and trust-tier cards were removed from the
    // Overview (2026-09-26): each read as internal instrument state rather than
    // something an operator of any install acts on, and together they were
    // four of the eight calls on the default page. Stuck detection still
    // feeds the Agents pane via stuckAgents().
    async stats() {
      const tc = (n, a) => callTool(n, a).catch(() => null);
      const rest = (p) => authFetch(p).catch(() => null);
      return withFallback(async () => {
        const [agentsR, kgR, dlcR, healthR] = await Promise.all([
          tc("agent", { action: "list", include_metrics: false, recent_days: 30, limit: 1, status_filter: "all" }), // summary only
          tc("knowledge", { action: "stats" }),
          // fields=compact: this batch reads phase/status off 50 sessions to
          // produce two counts. The full shape ships `resolution` (~629 B each)
          // and was 130,936 B for 1,114 B of consumed fields — on the default
          // page. Compact is a strict subset, so the mapping below is unchanged.
          tc("dialectic", { action: "list", limit: 50, fields: "compact" }),
          rest("/health/deep"),
        ]);
        if (![agentsR, kgR, dlcR, healthR].some(Boolean)) return null;

        // Live path: a sub-tool that fails is NULL, not a stale snapshot value.
        // Showing the bundled snapshot under a "live" badge would read as a current
        // metric — the card renders "—" (unavailable) instead, and `degraded`
        // flags how many sources didn't answer. The whole-accessor snapshot
        // fallback (() => snap, honestly badged "snapshot") only fires when EVERY
        // source failed.
        let agentsActive = null, agentsLive = null, agentsPresenceUnknown = null;
        let agentsPresenceUnavailable = null, agentsTotal = null;
        if (agentsR && agentsR.summary) {
          agentsTotal = agentsR.summary.total;
          agentsActive = (agentsR.summary.by_status || {}).active;
          const presence = agentsR.summary.by_presence;
          if (presence) {
            agentsLive = presence.live;
            agentsPresenceUnknown = presence.unknown;
            agentsPresenceUnavailable = presence.unavailable;
          }
        }
        const kg = kgR ? (kgR.stats || kgR) : null;
        const dlcSessions = dlcR && Array.isArray(dlcR.sessions) ? dlcR.sessions : null;
        const hb = healthR && healthR.status_breakdown ? healthR.status_breakdown : null;

        return {
          agentsActive, agentsLive, agentsPresenceUnknown, agentsPresenceUnavailable,
          agentsTotal,
          discoveries: kg && typeof kg.total_discoveries === "number" ? kg.total_discoveries : null,
          discoveriesToday: null, // no honest live "today" delta; show neutral subtitle
          dialectic: dlcSessions ? dlcSessions.filter((s) => !["resolved", "failed"].includes(s.phase || s.status)).length : null,
          // Recent-outcome context for the card: "0 open" alone reads as
          // all-quiet even when most recent sessions failed.
          dialecticRecent: dlcSessions ? dlcSessions.length : null,
          dialecticFailed: dlcSessions ? dlcSessions.filter((s) => (s.phase || s.status) === "failed").length : null,
          systemHealth: healthR ? (healthR.status === "healthy" ? "OK" : healthR.status) : null,
          // Name every non-healthy bucket the headline status is derived from.
          // Omitting degraded/unavailable read "moderate · 10 ok · 0 warn" on a
          // fresh install, a status the detail line appeared to contradict.
          systemHealthDetail: hb ? `${hb.healthy || 0} ok · ${hb.warning || 0} warn`
            + (hb.degraded ? ` · ${hb.degraded} degraded` : "")
            + (hb.unavailable ? ` · ${hb.unavailable} unavailable` : "")
            + (hb.error ? ` · ${hb.error} err` : "") : null,
          degraded: [agentsR, kgR, dlcR, healthR].filter((x) => !x).length,
        };
      // LAZY, deliberately. This used to read `const snap = S().stats` as the
      // first statement of stats(), before any try — so it touched the snapshot
      // even when every live call was about to succeed.
      //
      // That is fatal over the tunnel. snapshot.js is auth-gated ON PURPOSE (it
      // bundles resident ids, EISV vectors, verdicts — the same data class
      // /v1/eisv/* is gated for) and app.html loads it with a plain
      // <script src>, which sends COOKIES and not the bearer token. So an
      // operator authenticated by bearer gets 200 on every REST call and 401 on
      // snapshot.js: window.SNAPSHOT stays undefined, `S().stats` throws
      // TypeError, and because render() awaits Promise.all the whole Overview
      // dies before renderStats runs — while refresh() (health + residents only,
      // both lazily-fallen-back) keeps painting the resident strip and pulse.
      //
      // Observed exactly that on 2026-08-28: resident fleet and pulse visible,
      // all nine headline cards missing. An unreachable FALLBACK must never be
      // able to break the path that did not need it.
      // `|| {}` so a MISSING snapshot degrades to a card grid of "—" rather than
      // handing renderStats an undefined it dereferences. Making S() non-throwing
      // only moved the crash one frame down; the consumer needs a shape, not a
      // hole.
      }, () => S().stats || {});
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
              // trust_tier is the tier NAME (lifecycle/query.py emits
              // tier_info["name"]); `null` for agents with no computed tier —
              // the badge distinguishes "unknown" from "not computed".
              tier: a.trust_tier,
              // Compatibility fallback for servers before observation_count.
              // This is a state-row count, never an authored-activity count.
              updates: a.observation_count ?? a.total_updates ?? 0,
              last: a.last_update || a.created, purpose: a.purpose, tags: a.tags || [],
              event_driven: a.event_driven === true, health: a.health_status,
              redacted: a.agent_id_redacted === true, parent: a.parent_agent_id,
              superseded: a.superseded === true, lifecycleReason: a.last_lifecycle_reason,
              presence: a.presence || { status: "unavailable", signals: [] },
              metrics: { coherence: m.coherence, risk: m.risk_score, riskSource: m.risk_score_source,
                phiRiskCurrent: m.phi_risk_current ?? m.current_risk,
                phiRiskMean: m.phi_risk_mean ?? m.mean_risk,
                verdict: m.verdict, verdictSource: m.verdict_resolution_source,
                E: m.E, I: m.I, S: m.S, V: m.V, basin: m.basin, phi: m.phi,
                source: m.source, recordedAt: m.recorded_at,
                rollingMetricsAvailable: m.rolling_metrics_available },
            });
          });
        });
        const s = r.summary || {};
        return {
          list: flat,
          summary: { total: s.total, active: (s.by_status || {}).active, archived: (s.by_status || {}).archived,
            paused: (s.by_status || {}).paused, observed: s.observed ?? s.participated,
            unobserved: s.unobserved ?? s.never_participated,
            live: s.by_presence && s.by_presence.live,
            presenceUnknown: s.by_presence && s.by_presence.unknown,
            presenceUnavailable: s.by_presence && s.by_presence.unavailable },
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
          // provenance the card now exposes
          agentId: d._agent_id, session: d.session_id_at_write, version: d.system_version, created: d.created_at,
        }));
        const st = statsR ? (statsR.stats || statsR) : null;
        return {
          list,
          total: st && typeof st.total_discoveries === "number" ? st.total_discoveries : r.total,
          byType: st ? st.by_type : null,
          byStatus: st ? st.by_status : null,
        };
      }, () => {
        const d = S().discoveries || {};
        return { list: d.list || [], total: d.total, byType: d.byType, byStatus: d.byStatus };
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
          // The list carries a ~280-char topic preview; get has the full text.
          topicTruncated: !!s.topic_truncated,
          awaiting: !!s.awaiting_facilitation,
          probe: isProbe(s.paused_agent_label),
          resolution: resolutionOf(s.resolution),
        }));
        // `awaiting_facilitation` means the session asked for a human. It does
        // NOT mean one can still be assigned: reassign is refused in any phase
        // but THESIS/ANTITHESIS, and every such session on record has already
        // been swept to `failed` by the stuck-session timer (38 of 38 as of
        // 2026-08-10). So these are unfacilitated failures, not a work queue —
        // they stay in the failed count, and get their own label because "why
        // it failed" is the useful part. Do not relabel them as pending work:
        // there is no action available, and prior work already established
        // that a badge alone changes nothing (36 flagged, 35 failed, 2 reassign
        // messages in the entire DB).
        const c = { total: sessions.length, resolved: 0, active: 0, failed: 0, unfacilitated: 0, probes: 0 };
        sessions.forEach((s) => {
          if (s.probe) c.probes++;
          if (["resolved"].includes(s.phase)) c.resolved++;
          else if (["failed", "escalated"].includes(s.phase)) {
            c.failed++;
            // Probes are excluded from the defect count only — they stay in
            // `failed`, because they DID fail; they just were never going to
            // do anything else.
            if (s.awaiting && !s.probe) c.unfacilitated++;
          } else c.active++;
        });
        return { sessions, counts: c };
      }, () => { const dl = S().dialectic || {}; return { sessions: dl.sessions || [], counts: dl.counts || {} }; });
    },

    async dialecticSession(id) {
      // Full transcript (thesis → antithesis → synthesis) + resolution for one
      // session — the history the list view hides behind message_count.
      return withFallback(async () => {
        const r = await callTool("dialectic", { action: "get", session_id: id });
        if (!r || !Array.isArray(r.transcript)) return null;
        return { transcript: r.transcript, resolution: r.resolution, reason: r.reason, recommended: r.recommended_action, topic: r.topic };
      }, () => null);
    },

    async activity() {
      return withFallback(async () => {
        const [ev, act, runtime] = await Promise.all([
          authFetch("/api/events?limit=40"),
          authFetch("/api/activity?window=60&bucket=5").catch(() => null),  // .catch: this route is auth-gated, and an un-caught rejection here
          // would fail the whole Promise.all — collapsing sibling panes that
          // had succeeded, a wider break than the gate intends.
          // limit=1000 is NOT a display cap and must not be tuned down to match
          // the view's slice(0, 30). It bounds the underlying AUDIT-EVENT scan
          // in read_runtime_activity(), and both `processes` and every `summary`
          // count are derived from that same bounded set — so shrinking it
          // silently turns fleet totals into page totals (the exact failure
          // db/mixins/audit.py:151 warns about). Leave it.
          authFetch("/v1/runtime/activity?window_hours=24&limit=1000").catch(() => null),
        ]);
        if (!ev || !ev.events) return null;
        const events = ev.events.map((e) => ({
          type: e.type, severity: e.severity, agent: e.agent_name || e.agent_id, ts: e.timestamp || e.ts,
          message: e.message, vclass: e.violation_class,
        }));
        const buckets = (act && act.buckets ? act.buckets : []).map((b) => ({ p: b.proceed || 0, g: b.guide || 0, x: b.pause || 0 }));
        const operational = runtime && runtime.success ? {
          available: true,
          source: "live",
          windowHours: runtime.window_hours || 24,
          summary: runtime.summary || {},
          processes: runtime.processes || [],
          semantics: runtime.semantics || {},
        } : { available: false, source: "unavailable", windowHours: 24, summary: {}, processes: [] };
        return { events, buckets, operational, windowMin: (act && act.window_minutes) || 60, bucketMin: (act && act.bucket_minutes) || 5 };
      }, () => S().activity || { events: [], buckets: [], operational: null, windowMin: 60, bucketMin: 5 });
    },

    async eisv() {
      return withFallback(async () => {
        // fields=compact: this view reads only eisv/coherence/risk/timestamp
        // plus the measurement-source tag, while the full event carries ~6.3 KB
        // of governance detail per row (decision, drift_trends, inputs,
        // risk_reason — zero references in this file or in sections/eisv.js).
        // Measured against the live ring buffer: 343,393 B -> 29,799 B, 91.3%.
        //
        // CORRECTION (verified 2026-08-28): an earlier version of this comment
        // said the saving recurred "every 10s per open tab". It does not.
        // app.html's refreshTick is `if (wsStatus !== "open") refreshActive()`
        // — a polling FALLBACK. Live updates normally arrive over /ws/eisv, so
        // the recurring cost is only paid while the socket is down and this tab
        // is active. The saving is real on every section load and on every
        // fallback poll; it is not a steady-state drip.
        //
        // Compact is a strict SUBSET, so the same parsing below works against
        // either shape and a server without the parameter returns the full event.
        const r = await authFetch("/v1/eisv/recent?limit=120&fields=compact");
        const evs = (r && r.events) || [];
        if (!r || !Array.isArray(r.events)) return null;
        // `raw` carries the unaveraged events so the section can keep
        // accumulating live pushes (same shape arrives over /ws/eisv) and
        // re-bucket the window itself, no refetch.
        return { series: bucketEisv(evs), raw: evs, sourceLanes: summarizeEisvSources(evs), coherenceEq: 0.5 };
      }, () => { const e = S().eisv || {}; return { series: e.series || [], raw: e.raw || [], sourceLanes: e.sourceLanes || [], coherenceEq: e.coherenceEq ?? 0.5 }; });
    },

    // Fleet risk history: trailing-7-day fleet-mean risk plus guide and pause
    // counts, one point per day, computed server-side from core.agent_state
    // (/v1/governance/trend). It used to read Chronicler's daily scrape, which
    // left the Risk tab empty on any install without that resident. The server
    // caps the window at 60 days, inside retained history.
    //
    // A successful response is live even when `risk` is empty: that is "the
    // producer ran and found no risk readings" (a fresh install), not an
    // outage, and its pause/guide series may still carry verdicts. Only a
    // failed or malformed response falls back.
    async riskTrend(days) {
      const d = Number.isFinite(days) ? Math.max(7, Math.min(60, Math.round(days))) : 60;
      return withFallback(async () => {
        const j = await authFetch("/v1/governance/trend?days=" + d);
        if (!j || !j.success || !Array.isArray(j.risk)) return null;
        return { windowDays: j.window_days || d, risk: j.risk, pause: j.pause || [], guide: j.guide || [] };
      }, () => S().riskTrend || { windowDays: d, risk: [], pause: [], guide: [] });
    },

    // Agents that checked in recently, newest first: one row per agent, the
    // latest state of each in the server's in-memory EISV ring, folded
    // server-side (/v1/eisv/agents). Any agent, not only residents, so the
    // Overview and the Risk picker are useful on an install that runs none.
    // `coverageStart` (epoch seconds) is where the ring's memory begins.
    async recentAgents() {
      return withFallback(async () => {
        const r = await authFetch("/v1/eisv/agents");
        if (!r || !Array.isArray(r.agents)) return null;
        const agents = r.agents.filter((e) => e && e.agent_id).map((e) => ({
          id: e.agent_id,
          name: e.agent_name || e.agent_id.slice(0, 8),
          ts: e.timestamp || null,
          eisv: e.eisv || null,
          coherence: typeof e.coherence === "number" ? e.coherence : null,
          risk: typeof e.risk === "number" ? e.risk : null,
          // The verdict is sub_action when present (a guided check-in is
          // action "proceed", sub_action "guide"), the rule the server persists by.
          action: (e.decision && (e.decision.sub_action || e.decision.action)) || null,
          checkins: e.checkins || 0,
        }));
        return { agents, coverageStart: typeof r.coverage_start === "number" ? r.coverage_start : null };
      }, () => S().recentAgents || { agents: [], coverageStart: null });
    },

    // Check-ins in the last hour by verdict, from the server's in-memory
    // activity ring (/api/activity). `coverageStart` (epoch seconds) is where
    // that ring's view of the window begins: after a restart, or once the ring
    // is full, the hour is only partly covered and the total says so.
    async checkinActivity() {
      return withFallback(async () => {
        const j = await authFetch("/api/activity?window=60&bucket=5");
        if (!j || !j.success || !Array.isArray(j.buckets)) return null;
        // Prefer the server's exact window totals: its buckets are aligned to
        // bucket boundaries and can start up to one bucket inside the hour.
        // Summing buckets is the fallback for an older server.
        let t = j.totals && typeof j.totals.proceed === "number" ? { proceed: j.totals.proceed, guide: j.totals.guide || 0, pause: j.totals.pause || 0 } : null;
        if (!t) {
          t = { proceed: 0, guide: 0, pause: 0 };
          j.buckets.forEach((b) => { t.proceed += b.proceed || 0; t.guide += b.guide || 0; t.pause += b.pause || 0; });
        }
        return { ...t, total: t.proceed + t.guide + t.pause, windowMin: j.window_minutes || 60,
          coverageStart: typeof j.coverage_start === "number" ? j.coverage_start : null };
      }, () => S().checkinActivity || null);
    },

    async agentHistory(id, opts) {
      // EISV state-observation trajectory for one agent (no snapshot fallback —
      // empty if offline). Authored reports and automatic substrate rows remain
      // separate in observationSummary; neither is inferred from the other.
      // opts: { limit, mode: "recent"|"all", includeTelemetry: true|"latest" }.
      opts = opts || {};
      return withFallback(async () => {
        const q = "?limit=" + (opts.limit || 200) +
          (opts.mode === "all" ? "&mode=all" : "") +
          (opts.includeTelemetry === "latest" ? "&include_telemetry=latest" : opts.includeTelemetry ? "&include_telemetry=true" : "");
        const r = await authFetch("/v1/agents/" + encodeURIComponent(id) + "/history" + q);
        return r && Array.isArray(r.points)
          ? { points: r.points, total: r.total || r.points.length, mode: r.mode || "recent",
              observationSummary: r.observation_summary || null,
              trajectoryContext: r.trajectory_context || null,
              telemetryMode: r.telemetry_mode || "none" }
          : null;
      }, () => {
        // Offline: serve a bundled trajectory if the snapshot carries one for
        // this agent, otherwise an honest empty result.
        const h = (S().agentHistory || {})[id];
        return h ? { points: h, total: h.length, mode: "all" } : { points: [], total: 0, mode: "recent" };
      });
    },

    operatorToken,

    // Read bearer, exposed so ws.js can put it in the /ws/eisv query string —
    // a browser cannot set headers on a WebSocket. Same credential authFetch
    // sends; exported rather than duplicated so the two cannot drift.
    apiToken: token,

    // Passkey security is live-only: rendering a snapshot of sessions or
    // credentials would be dangerously misleading. Views stay behind DATA,
    // but failures surface honestly instead of degrading to fixture data.
    async passkeySecurity() {
      return authFetch("/auth/sessions", {
        headers: { "X-Unitares-Csrf": "1" },
      });
    },

    async logoutDashboardSession() {
      return authFetch("/auth/logout", {
        method: "POST",
        headers: { "X-Unitares-Csrf": "1" },
      });
    },

    async revokeAllDashboardSessions() {
      return authFetch("/auth/sessions", {
        method: "POST",
        headers: { "X-Unitares-Csrf": "1" },
      });
    },

    async revokePasskey(credentialId) {
      const headers = { "X-Unitares-Csrf": "1" };
      const op = operatorToken();
      if (op) headers["X-Unitares-Operator"] = op;
      return authFetch("/auth/credentials/" + encodeURIComponent(credentialId) + "/revoke", {
        method: "POST",
        headers,
      });
    },

    async mintEnrollmentCode() {
      const op = operatorToken();
      if (!op) throw new Error("operator credential required");
      return authFetch("/auth/enroll", {
        method: "POST",
        headers: {
          "X-Unitares-Csrf": "1",
          "X-Unitares-Operator": op,
        },
      });
    },

    // Light freshness map for the residents (label -> {silence, status, coherence}).
    // The Agents pane uses it to keep a lease-anchored in-process resident
    // (zero agent_state rows by design, liveness lives in lease_plane
    // heartbeats) out of the unobserved bucket. `coherence`
    // rides along so the pane can apply DATA.residentLiveness — the SAME
    // predicate the Overview applies — instead of inferring liveness from the
    // mere presence of a silence number.
    async residentFreshness() {
      return withFallback(
        async () => {
          const j = await authFetch("/v1/residents");
          if (!j || !Array.isArray(j.residents)) return null;
          const map = {};
          j.residents.forEach((r) => {
            if (r.label) map[r.label] = { silence: r.silence_seconds, status: r.status, coherence: r.coherence };
          });
          return map;
        },
        () => S().residentFreshness || {},
      );
    },

    // Stuck detections on their own (one tool call). The Overview card gets its
    // copy inside the coordinated stats() batch; the Agents pane calls this so a
    // stuck row can name its REASON instead of a generic stale-observation tag.
    // Read-only: auto_recover defaults false server-side.
    async stuckAgents() {
      return withFallback(async () => {
        const r = await callTool("detect_stuck_agents", {});
        if (!r || !Array.isArray(r.stuck_agents)) return null;
        return r.stuck_agents.map(mapStuck);
      }, () => (S().stats && S().stats.stuckList) || []);
    },

    // Operator extensions (see dashboard/EXTENSIONS.md). The manifest is
    // served only when the deployment sets UNITARES_DASHBOARD_EXT_DIR, and
    // only to an authenticated caller. Any failure means "no extensions" —
    // a plain fetch, not authFetch, so a 401/404 here never redirects to
    // sign-in or disturbs the core page.
    async extManifest() {
      if (location.protocol === "file:") return null;
      try {
        const headers = {};
        const t = token();
        if (t) headers["Authorization"] = "Bearer " + t;
        const r = await fetch("/dashboard/ext/manifest.json", { credentials: "same-origin", headers });
        if (!r.ok) return null;
        const j = await r.json();
        return j && Array.isArray(j.sections) ? j : null;
      } catch { return null; }
    },

    // Fetch one extension script's source with the same credentials as the
    // data calls, so a bearer-only operator can load it (a <script src> sends
    // cookies only). The caller injects it.
    async extScript(path) {
      const headers = {};
      const t = token();
      if (t) headers["Authorization"] = "Bearer " + t;
      const r = await fetch("/dashboard/ext/" + path, { credentials: "same-origin", headers });
      if (!r.ok) throw new Error("/dashboard/ext/" + path + " -> " + r.status);
      return r.text();
    },

    // The data seam, exported for extensions so an operator's section reads
    // live data the same way core sections do (bearer + session, 401 -> sign-in,
    // failure -> the fallback it supplies). Extensions add their own accessors
    // onto DATA; they never reach into the bundled snapshot.
    seam: { authFetch, callTool, withFallback },

  };

  window.DATA = DATA;

  // Absorb ?operator_token= at load. Sections call operatorToken() lazily,
  // so without this the credential only persists if the Adjudication pane
  // opens while the param is still in the URL — a provisioning URL that
  // lands on any other pane silently does nothing.
  operatorToken();
})();
