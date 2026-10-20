/*
 * Knowledge flow panel (inside Discoveries) — who built on whose findings.
 * One row per builder → source agent pair from /v1/knowledge/flow: the builder
 * wrote a discovery that links (RELATED_TO) to one the source wrote. Reads
 * DATA.knowledgeFlow() (live-or-snapshot). The aggregate walks the graph, so it
 * loads with the Discoveries tab and refreshes on its own ↻, not on a tick.
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const name = (label, id) => label || (id || "").slice(0, 8) || "—";

  function row(f, max) {
    const w = max ? Math.max(2, (f.links / max) * 100) : 0;
    return `<tr>
      <td title="${esc(f.builder)}">${esc(name(f.builderLabel, f.builder))}</td>
      <td style="color:var(--faint)">←</td>
      <td title="${esc(f.source)}">${esc(name(f.sourceLabel, f.source))}</td>
      <td style="width:40%"><div class="track" style="height:6px"><div style="width:${w}%;height:100%;background:var(--accent)"></div></div></td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${esc(f.links)}</td>
    </tr>`;
  }

  async function load() {
    const mount = $("#kflow-mount");
    if (!mount) return;
    const r = await DATA.knowledgeFlow();
    const flows = (r.data && r.data.flows) || [];
    const max = flows.reduce((m, f) => Math.max(m, f.links || 0), 0);
    // "unavailable" covers a server that did not answer and one that answered
    // with an error (a failed graph probe is a 503), so it never reads as "no graph".
    const empty = r.source === "unavailable" ? "Knowledge flow unavailable — the server did not answer or could not read its graph."
      : r.data && r.data.graphAvailable === false ? "No graph backend on this server, so there is no flow to read."
      : "No finding links across agents yet.";
    mount.innerHTML = `<div class="panel" style="padding:var(--space-5);margin-bottom:var(--space-4)">
      <div style="display:flex;gap:var(--space-3);align-items:center;margin-bottom:var(--space-3)">
        <strong>Knowledge flow</strong>
        <span class="fresh">builder ← source · finding links between agents</span>
        <span class="spring"></span>
        <span class="src-badge ${esc(r.source)}">${esc(r.source)}</span>
        <button id="kflow-reload" class="theme-toggle" title="Reload knowledge flow">↻</button>
      </div>
      ${flows.length
        ? `<table style="width:100%;border-collapse:collapse;font-size:var(--text-sm)"><tbody>${flows.map((f) => row(f, max)).join("")}</tbody></table>`
        : `<p class="empty">${empty}</p>`}
    </div>`;
    const b = $("#kflow-reload");
    if (b) b.onclick = load;
  }

  window.KnowledgeFlow = { load };
})();
