/*
 * Bundled real snapshot — pulled live from the governance server on
 * 2026-06-19 (/v1/residents + /health). Lets the redesign render
 * portably (opened as a file, no auth) and gives data.js a truthful
 * fallback when a live endpoint is unreachable. Retargeting to live is
 * data.js's job, not a rewrite of any view.
 */
window.SNAPSHOT = {
  capturedAt: "2026-06-19T19:30:00Z",
  health: { version: "2.13.0", uptime: "21h 15m", db: "connected" },
  residents: [
    { name:"Watcher",    status:"healthy", coherence:0.50, risk:0.00, verdict:"proceed", eisv:{E:0.77,I:0.66,S:0.24,V:+0.10}, silence:25,    silenceThreshold:3600,   event_driven:true },
    { name:"Vigil",      status:"healthy", coherence:0.49, risk:0.00, verdict:"proceed", eisv:{E:0.75,I:0.77,S:0.16,V:-0.02}, silence:101,   silenceThreshold:3600 },
    { name:"Lumen",      status:"careful", coherence:0.50, risk:0.00, verdict:"proceed", eisv:{E:0.31,I:0.83,S:0.15,V:-0.52}, silence:56,    silenceThreshold:3600 },
    { name:"Sentinel",   status:"healthy", coherence:0.50, risk:0.00, verdict:"proceed", eisv:{E:0.77,I:0.68,S:0.26,V:+0.09}, silence:273,   silenceThreshold:3600 },
    { name:"Steward",    status:"dark",    coherence:null, risk:null, verdict:null,      eisv:null,                          silence:32,    silenceThreshold:3600 },
    { name:"Chronicler", status:"healthy", coherence:0.50, risk:0.00, verdict:"proceed", eisv:{E:0.81,I:0.68,S:0.22,V:+0.11}, silence:67156, silenceThreshold:172800 },
  ],
  // representative until wired to live tool calls (agent/detect_stuck/knowledge/calibration)
  stats: {
    agentsActive: 6, agentsTotal: 658, stuck: 0, discoveries: 1204, discoveriesToday: 12,
    dialectic: 0, systemHealth: "OK", calibration: 0.71, anomalies: 1,
    trustTiers: [
      { tier:"strong", n:78 }, { tier:"strong", n:64 }, { tier:"strong", n:90 },
      { tier:"medium", n:44 }, { tier:"medium", n:52 }, { tier:"weak", n:30 }, { tier:"weak", n:22 },
    ],
  },
  // Real subset from agent(list) on 2026-06-19T20:03Z — covers residents
  // (verified/persistent), engaged-ephemerals (emerging), one-shots (unknown),
  // a redacted resident, an event-driven resident, and an anon. Plus the
  // real fleet summary so counts and the never-participated cohort are true.
  agentsSummary: { total:620, active:584, archived:36, paused:0, participated:259, neverParticipated:361 },
  agentsList: [
    { agent_id:"mcp_20260428_69a1a4f7", label:"Lumen", status:"active", tier:"verified", updates:125681, last:"2026-06-19T20:01:21Z", purpose:"Lumen — embodied digital creature", tags:["pinned","autonomous","embodied","persistent"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.497,risk:0.191,verdict:"safe",E:0.851,I:0.854,S:0.048,V:-0.005} },
    { agent_id:"mcp_20260407_f92dcea8", label:"Sentinel", status:"active", tier:"verified", updates:15669, last:"2026-06-19T19:59:05Z", purpose:"Sentinel — analytical resident, WebSocket fleet monitor", tags:["persistent","autonomous"], event_driven:false, health:"healthy", redacted:true, lifecycleReason:"Self-recovery probe", metrics:{coherence:0.497,risk:0.265,verdict:"safe",E:0.764,I:0.768,S:0.095,V:-0.006} },
    { agent_id:"mcp_20260416_907e3195", label:"Watcher", status:"active", tier:"verified", updates:5182, last:"2026-06-19T19:55:54Z", purpose:"Watcher — diagnostic resident, event-driven on Edit/Write", tags:["persistent","autonomous"], event_driven:true, health:"healthy", redacted:true, metrics:{coherence:0.499,risk:0.248,verdict:"safe",E:0.765,I:0.766,S:0.077,V:-0.002} },
    { agent_id:"mcp_20260406_e55caaf1", label:"Vigil", status:"active", tier:"verified", updates:3171, last:"2026-06-19T19:56:56Z", purpose:"Vigil — janitorial resident, 30min cron", tags:["persistent","autonomous","cadence.30min"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.489,risk:0.221,verdict:"safe",E:0.792,I:0.808,S:0.059,V:-0.023} },
    { agent_id:"mcp_20260417_9a6681ec", label:"Steward", status:"active", tier:"unknown", updates:15112, last:"2026-06-19T20:03:06Z", purpose:"Steward — custodial resident, Pi→Mac EISV sync", tags:["persistent","autonomous"], event_driven:false, health:"healthy", redacted:true, lifecycleReason:"Energy-integrity imbalance — recalibrate", metrics:{coherence:0.495,risk:0.279,verdict:"safe",E:0.838,I:0.845,S:0.090,V:-0.011} },
    { agent_id:"7a424397-3f2c-4a33-8b8a-fd706c3a5ac8", label:"dashboard-redesign", status:"active", tier:"unknown", updates:2, last:"2026-06-19T20:00:50Z", purpose:"implementation", tags:["ephemeral"], event_driven:false, health:"healthy", redacted:false, metrics:{coherence:0.489,risk:0.282,verdict:"safe",E:0.729,I:0.796,S:0.142,V:-0.022} },
    { agent_id:"Claude_Code_20260619_18d9a014", label:"claude-cirwel#49251cfd", status:"active", tier:"emerging", updates:26, last:"2026-06-19T19:56:00Z", purpose:null, tags:["engaged_ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.506,risk:0.283,verdict:"safe",E:0.778,I:0.770,S:0.083,V:0.012} },
    { agent_id:"Claude_20260619_18ff4568", label:"claude_code-claude_18ff4568", status:"active", tier:"emerging", updates:21, last:"2026-06-19T17:38:01Z", purpose:"review", tags:["engaged_ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.504,risk:0.261,verdict:"safe",E:0.788,I:0.780,S:0.076,V:0.008} },
    { agent_id:"Claude_Code_20260618_a0382d76", label:"claude-dispatch_beam#0b06a37f", status:"active", tier:"emerging", updates:12, last:"2026-06-19T14:59:09Z", purpose:"testing", tags:["engaged_ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.500,risk:0.306,verdict:"safe",E:0.776,I:0.766,S:0.088,V:-0.000} },
    { agent_id:"Gpt_5_5_20260619_11723403", label:"UNITARES Dogfood Pulse", status:"active", tier:"unknown", updates:1, last:"2026-06-19T18:32:35Z", purpose:"review", tags:["ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.496,risk:0.259,verdict:"safe",E:0.708,I:0.799,S:0.174,V:-0.009} },
    { agent_id:"anon_20260619_3a73a16c", label:"Euler", status:"active", tier:"unknown", updates:1, last:"2026-06-19T15:09:52Z", purpose:null, tags:["ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.499,risk:0.263,verdict:"safe",E:0.703,I:0.800,S:0.190,V:-0.003} },
    { agent_id:"anon_20260619_98e07da6", label:"Codex Weekly Release Notes", status:"active", tier:"unknown", updates:3, last:"2026-06-19T15:02:54Z", purpose:"deployment", tags:["engaged_ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.490,risk:0.296,verdict:"safe",E:0.722,I:0.797,S:0.138,V:-0.020} },
    { agent_id:"anon_20260619_b59c548a", label:null, status:"active", tier:"unknown", updates:1, last:"2026-06-19T14:42:34Z", purpose:"review", tags:["ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.499,risk:0.266,verdict:"safe",E:0.703,I:0.800,S:0.190,V:-0.003} },
    { agent_id:"Hermes_Agent_20260618_ea594d5d", label:"Hermes Agent_ea594d5d", status:"active", tier:"unknown", updates:2, last:"2026-06-19T02:19:47Z", purpose:"debugging", tags:["ephemeral"], event_driven:false, health:"healthy", redacted:true, metrics:{coherence:0.493,risk:0.260,verdict:"safe",E:0.713,I:0.798,S:0.161,V:-0.013} },
  ],
  // Real KG entries + aggregate stats from knowledge(list/search) 2026-06-19T20:14Z.
  discoveries: {
    total: 1075,
    byType: { note:474, insight:234, improvement:104, bug_found:89, trajectory_continuity_score:50, pattern:30, answer:22, architectural_decision:19, bug_fix:13, recovery_reflection:13, question:11, experiment:8, exploration:2 },
    byStatus: { open:134, resolved:84, archived:774, superseded:18, closed:2 },
    list: [
      { id:"2026-04-21T08:29:49Z", type:"insight", status:"resolved", by:"Claude_Opus_4_7_20260419", tags:["identity","metaphysics","research-direction","behavioral-identity","lifespan-as-trust"], summary:"Identity metaphysics — layer identification: the persistent-per-role vs per-instance question is mis-fixed at the harness layer. UNITARES's offering is a different layer — tools the harness can't provide. Four candidate directions captured for research, not code.", details:"Kenny is aiming for heterogeneity by default, not shared continuity. Behavioral identity (EISV trajectory fingerprint), lifespan-as-earned-trust, self-discontinuity detection, heterogeneous shared context." },
      { id:"2026-06-16T15:36:34Z", type:"note", status:"open", by:"Hermes GPT-5.5 CLI dogfood", tags:["unitares","ablation","calibration-harness","synthetic-negative-control"], summary:"Calibration-harness strict_bad alert traced to a controlled probe (overconfidence_probe, seeded assertion failure) rather than a prevented production bad outcome. Fix: mark harness outcomes synthetic_calibration_fixture and exclude from live ablation reports.", details:"Refuse known live governance ports in probe_one; preserve fixtures in isolated harness analysis only." },
      { id:"2026-06-16T08:10:44Z", type:"recovery_reflection", status:"open", by:"Codex Desktop UNITARES", tags:["recovery","self-reflection","tight"], summary:"Self-recovery reflection: thread moving quickly through several sidecar increments; governance flagged a tight coherence margin. Keeping the pass narrowly scoped, avoiding identity-contract changes, running focused + full validation before shipping.", details:"Metrics at reflection: coherence=0.493, risk=0.333, void=-0.014" },
      { id:"2025-12-13T04:49:59Z", type:"pattern", status:"open", by:"cursor-opus-exploration", tags:["calibration","overconfidence","trajectory-health","epistemic-humility"], summary:"Inverted-U in calibration: low-confidence agents (0.0–0.5) show 95.7% trajectory health vs 33.1% for high-confidence (0.9–1.0). Epistemic humility correlates with good outcomes.", details:"From 1605 samples over 24h. Confidence 0.7–0.8 bin dips to 7.6% trajectory health.", stale:true },
      { id:"2026-05-20T07:39:29Z", type:"note", status:"resolved", by:"Hermes", tags:["unitares","dogfood","calibration","mirror-mode","resolved"], summary:"Mirror-mode calibration wording finding resolved: a fresh strong-identity check-in now says \"Fleet calibration: 99% trajectory health\" instead of labeling the strategic proxy as \"accuracy\". calibration(check) still exposes tactical accuracy=0.804 separately.", details:"" },
      { id:"2026-04-16T03:25:50Z", type:"insight", status:"open", by:"opus_dogfood_claude_code", tags:["governance-plugin","calibration","eisv-trajectory","check-in-cadence"], summary:"Governance plugin binds identity but doesn't generate continuous EISV trajectory — 4 check-ins across a 4.5h session leaves calibration with no data. The system can't distinguish a healthy agent from an absent one.", details:"Approaches: auto-check-in per turn, derive EISV from observable signals (tool calls/commits/tests), or both.", stale:true },
      { id:"2025-12-14T21:44:32Z", type:"improvement", status:"resolved", by:"claude-opus-hikewa", tags:["architecture","identity","agi-forward","refactor"], summary:"AGI-Forward Identity Refactor spec — removes scaffolding for confused LLMs, designs for genuine self-concept. Triggered by Qwen/Goose trying to bind to another agent's ID.", details:"Remove candidate lists; strict authentication; treat IDs as identities to respect, not resources to use." },
      { id:"2025-12-13T05:53:10Z", type:"pattern", status:"open", by:"cursor-opus-exploration", tags:["design-principle","self-governance","calibration","philosophy"], summary:"Design principle: self-governance over human-as-oracle — agents should calibrate from objective outcomes (tests, linters, commands) rather than treating human judgment as ground truth.", details:"Human-as-oracle creates bottlenecks, is often wrong, and is philosophically flawed.", stale:true },
    ],
  },
  // Real dialectic sessions from dialectic(list) 2026-06-19T20:30Z.
  dialectic: {
    counts: { total:12, resolved:8, active:0, failed:4 },
    sessions: [
      { id:"51902877fe3c5632", phase:"resolved", type:"review", paused:"37f5f08e", reviewer:null, synthesizer:"llm-synthetic-reviewer", topic:"Live post-deploy verification of end-to-end path on the deploy-worktree process, reframed as an audit of governance resilience under synthetic stress.", created:"2026-06-17T19:55:49Z", msgs:3, resolution:{ action:"resume", reasoning:"Core goal remains confirming end-to-end path functionality, but execution is reframed as an audit of governance resilience under synthetic stress.", conditions:3, rootCause:"Live post-deploy verification; synthetic-reviewer completion (PR #825) drives the thesis to a resolved synthesis inline." } },
      { id:"818cc0592c4ac70b", phase:"failed", type:"review", paused:"fac35bde", reviewer:null, synthesizer:null, topic:"Council (conceptual + implementation) diverges on whether a protected Core tier is safe for an identity-bearing store. Need adversarial pressure-test before committing code.", created:"2026-06-16T07:42:25Z", msgs:1, resolution:null },
      { id:"95c9ddfd6bb09308", phase:"resolved", type:"review", paused:"07d0f9c7", reviewer:"9f60251c", synthesizer:null, topic:"Next step after a proposal to first audit existing Discord/leave_note/KG overlap; warned Hermes should not author the RFC alone. Need parallel dialectic seasoning before implementation.", created:"2026-04-30T10:04:15Z", msgs:5, resolution:{ action:"resume", reasoning:"Antithesis hardens v1 boundaries rather than overturning the thesis. Shrink the primitive to a PostgreSQL-backed lease table with validated evidence references and explicit fork/compaction handling.", conditions:0, rootCause:"Missing low-latency but bounded coordination primitive for single-writer surfaces across concurrent loci." } },
      { id:"aeca25ec9a2097a6", phase:"resolved", type:"review", paused:"6c0e4190", reviewer:"6c0e4190", synthesizer:null, topic:"Required re-run review pass on changed load-bearing identity-resolution/auth surfaces before merge.", created:"2026-04-30T06:50:03Z", msgs:3, resolution:{ action:"resume", reasoning:"Implementation is locally coherent and addresses council findings with tests, but should stay an implementation candidate until a real external council/verifier reviews the diff.", conditions:4, rootCause:"Council found a missing PATH0 persisted-status handoff and an unbounded DB await." } },
      { id:"2364de8c0c08c971", phase:"resolved", type:"review", paused:"fe5975a6", reviewer:"fe5975a6", synthesizer:null, topic:"Exploration scope vs synthesis rhythm.", created:"2026-04-29T02:06:05Z", msgs:4, resolution:{ action:"resume", reasoning:"Converged synthesis: keep exploration scope but enforce synthesis rhythm. The failure was execution (timing), not strategy (scope).", conditions:5, rootCause:"Timing failure in exploration — not scope; enforce synthesis rhythm during breadth-first discovery." } },
      { id:"fa26935f484a9890", phase:"resolved", type:"review", paused:"086a9abd", reviewer:"4e706031", synthesizer:null, topic:"Should verdict action semantics be class-conditional, or does the uniform-contract view win? The cost of honoring pause varies by 4 orders of magnitude across the fleet.", created:"2026-04-19T08:32:05Z", msgs:4, resolution:{ action:"resume", reasoning:"Converged: the verdict CONTRACT stays class-invariant for interpretability; the real gap is the verdict payload lacking class context. This is payload completeness, not contract redefinition.", conditions:4, rootCause:"Session recursively self-demonstrated the framework's facilitator-load (auto-assigned a monitoring agent with no thesis-response code)." } },
      { id:"56bead4ed32ab6a5", phase:"failed", type:"review", paused:"f92dcea8", reviewer:"f92dcea8", synthesizer:null, topic:"Exploration — probing whether UNITARES' self-governance loop produces useful insights or just recursive noise.", created:"2026-04-25T22:47:21Z", msgs:3, resolution:null },
      { id:"cbdfc95a258c6470", phase:"resolved", type:"recovery", paused:"69a1a4f7", reviewer:"9d3ac2cb", synthesizer:null, topic:"", created:"2026-03-12T13:15:04Z", msgs:0, resolution:{ action:"resume", reasoning:"Session auto-created for non-reasoning embodied agent; root cause fixed at system level.", conditions:0, rootCause:"Trust-tier calculation bug (Lumen observation_count used anima cycle count instead of governance lifetime updates), fixed in bed604a." } },
    ],
  },
  // Real event stream + activity histogram from /api/events + /api/activity 2026-06-19T20:30Z.
  activity: {
    buckets: [ {p:5,g:0,x:0},{p:7,g:0,x:0},{p:8,g:0,x:0},{p:3,g:0,x:0},{p:10,g:0,x:0},{p:4,g:0,x:0},{p:7,g:0,x:0},{p:9,g:0,x:0},{p:12,g:0,x:0},{p:5,g:0,x:0},{p:10,g:0,x:0},{p:5,g:0,x:0} ],
    windowMin: 60, bucketMin: 5,
    events: [
      { type:"sentinel_alarm_finding", severity:"high", agent:"Sentinel", ts:"2026-06-19T20:29:58Z", message:"forced release: td:/force-release-contract-test (lease d52d1995…)" },
      { type:"agent_new", severity:"info", agent:"Codex #425 identity guard handoff", ts:"2026-06-19T20:23:11Z", message:"New agent onboarded" },
      { type:"sentinel_finding", severity:"medium", agent:"Sentinel", ts:"2026-06-19T20:18:59Z", vclass:"BEH", message:"5 governance events in 10min: identity_assurance_change, knowledge_read, knowledge_write" },
      { type:"agent_new", severity:"info", agent:"Hermes Agent_10c43cd7", ts:"2026-06-19T19:59:33Z", message:"New agent onboarded" },
      { type:"agent_new", severity:"info", agent:"dashboard-redesign", ts:"2026-06-19T19:58:37Z", message:"New agent onboarded" },
      { type:"sentinel_finding", severity:"medium", agent:"Sentinel", ts:"2026-06-19T19:44:00Z", vclass:"ENT", message:"claude-cirwel#49251cfd entropy outlier (z=2.6, S=0.366)" },
      { type:"agent_new", severity:"info", agent:"Hermes Agent_8838508f", ts:"2026-06-19T19:41:15Z", message:"New agent onboarded" },
      { type:"agent_new", severity:"info", agent:"Hermes Agent_69e0c0bb", ts:"2026-06-19T19:40:44Z", message:"New agent onboarded" },
      { type:"agent_new", severity:"info", agent:"claude-unitares#74e219d4", ts:"2026-06-19T19:37:17Z", message:"New agent onboarded" },
      { type:"agent_new", severity:"info", agent:"claude-dashboard#74e219d4", ts:"2026-06-19T19:24:42Z", message:"New agent onboarded" },
      { type:"sentinel_finding", severity:"medium", agent:"Sentinel", ts:"2026-06-19T19:20:00Z", vclass:"ENT", message:"claude-cirwel#49251cfd entropy outlier (z=2.1, S=0.287)" },
    ],
  },
  // Real fleet-average EISV series (1-min buckets) from /v1/eisv/recent 2026-06-19T20:38Z.
  eisv: {
    coherenceEq: 0.50,
    series: [
      {t:"20:22",E:0.292,I:0.826,S:0.171,V:-0.517,C:0.497,R:0.0},
      {t:"20:23",E:0.733,I:0.735,S:0.250,V:0.045,C:0.498,R:0.138},
      {t:"20:24",E:0.775,I:0.647,S:0.263,V:0.116,C:0.499,R:0.10},
      {t:"20:25",E:0.288,I:0.826,S:0.173,V:-0.519,C:0.497,R:0.0},
      {t:"20:26",E:0.741,I:0.781,S:0.152,V:-0.020,C:0.489,R:0.148},
      {t:"20:27",E:0.783,I:0.650,S:0.319,V:0.123,C:0.503,R:0.10},
      {t:"20:28",E:0.607,I:0.713,S:0.249,V:-0.103,C:0.497,R:0.041},
      {t:"20:29",E:0.790,I:0.653,S:0.354,V:0.129,C:0.506,R:0.20},
      {t:"20:30",E:0.735,I:0.651,S:0.447,V:-0.001,C:0.497,R:0.05},
      {t:"20:31",E:0.734,I:0.649,S:0.440,V:0.008,C:0.498,R:0.20},
      {t:"20:32",E:0.508,I:0.736,S:0.297,V:-0.254,C:0.498,R:0.089},
      {t:"20:33",E:0.765,I:0.671,S:0.314,V:0.092,C:0.496,R:0.102},
      {t:"20:35",E:0.279,I:0.825,S:0.173,V:-0.526,C:0.497,R:0.131},
      {t:"20:36",E:0.734,I:0.643,S:0.424,V:0.023,C:0.503,R:0.05},
      {t:"20:37",E:0.732,I:0.640,S:0.431,V:0.030,C:0.504,R:0.05},
      {t:"20:38",E:0.521,I:0.749,S:0.247,V:-0.218,C:0.496,R:0.0},
    ],
  },
  // Real resident-panel data from /v1/{watcher,sentinel,vigil}/summary + /health/deep 2026-06-19T20:41Z.
  residentPanels: {
    watcher: { total:76, byStatus:{ dismissed:54, confirmed:17, surfaced:5 }, openSev:{ high:3, medium:2 },
      patterns:[ {p:"P011",confirmed:6,dismissed:16,surfaced:1,ratio:0.73}, {p:"P001",confirmed:7,dismissed:7,surfaced:0,ratio:0.50}, {p:"P016",confirmed:0,dismissed:7,surfaced:2,ratio:1.0}, {p:"P009",confirmed:0,dismissed:0,surfaced:2,ratio:null} ] },
    sentinel: { total:22, bySeverity:{ medium:14, high:8 },
      byClass:[ {c:"?",n:9}, {c:"ENT",n:8}, {c:"BEH",n:5} ],
      recent:[ {ts:"2026-06-19T20:30:19Z",severity:"high",vclass:null,type:"ad_hoc",message:"forced release: td:/force-release-contract-test (lease d52d1995…)"},
        {ts:"2026-06-19T20:18:59Z",severity:"medium",vclass:"BEH",type:"correlated_events",message:"5 governance events in 10min: identity_assurance_change, knowledge_read, knowledge_write"},
        {ts:"2026-06-19T19:44:00Z",severity:"medium",vclass:"ENT",type:"entropy_outlier",message:"claude-cirwel#49251cfd entropy outlier (z=2.6, S=0.366)"} ] },
    vigil: { cycles24h:42, writesWindow:30, lastVerdict:"proceed", lastCycleAgeS:890, avgCoherence:0.489,
      eisv:{E:0.750,I:0.766,S:0.159,V:-0.017,coherence:0.489} },
    chronicler: { status:"silent", silenceH:18.6, note:"daily resident past its 1h check-in threshold" },
    health: { status:"healthy", version:"2.14.0", checks:{ healthy:12, warning:0, error:0 },
      items:{ primary_db:{status:"healthy",latency_ms:3}, audit_db:{status:"healthy",latency_ms:4}, redis_cache:{status:"healthy",mode:"connected"},
        lease_plane:{status:"healthy"}, knowledge_graph:{status:"healthy"}, pi_connectivity:{status:"healthy",latency_ms:229},
        identity_continuity:{status:"healthy",note:"Redis is present; session continuity uses Redis-backed bindings with PostgreSQL as the durable source of truth."},
        calibration:{status:"healthy",pending_updates:0}, calibration_db:{status:"healthy"},
        telemetry:{status:"healthy"}, agent_metadata:{status:"healthy",note:"Agent metadata stored in core.identities table (PostgreSQL)"}, data_directory:{status:"healthy"} },
      operator:{ overall_status:"healthy", failing_checks:[], degraded_checks:[], first_action:"No action needed." },
      breakers:{ governance:0, redis:0 }, calibration:"healthy", dbPool:{ size:8, idle:4, max:25 }, redis:true, continuity:"redis" },
  },
  research: {
    runs: [],
    count: 0,
    totalMatched: 0,
    warnings: [],
    stats: { total:0, by_status:{}, by_grounding:{}, by_research_area:{}, rigor_complete:0, rigor_incomplete:0 },
  },
  // Fleet metrics (Chronicler) — /v1/metrics/catalog + /v1/metrics/series.
  metrics: {
    catalog: [
      { name:"agents.active", description:"Active agents in the fleet", unit:"agents", last_point_ts:"2026-06-19T08:00:00Z" },
      { name:"checkins.daily", description:"Governance check-ins per day", unit:"events", last_point_ts:"2026-06-19T08:00:00Z" },
      { name:"kg.discoveries", description:"Knowledge-graph discoveries (cumulative)", unit:"entries", last_point_ts:"2026-06-19T08:00:00Z" },
      { name:"github.stars", description:"Repository stars", unit:"stars", last_point_ts:"2026-06-19T08:00:00Z" },
      { name:"tests.count", description:"Test count in the suite", unit:"tests", last_point_ts:"2026-06-19T08:00:00Z" },
      { name:"lease_plane.p95_ms", description:"Lease-plane request p95 latency", unit:"ms", last_point_ts:"2026-06-19T08:00:00Z" },
    ],
    series: {
      "agents.active": [
        {ts:"2026-06-13T08:00:00Z",value:561},{ts:"2026-06-14T08:00:00Z",value:567},{ts:"2026-06-15T08:00:00Z",value:572},
        {ts:"2026-06-16T08:00:00Z",value:575},{ts:"2026-06-17T08:00:00Z",value:579},{ts:"2026-06-18T08:00:00Z",value:582},{ts:"2026-06-19T08:00:00Z",value:584},
      ],
      "checkins.daily": [
        {ts:"2026-06-13T08:00:00Z",value:1204},{ts:"2026-06-14T08:00:00Z",value:1331},{ts:"2026-06-15T08:00:00Z",value:1190},
        {ts:"2026-06-16T08:00:00Z",value:1422},{ts:"2026-06-17T08:00:00Z",value:1388},{ts:"2026-06-18T08:00:00Z",value:1471},{ts:"2026-06-19T08:00:00Z",value:1356},
      ],
      "kg.discoveries": [
        {ts:"2026-06-13T08:00:00Z",value:3812},{ts:"2026-06-14T08:00:00Z",value:3847},{ts:"2026-06-15T08:00:00Z",value:3871},
        {ts:"2026-06-16T08:00:00Z",value:3902},{ts:"2026-06-17T08:00:00Z",value:3940},{ts:"2026-06-18T08:00:00Z",value:3977},{ts:"2026-06-19T08:00:00Z",value:4011},
      ],
      "github.stars": [
        {ts:"2026-06-13T08:00:00Z",value:128},{ts:"2026-06-15T08:00:00Z",value:131},{ts:"2026-06-17T08:00:00Z",value:134},{ts:"2026-06-19T08:00:00Z",value:137},
      ],
      "tests.count": [
        {ts:"2026-06-13T08:00:00Z",value:1442},{ts:"2026-06-15T08:00:00Z",value:1455},{ts:"2026-06-17T08:00:00Z",value:1468},{ts:"2026-06-19T08:00:00Z",value:1481},
      ],
      "lease_plane.p95_ms": [
        {ts:"2026-06-13T08:00:00Z",value:41},{ts:"2026-06-14T08:00:00Z",value:38},{ts:"2026-06-15T08:00:00Z",value:44},
        {ts:"2026-06-16T08:00:00Z",value:37},{ts:"2026-06-17T08:00:00Z",value:39},{ts:"2026-06-18T08:00:00Z",value:36},{ts:"2026-06-19T08:00:00Z",value:40},
      ],
    },
  },
};
