/*
 * Dialectic section — peer-review / recovery sessions.
 * Rebuilt from old dialectic.js: counts (resolved/active/failed), phase
 * filter, session cards (phase badge, type, agents, topic, msgs, time) and
 * expandable resolution (action, reasoning, conditions, root cause).
 * Composes kit; reads DATA.dialectic() (live-or-snapshot).
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const PHASE = {
    resolved: { color: "var(--ok)", label: "Resolved" },
    failed: { color: "var(--warn)", label: "Failed" },
    escalated: { color: "var(--warn)", label: "Escalated" },
    quorum_voting: { color: "var(--eisv-e)", label: "Quorum" },
    thesis: { color: "var(--eisv-c)", label: "Thesis" },
    antithesis: { color: "var(--eisv-e)", label: "Antithesis" },
    synthesis: { color: "var(--eisv-s)", label: "Synthesis" },
    awaiting_thesis: { color: "var(--muted)", label: "Awaiting thesis" },
  };
  function relTime(iso) {
    const ms = Date.now() - Date.parse(iso); if (isNaN(ms)) return "";
    const h = ms / 3.6e6, d = h / 24;
    if (h < 24) return Math.round(h) + "h ago";
    if (d < 60) return Math.round(d) + "d ago";
    return Math.round(d / 30) + "mo ago";
  }

  let MODEL = { sessions: [], counts: {}, source: "snapshot" };
  let phaseFilter = "all";

  function agentPill(role, id, cls) {
    if (!id) return "";
    return `<span class="tag" style="${cls || ""}" title="${role}">${role}: ${esc(id)}</span>`;
  }

  function card(s) {
    const p = PHASE[s.phase] || { color: "var(--muted)", label: s.phase || "—" };
    const res = s.resolution;
    const pills = [
      agentPill("requestor", s.paused),
      agentPill("reviewer", s.reviewer),
      s.synthesizer ? agentPill("synth", s.synthesizer, "color:var(--eisv-e)") : "",
    ].filter(Boolean).join(" ");
    return `<div class="panel" style="padding:var(--space-5)">
      <div style="display:flex;gap:var(--space-3);align-items:center;flex-wrap:wrap;margin-bottom:var(--space-2)">
        <span class="tag" style="color:${p.color};border-color:color-mix(in srgb, ${p.color} 40%, var(--line-2))">${p.label}</span>
        <span class="tag">${esc(s.type)}</span>
        <span class="fresh" title="${esc(s.id)}">${esc((s.id || "").slice(0, 12))}…</span>
        <span class="spring"></span>
        <span class="fresh">${s.msgs} msg${s.msgs === 1 ? "" : "s"} · ${relTime(s.created)}</span>
      </div>
      ${s.topic ? `<div style="color:var(--ink);font-size:var(--text-base);line-height:var(--leading-body);margin-bottom:var(--space-3)">${esc(s.topic)}</div>`
        : `<div style="color:var(--muted);font-style:italic;font-size:var(--text-sm);margin-bottom:var(--space-3)">(no topic recorded)</div>`}
      <div style="display:flex;gap:6px;flex-wrap:wrap;${res ? "margin-bottom:var(--space-3)" : ""}">${pills}</div>
      ${res ? `<details><summary style="cursor:pointer;color:var(--muted);font-size:var(--text-sm)">resolution · ${esc(res.action || "—")}${res.conditions ? ` · ${res.conditions} condition${res.conditions === 1 ? "" : "s"}` : ""}</summary>
        <div style="margin-top:var(--space-2);font-size:var(--text-sm);color:var(--ink-2);line-height:var(--leading-body)">
          ${res.reasoning ? `<div style="margin-bottom:var(--space-2)">${esc(res.reasoning)}</div>` : ""}
          ${res.rootCause ? `<div style="color:var(--muted)"><b style="color:var(--ink-2)">root cause:</b> ${esc(res.rootCause)}</div>` : ""}
        </div></details>` : ""}
    </div>`;
  }

  function render() {
    const c = MODEL.counts || {};
    let rows = MODEL.sessions.slice();
    if (phaseFilter === "active") rows = rows.filter((s) => !["resolved", "failed"].includes(s.phase));
    else if (phaseFilter !== "all") rows = rows.filter((s) => s.phase === phaseFilter);
    rows.sort((a, b) => Date.parse(b.created || 0) - Date.parse(a.created || 0));

    const chips = [["all", "all " + (c.total ?? "")], ["active", "active " + (c.active ?? 0)], ["resolved", "resolved " + (c.resolved ?? 0)], ["failed", "failed " + (c.failed ?? 0)]]
      .map(([v, t]) => `<button class="theme-toggle dlc-f" data-f="${v}" style="${v === phaseFilter ? "border-color:var(--accent);color:var(--accent)" : ""}">${t}</button>`).join("");

    $("#dlc-mount").innerHTML =
      `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:var(--space-4)">
         ${chips}<span class="spring"></span><span class="src-badge ${MODEL.source}">${MODEL.source}</span></div>
       ${rows.length ? `<div style="display:flex;flex-direction:column;gap:var(--space-3)">${rows.map(card).join("")}</div>`
         : `<div class="empty">🔄 No dialectic sessions in this view. Sessions open when an agent's circuit-breaker trips or review is requested.</div>`}`;
    document.querySelectorAll(".dlc-f").forEach((b) => b.onclick = () => { phaseFilter = b.dataset.f; render(); });
  }

  async function load() {
    const r = await DATA.dialectic();
    MODEL = { sessions: r.data.sessions || [], counts: r.data.counts || {}, source: r.source };
    render();
  }
  window.Dialectic = { load };
})();
