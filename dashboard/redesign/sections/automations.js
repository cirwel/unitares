/*
 * Automations section — the automation registry / census map.
 * Reads DATA.automations() (the `unitares-automations` census snapshot).
 * Overview answers "do I need to care right now?"; this page answers
 * "what exists, who runs it, when, where, and what did it last do?".
 * Composes kit primitives; no fetch logic here, no styles here.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  // Local schedulers the operator runs directly; github-actions (the bulk) is
  // folded away by default so the map opens on what's actually theirs to mind.
  const LOCAL = ["launchd", "hermes", "codex", "claude"];

  let MODEL = { items: [], summary: {}, stale: false, ageS: null, warnings: [], attn: new Set(), source: "snapshot" };
  let scope = "local"; // "local" (+attention) | "all"
  let sourceF = "all", kindF = "all", q = "";

  function fmtAge(s) {
    if (s == null) return "age unknown";
    return s < 90 ? s + "s old" : s < 5400 ? Math.round(s / 60) + "m old"
      : s < 172800 ? (s / 3600).toFixed(1) + "h old" : (s / 86400).toFixed(1) + "d old";
  }
  function relTime(iso) {
    if (!iso) return "—";
    const ms = Date.now() - Date.parse(iso);
    if (isNaN(ms)) return esc(iso);
    const h = ms / 3.6e6;
    return h < 1 ? Math.round(ms / 6e4) + "m ago" : h < 48 ? Math.round(h) + "h ago" : Math.round(h / 24) + "d ago";
  }
  function statusClass(s) {
    s = (s || "").toLowerCase();
    if (["active", "completed", "ok", "success", "healthy", "passed"].includes(s)) return "ok";
    if (["failed", "error", "failure"].includes(s)) return "danger";
    return "warn"; // pending / paused / stale / unknown
  }
  function statusTag(s) {
    const k = statusClass(s);
    const color = k === "ok" ? "var(--ok)" : k === "danger" ? "var(--danger)" : "var(--warn)";
    return `<span class="tag" style="color:${color};border-color:color-mix(in srgb, ${color} 35%, var(--line-2))">${esc(s || "—")}</span>`;
  }

  // Verifier-centric scorecard: how is each automation grounded in truth?
  const GATE_META = {
    machine: { color: "var(--ok)", label: "machine-gated" },
    human: { color: "var(--warn)", label: "human-gated" },
    ungated: { color: "var(--danger)", label: "ungated" },
    external: { color: "var(--faint)", label: "external" },
    unclassified: { color: "var(--faint)", label: "unclassified" },
  };
  function gateClass(it) {
    const tag = (it.notes || []).find((n) => typeof n === "string" && n.indexOf("gate:") === 0);
    return tag ? tag.slice(5) : "unclassified";
  }
  function gateBadge(cls) {
    const m = GATE_META[cls] || GATE_META.unclassified;
    return `<span class="tag" style="color:${m.color};border-color:color-mix(in srgb, ${m.color} 40%, var(--line-2))">${m.label}</span>`;
  }
  function gateCell(it) {
    const cls = gateClass(it);
    const verifier = it.escalates_to
      ? `<div style="font-size:var(--text-xs);color:${cls === "ungated" ? "var(--danger)" : "var(--muted)"};margin-top:3px;max-width:240px;white-space:normal" title="${esc(it.escalates_to)}">${esc(it.escalates_to)}</div>`
      : "";
    // owner is the constant principal; only worth showing when it isn't you.
    const owner = it.owner && it.owner !== "Kenny" ? `<div style="font-size:var(--text-xs);color:var(--faint);margin-top:2px">${esc(it.owner)}</div>` : "";
    return `<div>${gateBadge(cls)}</div>${verifier}${owner}`;
  }
  function pathCell(it) {
    const where = it.workdir || it.repo || "", cfg = it.config_path || "";
    const tilde = (p) => p ? p.replace(/^\/Users\/[^/]+/, "~") : "";
    return `<div style="font-family:var(--font-mono);font-size:var(--text-xs);color:var(--muted);max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">`
      + (where ? `<div title="${esc(where)}">${esc(tilde(where))}</div>` : "")
      + (cfg ? `<div title="${esc(cfg)}" style="color:var(--faint)">${esc(tilde(cfg))}</div>` : "")
      + (!where && !cfg ? "—" : "") + `</div>`;
  }
  function listCell(values) {
    const list = (values || []).filter(Boolean);
    if (!list.length) return "—";
    const shown = list.slice(0, 3).map((v) => `<span class="tag" title="${esc(v)}">${esc(v)}</span>`).join("");
    const more = list.length > 3 ? `<span style="font-size:var(--text-xs);color:var(--faint)">+${list.length - 3}</span>` : "";
    return `<div style="display:flex;gap:4px;flex-wrap:wrap;max-width:260px">${shown}${more}</div>`;
  }
  function contractCell(it) {
    return `<div style="display:grid;gap:4px">`
      + `<div><span style="font-size:var(--text-xs);color:var(--faint)">outputs</span> ${listCell(it.expected_outputs)}</div>`
      + `<div><span style="font-size:var(--text-xs);color:var(--faint)">surfaces</span> ${listCell(it.surface_claims)}</div>`
      + `</div>`;
  }
  function priority(it) {
    const p = Number(it.dashboard_priority);
    return Number.isFinite(p) ? p : 999;
  }

  function visible() {
    return MODEL.items.filter((it) => {
      if (sourceF !== "all") { if (it.source !== sourceF) return false; }
      else if (scope === "local" && !(LOCAL.includes(it.source) || MODEL.attn.has(it.id))) return false;
      if (kindF !== "all" && it.kind !== kindF) return false;
      const needle = q.trim().toLowerCase(); // q holds the raw typed text; match case-insensitively
      if (needle) {
        const hay = (it.name + " " + it.source + " " + it.kind + " " + (it.runner || "") + " "
          + (it.owner || "") + " " + (it.escalates_to || "") + " " + (it.description || "") + " "
          + (it.workdir || "") + " " + (it.repo || "") + " " + (it.config_path || "") + " "
          + (it.expected_outputs || []).join(" ") + " " + (it.surface_claims || []).join(" ")).toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    }).sort((a, b) => {
      const aa = MODEL.attn.has(a.id) ? 0 : 1, bb = MODEL.attn.has(b.id) ? 0 : 1;
      if (aa !== bb) return aa - bb;
      const pa = priority(a), pb = priority(b);
      if (pa !== pb) return pa - pb;
      return (a.source + a.name + a.id).localeCompare(b.source + b.name + b.id);
    });
  }

  function warnStrip() {
    const bits = [];
    const att = MODEL.summary.needs_attention || [];
    if (att.length) bits.push({ cls: "attn-band", html: `<span class="glyph">●</span><span><b>${att.length}</b> automation${att.length > 1 ? "s" : ""} flagged for attention.</span>` });
    if (MODEL.stale) bits.push({ cls: "attn-band", html: `<span class="glyph">⚠</span><span>census snapshot is <b>${fmtAge(MODEL.ageS)}</b> — run <code>unitares-automations census --write</code> to refresh.</span>` });
    (MODEL.warnings || []).forEach((w) => bits.push({ cls: "attn-band calm", html: `<span class="glyph">·</span><span>${esc(w)}</span>` }));
    return bits.map((b) => `<div class="${b.cls}" style="margin-bottom:var(--space-2)">${b.html}</div>`).join("");
  }

  function render() {
    const sum = MODEL.summary || {};
    const rows = visible();
    const sel = (id, cur, opts) => `<select id="${id}" class="theme-toggle">${opts.map((o) => `<option value="${o}" ${o === cur ? "selected" : ""}>${o}</option>`).join("")}</select>`;
    // Role-reversal scorecard over the whole fleet: where is verification already
    // machine-grounded, where are you still the gate, what's unverified.
    const gc = { machine: 0, human: 0, ungated: 0, external: 0, unclassified: 0 };
    MODEL.items.forEach((it) => { const c = gateClass(it); gc[c] = (gc[c] || 0) + 1; });
    const chip = (n, color, label) => `<span style="color:${color}"><b>${n}</b> ${label}</span>`;
    const sep = `<span style="color:var(--faint)">·</span>`;
    $("auto-mount").innerHTML =
      warnStrip()
      + `<div style="display:flex;align-items:center;gap:var(--space-4);flex-wrap:wrap;margin-bottom:var(--space-2);font-size:var(--text-xs);color:var(--muted)">
           <span><b style="color:var(--ink)">${sum.total != null ? sum.total : MODEL.items.length}</b> automations</span>
           <span>snapshot ${fmtAge(MODEL.ageS)}</span>
           <span class="src-badge ${MODEL.source}">${MODEL.source}</span>
         </div>`
      + `<div style="display:flex;gap:var(--space-3);flex-wrap:wrap;align-items:center;margin-bottom:var(--space-4);font-size:var(--text-xs)">
           <span style="color:var(--muted)">grounded in truth:</span>
           ${chip(gc.machine, "var(--ok)", "machine-gated")} ${sep} ${chip(gc.human, "var(--warn)", "human-gated")} ${sep} ${chip(gc.ungated, "var(--danger)", "ungated")} ${sep} <span style="color:var(--faint)">${gc.unclassified} unclassified</span>
         </div>`
      + `<div style="display:flex;gap:var(--space-3);flex-wrap:wrap;align-items:center;margin-bottom:var(--space-4)">
           <input id="auto-q" placeholder="search name · path · runner" value="${q.replace(/"/g, "&quot;")}"
             style="flex:1;min-width:180px;padding:var(--space-2) var(--space-3);font-family:var(--font-sans);font-size:var(--text-sm);background:var(--surface);color:var(--ink);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-sm)" />
           ${sel("auto-source", sourceF, ["all"].concat(Object.keys(sum.by_source || {})))}
           ${sel("auto-kind", kindF, ["all"].concat(Object.keys(sum.by_kind || {})))}
           <label style="font-size:var(--text-xs);color:var(--muted);display:flex;gap:6px;align-items:center"><input type="checkbox" id="auto-all" ${scope === "all" ? "checked" : ""}/> show all sources</label>
         </div>`
      + (rows.length ? `<table class="tbl"><thead><tr>
            <th>Automation</th><th>Gate · verifier</th><th>Source</th><th>Status</th><th>Cadence</th><th>Last run</th><th>Contract</th><th>Where</th></tr></thead><tbody>`
        + rows.map((it) => `<tr>
              <td><div style="font-weight:500;color:var(--ink)">${esc(it.name)}</div>
                  ${it.description ? `<div style="font-size:var(--text-xs);color:var(--muted);margin-top:2px;max-width:280px">${esc(it.description)}</div>` : ""}
                  <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:2px"><span class="tag">${esc(it.kind)}</span>${MODEL.attn.has(it.id) ? `<span class="tag warn">attention</span>` : ""}${(() => { const ns = (it.notes || []).filter((n) => !(typeof n === "string" && n.indexOf("gate:") === 0)); return ns.length ? `<span style="font-size:var(--text-xs);color:var(--faint)">${esc(ns.join(" · "))}</span>` : ""; })()}</div></td>
              <td>${gateCell(it)}</td>
              <td><span class="tag">${esc(it.source)}</span><div style="font-size:var(--text-xs);color:var(--muted);margin-top:2px">${esc(it.runner || "")}</div></td>
              <td>${statusTag(it.status)}</td>
              <td style="font-size:var(--text-sm);color:var(--ink-2)">${esc(it.cadence || "—")}${it.next_run ? `<div style="font-size:var(--text-xs);color:var(--muted)">next ${relTime(it.next_run)}</div>` : ""}</td>
              <td style="font-size:var(--text-sm);color:var(--muted)">${relTime(it.last_run)}${it.last_status ? " " + statusTag(it.last_status) : ""}</td>
              <td>${contractCell(it)}</td>
              <td>${pathCell(it)}</td></tr>`).join("")
        + `</tbody></table>`
        : `<p class="empty">No automations match — ${scope === "local" && sourceF === "all" ? "try “show all sources”." : "adjust the filters."}</p>`)
      + `<div style="margin-top:var(--space-3);font-size:var(--text-xs);color:var(--faint)">showing ${rows.length} of ${MODEL.items.length}${scope === "local" && sourceF === "all" ? " · local + attention (toggle “show all sources” for github-actions)" : ""}</div>`;
    wire();
  }

  function wire() {
    const s = $("auto-q"); if (s) { s.oninput = () => { const pos = s.selectionStart; q = s.value; render(); const e = $("auto-q"); if (e) { e.focus(); if (pos != null) e.setSelectionRange(pos, pos); } }; }
    const src = $("auto-source"); if (src) src.onchange = () => { sourceF = src.value; render(); };
    const k = $("auto-kind"); if (k) k.onchange = () => { kindF = k.value; render(); };
    const all = $("auto-all"); if (all) all.onchange = () => { scope = all.checked ? "all" : "local"; render(); };
  }

  async function load() {
    const r = await DATA.automations();
    const d = r.data || {};
    MODEL = {
      items: d.automations || [], summary: d.summary || {}, stale: !!d.stale,
      ageS: d.snapshot_age_seconds,
      warnings: (d.summary && d.summary.warnings) || d.warnings || [],
      attn: new Set((d.summary && d.summary.needs_attention) || []),
      source: r.source,
    };
    render();
  }
  window.Automations = { load };
})();
