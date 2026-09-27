/*
 * SYNTHETIC offline fixture. Every name, id, number and sentence below is
 * invented; nothing is a capture of any deployment.
 *
 * data.js reads this only where there is no server to ask: the page opened
 * from a file, or a design preview passed ?snapshot=1. On a served page a
 * failed read renders "unavailable" instead (see SNAPSHOT_FALLBACK in data.js).
 * Until 2026-09-27 this file was a real capture of the maintainer's fleet —
 * resident names, EISV vectors, verdicts, knowledge-graph text — shipped in
 * every checkout and image. The shapes are unchanged, so every view renders
 * offline exactly as it does live.
 *
 * Values come from a seeded generator (so the file is deterministic) and
 * timestamps are relative to page load (so "3m ago" reads as recent in a
 * preview opened today).
 */
(function () {
  "use strict";
  let seed = 20260927;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  const r3 = (x) => Math.round(x * 1000) / 1000;
  const between = (lo, hi) => r3(lo + rnd() * (hi - lo));
  const NOW = Date.now();
  const at = (secondsAgo) => new Date(NOW - secondsAgo * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
  const hhmm = (secondsAgo) => at(secondsAgo).slice(11, 16);
  const eisv = () => ({ E: between(0.55, 0.8), I: between(0.6, 0.85), S: between(0.1, 0.3), V: between(-0.1, 0.1) });

  // ── residents: a small, generic roster so the strip renders offline ────────
  const residents = [
    { id: "res-alpha-0001", name: "resident-alpha", status: "healthy", coherence: 0.50, risk: 0.04, verdict: "proceed", eisv: eisv(), silence: 40, silenceThreshold: 3600, event_driven: true },
    { id: "res-beta-0002", name: "resident-beta", status: "healthy", coherence: 0.49, risk: 0.07, verdict: "proceed", eisv: eisv(), silence: 420, silenceThreshold: 3600 },
    { id: "res-gamma-0003", name: "resident-gamma", status: "healthy", coherence: 0.50, risk: 0.02, verdict: "proceed", eisv: eisv(), silence: 30000, silenceThreshold: 172800 },
  ];

  // ── agents that checked in recently (Overview feed, Risk picker) ───────────
  const ACTIONS = ["proceed", "proceed", "proceed", "guide", "proceed", "guide", "proceed", "pause"];
  const recentAgents = {
    coverageStart: (NOW - 6 * 3600 * 1000) / 1000,
    agents: [
      ["a1f0c2d4", "coding-agent-1", 45], ["b7e2a913", "resident-alpha", 120], ["c3d9e0f1", "review-bot", 600],
      ["d4a8b2c6", "coding-agent-2", 1500], ["e5f1c7a0", "resident-beta", 2400], ["f6b3d8e2", "research-agent", 5400],
      ["a7c4e9f3", "docs-agent", 9000], ["b8d5f0a4", "coding-agent-3", 14000],
    ].map(([id, name, ago], i) => ({
      id: id + "-0000-4000-8000-000000000000", name, ts: at(ago), eisv: eisv(),
      coherence: between(0.47, 0.51), risk: between(0.03, 0.35), action: ACTIONS[i], checkins: 2 + Math.floor(rnd() * 40),
    })),
  };

  // Daily trajectory for one agent's drill-down (Agents and Risk panes).
  const agentHistory = {};
  agentHistory[recentAgents.agents[0].id] = Array.from({ length: 14 }, (_, i) => {
    const t = at((14 - i) * 86400).slice(0, 10);
    const risk = r3(0.08 + i * 0.01);
    const action = i === 11 ? "pause" : i > 6 ? "guide" : "approve";
    return { t, E: r3(0.7 - i * 0.02), I: 0.8, S: r3(0.12 + i * 0.004), V: r3(-0.1 - i * 0.02), coherence: 0.5, risk, action, verdict: risk > 0.18 ? "caution" : "safe" };
  });

  const TIERS = ["verified", "established", "emerging", "emerging", "provisional", "unknown"];
  const PURPOSES = ["implement feature", "code review", "docs update", "debugging", "research", "refactor"];
  const agentsList = recentAgents.agents.map((a, i) => ({
    agent_id: a.id, label: a.name, status: "active", tier: TIERS[i % TIERS.length],
    updates: a.checkins * 7, last: a.ts, purpose: PURPOSES[i % PURPOSES.length],
    tags: i < 2 ? ["persistent"] : ["ephemeral"], event_driven: i === 1, health: "healthy", redacted: false,
    metrics: { coherence: a.coherence, risk: a.risk, verdict: a.risk > 0.3 ? "caution" : "safe", E: a.eisv.E, I: a.eisv.I, S: a.eisv.S, V: a.eisv.V },
  }));

  const stuckList = [
    { id: recentAgents.agents[7].id, name: recentAgents.agents[7].name, reason: "cadence_silence", soft: true,
      details: "Active cadence ~10 min over 12 updates, then silent 230 min. Possibly abandoned mid-work — verify. Soft signal; not auto-recovered." },
  ];

  // ── knowledge graph ─────────────────────────────────────────────────────────
  const DISC = [
    ["insight", "resolved", "Retry storms come from two clients sharing one session id; give each process its own.", "Observed as bursts of identical tool calls within a second."],
    ["bug_found", "open", "Search returns archived entries first when the query is a single short word.", "Ranking weights recency below the archived flag; reproduce with any three-letter query."],
    ["note", "open", "The nightly export finishes in about four minutes on the example dataset.", "Timing only; no action needed."],
    ["pattern", "resolved", "Agents that check in every few minutes stay in the high basin; long gaps drift toward boundary.", "Descriptive, from the example fleet."],
    ["improvement", "open", "Show the reviewer's model name on each dialectic session card.", "Operators asked which backend produced a verdict."],
    ["answer", "resolved", "Yes: a paused agent can resume itself when its risk is below the recovery threshold.", "See self_recovery."],
  ];
  const discoveries = {
    total: 128,
    byType: { note: 52, insight: 30, bug_found: 14, improvement: 12, pattern: 9, answer: 6, question: 5 },
    byStatus: { open: 41, resolved: 57, archived: 26, superseded: 4 },
    list: DISC.map(([type, status, summary, details], i) => ({
      id: at((i + 1) * 43000), type, status, by: recentAgents.agents[i % recentAgents.agents.length].name,
      tags: [type, "example"], summary, details,
    })),
  };

  // ── dialectic ───────────────────────────────────────────────────────────────
  const TOPICS = [
    "Agent paused after a burst of failed tool calls; asks to resume with a smaller batch size.",
    "Review of a schema change before it lands.",
    "Agent disputes a guide verdict on a long-running refactor.",
    "Recovery after a stuck detection during a test run.",
    "Review of a proposed retry policy.",
  ];
  const sessions = TOPICS.map((topic, i) => {
    const failed = i === 2;
    return {
      id: (0x5a1e0000 + i).toString(16) + "c0ffee00", phase: failed ? "failed" : "resolved", type: i === 3 ? "recovery" : "review",
      paused: recentAgents.agents[i].id.slice(0, 8), reviewer: "review-bot", synthesizer: "review-bot", topic,
      created: at((i + 1) * 20000), msgs: failed ? 2 : 3,
      resolution: failed ? null : { action: "resume", reasoning: "Conditions agreed; resume with the narrower scope.", conditions: 2, rootCause: "Scope too wide for one step." },
    };
  });
  const dialectic = { counts: { total: 5, resolved: 4, active: 0, failed: 1 }, sessions };

  // ── activity ────────────────────────────────────────────────────────────────
  const buckets = Array.from({ length: 12 }, (_, i) => ({ p: 3 + Math.floor(rnd() * 8), g: i % 4 === 1 ? 1 : 0, x: i === 9 ? 1 : 0 }));
  const EVENTS = [
    ["agent_new", "info", null, "New agent registered"],
    ["sentinel_finding", "medium", "BEH", "Agent check-in cadence changed sharply"],
    ["agent_new", "info", null, "New agent registered"],
    ["sentinel_finding", "medium", "ENT", "Risk rose across three consecutive check-ins"],
    ["agent_new", "info", null, "New agent registered"],
  ];
  const activity = {
    operational: { available: false, source: "snapshot", windowHours: 24, summary: {}, processes: [] },
    buckets, windowMin: 60, bucketMin: 5,
    events: EVENTS.map(([type, severity, vclass, message], i) => ({
      type, severity, vclass, agent: recentAgents.agents[i].name, ts: at((i + 1) * 300), message,
    })),
  };

  // ── EISV chart: a smooth minute series, one measurement lane ─────────────────
  const series = Array.from({ length: 16 }, (_, i) => {
    const w = Math.sin(i / 3);
    return { t: hhmm((16 - i) * 60), E: r3(0.68 + 0.05 * w), I: r3(0.74 - 0.03 * w), S: r3(0.2 + 0.03 * w), V: r3(-0.02 + 0.04 * w), C: r3(0.495 + 0.004 * w), R: r3(0.1 + 0.05 * Math.abs(w)) };
  });
  const eisvChart = {
    coherenceEq: 0.5,
    sourceLanes: [{ source: "behavioral", events: 16, E: 0.68, I: 0.74, S: 0.2, V: -0.02, confidence: 0.9,
      missingObservations: 0, missingInputs: [], enforcementRequested: 0, enforcementApplied: 0, latest: at(60) }],
    series,
  };

  // ── Risk trend: trailing-7-day series, one point per day ───────────────────
  const days = 30;
  const riskTrend = { windowDays: days, risk: [], pause: [], guide: [] };
  for (let i = days - 1; i >= 0; i--) {
    const ts = at(i * 86400).slice(0, 10) + "T00:00:00Z";
    const k = days - i;
    riskTrend.risk.push({ ts, value: r3(0.04 + 0.015 * Math.sin(k / 5)) });
    riskTrend.pause.push({ ts, value: Math.max(0, Math.round(3 + 3 * Math.sin(k / 4))) });
    riskTrend.guide.push({ ts, value: Math.round(900 + 300 * Math.sin(k / 6)) });
  }

  const checkinActivity = (() => {
    const t = buckets.reduce((acc, b) => ({ proceed: acc.proceed + b.p, guide: acc.guide + b.g, pause: acc.pause + b.x }), { proceed: 0, guide: 0, pause: 0 });
    return Object.assign(t, { total: t.proceed + t.guide + t.pause, windowMin: 60, coverageStart: (NOW - 6 * 3600 * 1000) / 1000 });
  })();

  window.SNAPSHOT = {
    synthetic: true,
    capturedAt: at(0),
    health: { version: "0.0.0-example", uptime: "3h 12m", db: "connected" },
    residents,
    residentFreshness: Object.fromEntries(residents.map((r) => [r.name, { silence: r.silence, status: r.status, coherence: r.coherence }])),
    recentAgents,
    checkinActivity,
    agentHistory,
    stats: {
      agentsActive: 8, agentsLive: 5, agentsPresenceUnknown: 3, agentsPresenceUnavailable: 0, agentsTotal: 40,
      discoveries: discoveries.total, discoveriesToday: null,
      dialectic: 0, dialecticRecent: 5, dialecticFailed: 1,
      systemHealth: "OK", systemHealthDetail: "11 ok · 0 warn", degraded: 0,
      stuckList,
    },
    agentsSummary: { total: 40, active: 36, archived: 4, paused: 0, observed: 20, unobserved: 20, live: 5, presenceUnknown: 3, presenceUnavailable: 0 },
    agentsList,
    discoveries,
    dialectic,
    activity,
    eisv: eisvChart,
    riskTrend,
  };
})();
