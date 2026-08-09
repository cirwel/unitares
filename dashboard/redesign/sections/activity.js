/*
 * Activity section — host evidence + governance updates + event stream.
 * Built from old timeline.js oracle: a proceed/guide/pause activity
 * histogram over the window, plus a filterable event timeline (icon by
 * type, severity/verdict colour, violation-class badge, agent, time,
 * message). Composes kit; reads DATA.activity() (live-or-snapshot).
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // type → icon (mirrors timeline.js glyph vocabulary)
  const ICON = {
    checkin: "●", verdict: "■", lifecycle: "⚑", identity: "◆", knowledge: "✎",
    circuit_breaker: "⚡", agent_new: "+", sentinel_finding: "○", sentinel_alarm_finding: "⚡", event: "○",
  };
  const SEV_COLOR = { critical: "var(--danger)", high: "var(--danger)", medium: "var(--warn)", moderate: "var(--warn)", warning: "var(--warn)", low: "var(--muted)", info: "var(--eisv-c)" };
  const importantSev = (s) => ["critical", "high", "medium", "moderate", "warning"].includes(s);

  function relTime(iso) {
    const ms = Date.now() - Date.parse(iso); if (isNaN(ms)) return "";
    const m = ms / 6e4, h = m / 60, d = h / 24;
    if (m < 60) return Math.max(1, Math.round(m)) + "m ago";
    if (h < 24) return Math.round(h) + "h ago";
    return Math.round(d) + "d ago";
  }
  const clock = (iso) => { const t = Date.parse(iso); return isNaN(t) ? "" : new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); };

  let MODEL = { events: [], buckets: [], operational: null, windowMin: 60, bucketMin: 5, source: "snapshot" };
  let filter = "all";

  const nfmt = (n) => typeof n === "number" ? n.toLocaleString() : "—";
  const shortId = (s) => String(s || "").slice(0, 8) || "—";

  function restorationDetails(p) {
    const capsule = p.restoration_capsule;
    if (!capsule) return "";
    const context = capsule.reflection && capsule.reflection.context || {};
    const continuity = capsule.continuity || {};
    const hostObservation = capsule.host_observation || capsule.operational || {};
    const task = context.task_label || context.comparison_key || "no agent-authored task label";
    const outcome = context.task_outcome ? ` · ${esc(context.task_outcome)}` : "";
    const missing = Array.isArray(continuity.missing) && continuity.missing.length
      ? continuity.missing.map((x) => String(x).replace(/_/g, " ")).join(", ")
      : "none";
    const eventRef = hostObservation.event_id ? shortId(hostObservation.event_id) : "unavailable";
    return `<details style="padding:0 0 var(--space-3) var(--space-2)">
      <summary class="fresh" style="cursor:pointer;user-select:none">Restoration capsule · ${esc(String(continuity.restore_basis || "evidence only").replace(/_/g, " "))}</summary>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:var(--space-3);padding:var(--space-3);margin-top:var(--space-2);background:var(--surface-2);border:var(--hairline) solid var(--line);border-radius:var(--radius-sm)">
        <div><div class="fresh">Agent-authored context</div><div>${esc(task)}${outcome}</div></div>
        <div><div class="fresh">Evidence relation</div><div>${esc(String(continuity.relationship || "unknown").replace(/_/g, " "))}</div></div>
        <div><div class="fresh">Evidence reference</div><div class="mono">audit ${esc(eventRef)}</div></div>
        <div><div class="fresh">Missing evidence</div><div>${esc(missing)}</div></div>
      </div>
    </details>`;
  }

  function processRow(p) {
    const name = p.agent_label || shortId(p.agent_id);
    const liveSource = MODEL.operational && MODEL.operational.source === "live";
    const toolRecent = liveSource && p.tool_activity_recent === true;
    const heartbeatRecent = liveSource && p.host_heartbeat_recent;
    const evidenceAt = p.last_tool_activity_at || p.last_host_observation_at || p.last_operational_at;
    const evidenceState = toolRecent
      ? "tool activity observed"
      : heartbeatRecent
        ? "hook parent observed"
        : "host evidence recorded";
    const evidenceColor = toolRecent ? "var(--eisv-c)" : "var(--muted)";
    const reportAt = p.last_agent_report_at || p.last_reflection_at;
    const report = reportAt
      ? `<span title="${esc(reportAt)}">agent check-in ${relTime(reportAt)}</span>`
      : `<span style="color:var(--faint)">no agent-authored check-in</span>`;
    const interpretationCount = p.substrate_interpretation_count || 0;
    const interpretation = p.last_interpretation_at
      ? `<div class="fresh" title="Automatic substrate interpretation — not agent-authored">${nfmt(interpretationCount || 1)} automatic turn ${interpretationCount === 1 ? "summary" : "summaries"} · latest ${relTime(p.last_interpretation_at)}</div>`
      : "";
    const initialization = p.bootstrap_count
      ? `<div class="fresh" title="Synthetic initialization — not a real check-in">${nfmt(p.bootstrap_count)} initialization ${p.bootstrap_count === 1 ? "row" : "rows"}</div>`
      : "";
    let relation;
    if (!reportAt) relation = `<span class="tag">${esc(String(p.state_update_profile || "no agent check-in").replace(/_/g, " "))}</span>`;
    else if (p.tool_activity_after_agent_report || p.operational_after_reflection) relation = `<span class="tag" style="color:var(--warn);border-color:color-mix(in srgb,var(--warn) 35%,var(--line-2))">tools since check-in</span>`;
    else if (p.host_observation_after_agent_report) relation = `<span class="tag">host evidence since check-in</span>`;
    else relation = `<span class="tag">agent check-in current</span>`;
    const mode = p.execution_mode || "unknown";
    const modeSource = p.execution_mode_source || "unspecified";
    const model = p.model ? ` · ${esc(p.model)}` : "";
    return `<div style="border-bottom:var(--hairline) solid var(--line)">
    <div style="display:flex;gap:var(--space-4);align-items:center;flex-wrap:wrap;padding:var(--space-3) 0">
      <div style="min-width:190px;flex:1.4">
        <div style="font-weight:600;color:var(--ink)" title="${esc(p.agent_id)}">${esc(name)}</div>
        <div class="fresh">${esc(p.host_family || "unknown")} · ${esc(mode)} · ${esc(modeSource)}${model}</div>
        <div class="fresh">slot ${esc(shortId(p.slot_hash))}</div>
      </div>
      <div style="min-width:150px;flex:1">
        <div><span class="dot-pip" style="display:inline-block;background:${evidenceColor};margin-right:6px"></span>${evidenceState} · <span title="${esc(evidenceAt)}">${relTime(evidenceAt)}</span></div>
        <div class="fresh">${esc((p.latest_kind || "observation").replace(/_/g, " "))}${heartbeatRecent ? " · hook-parent scope" : ""}</div>
      </div>
      <div style="min-width:155px;flex:1">
        <div>${report}</div>${interpretation}${initialization}
      </div>
      <div style="min-width:130px;flex:.8" class="mono">
        <div>${nfmt(p.tool_count)} tool receipts</div><div class="fresh">+${nfmt(p.tools_in_window)} in window</div>
      </div>
      <div style="min-width:145px;flex:none">${relation}</div>
    </div>${restorationDetails(p)}</div>`;
  }

  function continuity() {
    const op = MODEL.operational;
    if (!op || !op.available) {
      return `<div class="attn-band calm"><span class="glyph">○</span><span>Host-observation stream unavailable. Governance state activity below remains independently sourced.</span></div>`;
    }
    const s = op.summary || {};
    const stat = (label, value, sub) => `<div class="card"><h3>${label}</h3><div class="num">${nfmt(value)}</div><div class="sub">${sub}</div></div>`;
    const rows = (op.processes || []).slice(0, 30);
    return `<div style="margin-bottom:var(--space-6)">
      <div class="panel" style="padding:var(--space-5);margin-bottom:var(--space-4)">
        <div style="display:flex;align-items:baseline;gap:var(--space-3);margin-bottom:var(--space-3)">
          <span class="eyebrow" style="margin:0">Host evidence and check-ins</span>
          <span class="fresh">separate evidence clocks · last ${nfmt(op.windowHours)}h · never agent runtime or EISV</span>
          <span class="spring"></span><span class="src-badge ${esc(op.source)}">${esc(op.source)}</span>
        </div>
        <p style="font-size:var(--text-sm);color:var(--ink-2)">Codex usually produces zero or one agent-authored <span class="mono">sync_state</span> check-in during a turn. Stop can add one automatic, non-agent-authored turn summary, while onboarding can add synthetic initialization. Completed-tool receipts are activity evidence. A heartbeat says only that the hook parent PID was observed; that PID may be shared across chats and never marks an agent as running.</p>
      </div>
      <div class="grid" style="margin-bottom:var(--space-4)">
        ${stat("Observed slots", s.observed_slots == null ? s.processes : s.observed_slots, `${nfmt(s.agents)} identities`)}
        ${stat("Recent tool activity", s.recent_tool_activity_slots == null ? s.recent_processes : s.recent_tool_activity_slots, op.source === "live" ? "completed-tool receipts within 1 hour" : "at snapshot capture")}
        ${stat("Host heartbeats", s.recent_host_heartbeat_slots, "hook-parent evidence; not agent runtime")}
        ${stat("No agent check-in", s.slots_without_agent_report, "agent_report count is zero")}
      </div>
      <div class="panel" style="padding:var(--space-2) var(--space-5)">
        ${rows.length ? rows.map(processRow).join("") : `<p class="empty" style="padding:var(--space-4) 0">No identity-bound host observations in this window.</p>`}
      </div>
    </div>`;
  }

  function histogram() {
    const b = MODEL.buckets;
    if (!b.length) return "";
    const max = Math.max(1, ...b.map((x) => x.p + x.g + x.x));
    const bars = b.map((x) => {
      const seg = (n, color) => n ? `<div style="height:${(n / max) * 100}%;background:${color}"></div>` : "";
      return `<div style="flex:1;display:flex;flex-direction:column-reverse;height:100%;gap:1px" title="${x.p} proceed · ${x.g} guide · ${x.x} pause">
        ${seg(x.p, "var(--ok)")}${seg(x.g, "var(--warn)")}${seg(x.x, "var(--danger)")}</div>`;
    }).join("");
    const total = b.reduce((a, x) => a + x.p + x.g + x.x, 0);
    return `<div class="panel" style="padding:var(--space-5);margin-bottom:var(--space-5)">
      <div style="display:flex;align-items:baseline;gap:var(--space-3);margin-bottom:var(--space-3)">
        <span class="eyebrow" style="margin:0">Governance state updates</span>
        <span class="fresh">state-writing updates · provenance varies · host observations excluded · last ${MODEL.windowMin}m · ${MODEL.bucketMin}m buckets · ${total} updates</span>
        <span class="spring"></span>
        <span class="legend" style="font-size:var(--text-xs)"><span><i style="background:var(--ok)"></i>proceed</span><span><i style="background:var(--warn)"></i>guide</span><span><i style="background:var(--danger)"></i>pause</span></span>
      </div>
      <div style="display:flex;gap:3px;align-items:flex-end;height:90px">${bars}</div>
    </div>`;
  }

  function row(e) {
    const color = SEV_COLOR[e.severity] || "var(--muted)";
    const icon = ICON[e.type] || ICON.event;
    const vclass = e.vclass ? `<span class="tag" title="violation class ${esc(e.vclass)}">${esc(e.vclass)}</span>` : "";
    return `<div style="display:flex;gap:var(--space-3);align-items:baseline;padding:var(--space-2) 0;border-bottom:var(--hairline) solid var(--line)">
      <span style="color:${color};width:14px;flex:none;text-align:center">${icon}</span>
      <span class="fresh" style="width:64px;flex:none" title="${esc(e.ts)}">${clock(e.ts)}</span>
      <span style="font-weight:500;color:var(--ink);width:170px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.agent)}">${esc(e.agent || "—")}</span>
      ${vclass}
      <span style="color:var(--ink-2);flex:1;min-width:0">${esc(e.message || e.type)}</span>
      <span class="fresh" style="flex:none">${relTime(e.ts)}</span>
    </div>`;
  }

  function render() {
    let rows = MODEL.events.slice();
    if (filter === "important") rows = rows.filter((e) => importantSev(e.severity));
    else if (filter !== "all") rows = rows.filter((e) => e.type === filter);
    rows.sort((a, b) => Date.parse(b.ts || 0) - Date.parse(a.ts || 0));

    const types = Array.from(new Set(MODEL.events.map((e) => e.type)));
    const chips = [["all", "all"], ["important", "important"]].concat(types.map((t) => [t, t.replace(/_/g, " ")]))
      .map(([v, t]) => `<button class="theme-toggle act-f" data-f="${v}" style="${v === filter ? "border-color:var(--accent);color:var(--accent)" : ""}">${esc(t)}</button>`).join("");

    $("#act-mount").innerHTML =
      continuity() + histogram() +
      `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:var(--space-3)">
         ${chips}<span class="spring"></span><span class="src-badge ${MODEL.source}">${MODEL.source}</span></div>
       <div class="panel" style="padding:var(--space-4) var(--space-5)">
         ${rows.length ? rows.slice(0, 50).map(row).join("") : `<p class="empty">No events in this view.</p>`}
       </div>`;
    document.querySelectorAll(".act-f").forEach((b) => b.onclick = () => { filter = b.dataset.f; render(); });
  }

  async function load() {
    const r = await DATA.activity();
    MODEL = { events: r.data.events || [], buckets: r.data.buckets || [], operational: r.data.operational || null, windowMin: r.data.windowMin || 60, bucketMin: r.data.bucketMin || 5, source: r.source };
    render();
  }
  window.Activity = { load };
})();
