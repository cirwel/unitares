/*
 * Discoveries section — knowledge-graph entries.
 * Rebuilt from old discoveries.js: lifecycle bar + type legend (clickable
 * filter) + search/type/time filters + cards (type badge, agent, date,
 * summary w/ highlight, tags, expandable details, staleness). Composes kit;
 * reads DATA.discoveries() (live-or-snapshot).
 *
 * In live mode, submitted search queries (Enter / Search button) and discrete
 * type/time filter changes hit the server's KG search endpoint via
 * DATA.discoveries(query, opts) so the full corpus is searchable (up to the
 * 30-row page limit) without sending partial keystroke fragments into
 * recall_misses.jsonl. In snapshot/offline mode, filtering stays client-side
 * over the bundled snapshot list.
 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);

  const TYPE_ORDER = ["insight", "improvement", "bug_found", "pattern", "question", "answer", "analysis", "note", "exploration"];
  const STATUS_COLOR = { open: "var(--ok)", resolved: "var(--eisv-c)", archived: "var(--faint)", superseded: "var(--warn)", closed: "var(--muted)" };
  const TIME_WINDOW_MS = { "24h": 864e5, "7d": 6048e5, "30d": 2592e6 };
  const PAGE_LIMIT = 30;
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const label = (t) => t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  function relTime(iso) {
    const ms = Date.now() - Date.parse(iso);
    if (isNaN(ms)) return "";
    const h = ms / 3.6e6, d = h / 24;
    if (h < 1) return Math.max(1, Math.round(ms / 6e4)) + "m ago";
    if (h < 24) return Math.round(h) + "h ago";
    if (d < 60) return Math.round(d) + "d ago";
    return Math.round(d / 30) + "mo ago";
  }
  function dateLabel(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return String(iso || "").slice(0, 10);
    return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function createdAfterIso(tf) {
    const ms = TIME_WINDOW_MS[tf];
    return ms ? new Date(Date.now() - ms).toISOString() : null;
  }
  function highlight(raw, q) {
    const text = String(raw == null ? "" : raw);
    if (!q) return esc(text);
    const terms = Array.from(new Set(q.trim().split(/\s+/).filter(Boolean)))
      .sort((a, b) => b.length - a.length)
      .slice(0, 8);
    if (!terms.length) return esc(text);
    try {
      const pat = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
      const re = new RegExp(pat, "ig");
      let out = "", last = 0, m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) out += esc(text.slice(last, m.index));
        out += "<mark>" + esc(m[0]) + "</mark>";
        last = m.index + m[0].length;
        if (m[0].length === 0) re.lastIndex += 1;
      }
      if (last < text.length) out += esc(text.slice(last));
      return out;
    } catch { return esc(text); }
  }

  let MODEL = {
    list: [], byType: {}, byStatus: {}, total: 0,
    source: "snapshot", searchMode: null, lowConfidence: false, moreAvailable: false,
  };
  let baseSource = "snapshot";
  let defaultList = [];
  let defaultListStale = false;
  let queryText = "";
  let serverQuery = "";
  let serverType = "all";
  let serverTime = "all";
  let typeFilter = "all", timeFilter = "all";
  let searching = false;
  let reqSeq = 0;
  // "N new" since the last load. An explore pane stays manual, so a knowledge_write
  // doesn't refetch (that would disrupt search/filter/scroll) — it just bumps this
  // badge as a refresh nudge. Reset on load().
  let newCount = 0;

  function card(d, q) {
    const tags = (d.tags || []).slice(0, 5).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ");
    const details = (d.details || "").trim();
    const stale = d.stale ? `<span class="tag warn" title="aged / still open — verify before acting">stale</span>` : "";
    return `<div class="panel" style="padding:var(--space-5)">
      <div style="display:flex;gap:var(--space-3);align-items:center;flex-wrap:wrap;margin-bottom:var(--space-2)">
        <span class="tag" style="border-color:var(--line-2)">${label(d.type)}</span>
        <span class="tag" style="color:${STATUS_COLOR[d.status] || "var(--muted)"}">${esc(d.status)}</span>
        ${stale}
        <span class="spring"></span>
        <span class="fresh">${esc(d.by || "—")} · ${dateLabel(d.id)} <span style="color:var(--faint)">(${relTime(d.id)})</span></span>
      </div>
      <div style="color:var(--ink);font-size:var(--text-base);line-height:var(--leading-body);margin-bottom:${details || tags ? "var(--space-3)" : "0"}">${highlight(d.summary, q)}</div>
      ${tags ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:${details ? "var(--space-3)" : "0"}">${tags}</div>` : ""}
      ${details || d.session || d.version || d.agentId ? `<details><summary style="cursor:pointer;color:var(--muted);font-size:var(--text-sm)">${details ? "details" : "provenance"}</summary>
        ${details ? `<div style="margin-top:var(--space-2);color:var(--ink-2);font-size:var(--text-sm);white-space:pre-wrap">${esc(details)}</div>` : ""}
        <div style="margin-top:var(--space-3);display:flex;gap:var(--space-4);flex-wrap:wrap;font-size:var(--text-xs);color:var(--faint)">
          ${d.agentId ? `<span title="${esc(d.agentId)}">agent ${esc((d.agentId || "").slice(0, 8))}</span>` : ""}
          ${d.session ? `<span title="${esc(d.session)}">session ${esc((d.session || "").slice(0, 8))}</span>` : ""}
          ${d.version ? `<span>v${esc(d.version)}</span>` : ""}
          ${d.created ? `<span title="${esc(d.created)}">created ${esc((d.created || "").slice(0, 10))}</span>` : ""}
        </div></details>` : ""}
    </div>`;
  }

  function ensureShell() {
    const mount = $("#dsc-mount");
    if (!mount || $("#dsc-list", mount)) return;
    mount.innerHTML =
      `<div id="dsc-stats"></div>
       <div id="dsc-types" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:var(--space-4)"></div>
       <div style="display:flex;gap:var(--space-3);flex-wrap:wrap;margin-bottom:var(--space-4)">
         <input id="dsc-search" type="search" placeholder="search summary · details · tags (Enter to search)" value="${esc(queryText)}"
           style="flex:1;min-width:200px;padding:var(--space-2) var(--space-3);font-family:var(--font-sans);font-size:var(--text-sm);background:var(--surface);color:var(--ink);border:var(--hairline) solid var(--line-2);border-radius:var(--radius-sm)" />
         <button id="dsc-search-btn" class="theme-toggle" type="button" title="Search the knowledge graph">Search</button>
         <select id="dsc-time" class="theme-toggle">${[["all", "all time"], ["24h", "24h"], ["7d", "7 days"], ["30d", "30 days"]].map(([v, t]) => `<option value="${v}" ${v === timeFilter ? "selected" : ""}>${t}</option>`).join("")}</select>
         <button id="dsc-new" class="theme-toggle" title="New knowledge written since you loaded — click to refresh" style="display:${newCount > 0 ? "inline-flex" : "none"};border-color:var(--accent);color:var(--accent)">${newCount} new · refresh</button>
       </div>
       <div id="dsc-status" style="display:none;margin-bottom:var(--space-3);font-size:var(--text-xs);color:var(--muted)"></div>
       <div id="dsc-list" style="display:flex;flex-direction:column;gap:var(--space-3)"></div>`;

    const s = $("#dsc-search", mount);
    if (s) {
      s.oninput = () => {
        queryText = s.value;
        onSearchInput();
      };
      s.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submitSearch();
        }
      };
    }
    const btn = $("#dsc-search-btn", mount);
    if (btn) btn.onclick = submitSearch;
    const t = $("#dsc-time", mount);
    if (t) {
      t.onchange = () => {
        timeFilter = t.value;
        onDiscreteFilterChange();
      };
    }
    const n = $("#dsc-new", mount);
    if (n) n.onclick = load;
  }

  function renderStats() {
    const statsEl = $("#dsc-stats");
    const typesEl = $("#dsc-types");
    if (!statsEl || !typesEl) return;

    // lifecycle bar
    const bs = MODEL.byStatus || {};
    const statusSum = Object.values(bs).reduce((a, n) => a + n, 0);
    // Entries the status breakdown doesn't account for (statuses outside the
    // lifecycle set, or rows past the stats read). Left implicit, the legend
    // sums short of the "All N" chip and reads as a counting bug.
    const other = typeof MODEL.total === "number" && MODEL.total > statusSum ? MODEL.total - statusSum : 0;
    const totalS = statusSum + other || 1;
    const barOrder = ["open", "resolved", "archived", "superseded", "closed"];
    const otherTitle = "not in the status breakdown (other statuses, or beyond this stats read)";
    const bar = barOrder.filter((k) => bs[k]).map((k) =>
      `<div title="${k}: ${bs[k]}" style="width:${(bs[k] / totalS) * 100}%;background:${STATUS_COLOR[k]};height:100%"></div>`).join("")
      + (other ? `<div title="other: ${other} — ${otherTitle}" style="width:${(other / totalS) * 100}%;background:var(--line-2);height:100%"></div>` : "");
    const barLegend = barOrder.filter((k) => bs[k]).map((k) =>
      `<span><i style="background:${STATUS_COLOR[k]}"></i>${k} ${bs[k]}</span>`).join("")
      + (other ? `<span title="${otherTitle}"><i style="background:var(--line-2)"></i>other ${other}</span>` : "");

    statsEl.innerHTML = Object.keys(bs).length
      ? `<div style="margin-bottom:var(--space-4)">
           <div class="track" style="height:10px;display:flex;gap:1px">${bar}</div>
           <div class="legend" style="margin-top:var(--space-2)">${barLegend} <span class="src-badge ${MODEL.source}" style="margin-left:auto">${MODEL.source}</span></div>
         </div>`
      : `<div class="legend" style="margin-bottom:var(--space-3)"><span class="src-badge ${MODEL.source}" style="margin-left:auto">${MODEL.source}</span></div>`;

    // type legend
    const bt = MODEL.byType || {};
    const types = Object.keys(bt).sort((a, b) => (TYPE_ORDER.indexOf(a) + 1 || 99) - (TYPE_ORDER.indexOf(b) + 1 || 99));
    typesEl.innerHTML = ["all"].concat(types).map((t) => {
      const n = t === "all" ? MODEL.total : bt[t];
      const on = t === typeFilter;
      return `<button class="theme-toggle dsc-type" data-type="${t}" style="${on ? "border-color:var(--accent);color:var(--accent)" : ""}">${label(t)} <span style="color:var(--faint)">${n ?? ""}</span></button>`;
    }).join("");
    typesEl.querySelectorAll(".dsc-type").forEach((b) => {
      b.onclick = () => {
        typeFilter = b.dataset.type;
        renderStats();
        onDiscreteFilterChange();
      };
    });
  }

  function renderList() {
    const listEl = $("#dsc-list");
    const statusEl = $("#dsc-status");
    if (!listEl) return;

    const q = queryText.trim();
    const qLower = q.toLowerCase();
    const highlightQuery = MODEL.source === "live" ? serverQuery : q;
    const isServerQuery = MODEL.source === "live" && serverQuery !== "";
    const isServerFiltered = MODEL.source === "live" && (serverQuery !== "" || serverType !== "all" || serverTime !== "all");
    let rows = MODEL.list.slice();

    // In snapshot/offline mode, filter locally. In live mode, server search
    // results already ran hybrid/FTS/semantic matching on the corpus, so keep
    // their ranking and never drop semantic or non-contiguous matches via
    // .includes(q).
    if (MODEL.source !== "live" || typeFilter !== serverType) {
      if (typeFilter !== "all") rows = rows.filter((d) => (d.type || "note") === typeFilter);
    }
    if (MODEL.source !== "live" || timeFilter !== serverTime) {
      if (timeFilter !== "all") {
        const cut = Date.now() - TIME_WINDOW_MS[timeFilter];
        rows = rows.filter((d) => Date.parse(d.id) >= cut);
      }
    }
    if (MODEL.source !== "live" && qLower) {
      rows = rows.filter((d) => ((d.summary || "") + " " + (d.details || "") + " " + (d.tags || []).join(" ")).toLowerCase().includes(qLower));
    }
    if (!isServerQuery) {
      rows.sort((a, b) => Date.parse(b.id || 0) - Date.parse(a.id || 0));
    }

    if (statusEl) {
      const capped = MODEL.moreAvailable || rows.length >= PAGE_LIMIT;
      if (searching) {
        statusEl.textContent = "Searching knowledge graph…";
        statusEl.style.display = "block";
      } else if (baseSource === "live" && q !== serverQuery) {
        statusEl.textContent = q
          ? "Press Enter or click Search to query the knowledge graph."
          : "";
        statusEl.style.display = q ? "block" : "none";
      } else if (isServerFiltered) {
        const mode = isServerQuery && MODEL.searchMode ? MODEL.searchMode + " · " : "";
        const countLabel = isServerQuery
          ? (capped ? `showing top ${rows.length} (capped at ${PAGE_LIMIT})` : `${rows.length} result${rows.length === 1 ? "" : "s"}`)
          : (capped ? `showing latest ${rows.length} (capped at ${PAGE_LIMIT})` : `showing ${rows.length} matching`);
        const conf = isServerQuery && MODEL.lowConfidence ? " · exploratory (semantic-only)" : "";
        statusEl.textContent = `${mode}${countLabel}${conf}`;
        statusEl.style.display = "block";
      } else {
        statusEl.textContent = "";
        statusEl.style.display = "none";
      }
    }

    listEl.innerHTML = rows.length
      ? rows.map((d) => card(d, highlightQuery)).join("")
      : MODEL.source === "unavailable"
        ? `<p class="empty">Server not answering — discoveries unavailable.</p>`
        : `<p class="empty">No matches. Clear filters or change search.</p>`;
  }

  function render() {
    ensureShell();
    renderStats();
    renderList();
  }

  function restoreOrRefetchDefault() {
    if (!defaultListStale) {
      reqSeq += 1;
      searching = false;
      serverQuery = "";
      serverType = "all";
      serverTime = "all";
      MODEL = {
        ...MODEL,
        list: defaultList.slice(),
        source: "live",
        searchMode: null,
        lowConfidence: false,
        moreAvailable: defaultList.length >= PAGE_LIMIT,
      };
      renderStats();
      renderList();
      return;
    }
    runServerSearch("");
  }

  function onSearchInput() {
    if (baseSource !== "live") {
      renderList();
      return;
    }
    const q = queryText.trim();
    if (!q && (serverQuery !== "" || searching || MODEL.source === "unavailable")) {
      // Clearing the search box immediately exits search/error mode without
      // sending partial prefixes to the server.
      if (typeFilter === "all" && timeFilter === "all") {
        restoreOrRefetchDefault();
      } else {
        runServerSearch("");
      }
      return;
    }
    renderList();
  }

  function submitSearch() {
    if (baseSource !== "live") {
      renderList();
      return;
    }
    const q = queryText.trim();
    if (!q && typeFilter === "all" && timeFilter === "all") {
      restoreOrRefetchDefault();
      return;
    }
    runServerSearch(q);
  }

  function onDiscreteFilterChange() {
    if (baseSource !== "live") {
      renderList();
      return;
    }
    // Use the committed serverQuery rather than uncommitted keystrokes in
    // queryText so chip clicks / time changes never send partial prefixes.
    const q = serverQuery;
    if (!q && typeFilter === "all" && timeFilter === "all") {
      restoreOrRefetchDefault();
      return;
    }
    runServerSearch(q);
  }

  async function runServerSearch(submittedQuery) {
    const seq = ++reqSeq;
    const q = typeof submittedQuery === "string" ? submittedQuery.trim() : serverQuery;
    const tf = typeFilter;
    const tm = timeFilter;
    const hasFilter = !!(q || tf !== "all" || tm !== "all");
    searching = true;
    renderList();

    const r = await DATA.discoveries(q || undefined, {
      discoveryType: tf !== "all" ? tf : undefined,
      createdAfter: createdAfterIso(tm) || undefined,
      skipStats: true,
    });
    if (seq !== reqSeq) return;
    searching = false;
    if (r.source === "unavailable") {
      MODEL = {
        ...MODEL, list: [], source: "unavailable",
        searchMode: null, lowConfidence: false, moreAvailable: false,
      };
      renderStats();
      renderList();
      return;
    }
    const list = r.data.list || [];
    MODEL = {
      ...MODEL,
      list,
      source: r.source,
      searchMode: r.data.searchMode || null,
      lowConfidence: !!r.data.lowConfidence,
      moreAvailable: !!r.data.moreAvailable,
    };
    if (!hasFilter && r.source === "live") {
      defaultList = list.slice();
      defaultListStale = false;
    }
    serverQuery = q;
    serverType = tf;
    serverTime = tm;
    renderStats();
    renderList();
  }

  async function load() {
    const seq = ++reqSeq;
    searching = false;

    const q = serverQuery;
    const tf = typeFilter;
    const tm = timeFilter;
    const hasFilter = !!(q || tf !== "all" || tm !== "all");
    const r = hasFilter
      ? await DATA.discoveries(q || undefined, {
        discoveryType: tf !== "all" ? tf : undefined,
        createdAfter: createdAfterIso(tm) || undefined,
      })
      : await DATA.discoveries();
    if (seq !== reqSeq) return;

    const list = r.data.list || [];
    MODEL = {
      list,
      byType: r.data.byType || {},
      byStatus: r.data.byStatus || {},
      total: r.data.total || list.length,
      source: r.source,
      searchMode: r.data.searchMode || null,
      lowConfidence: !!r.data.lowConfidence,
      moreAvailable: !!r.data.moreAvailable,
    };
    baseSource = r.source;
    if (!hasFilter && r.source === "live") {
      defaultList = list.slice();
      defaultListStale = false;
    } else if (hasFilter) {
      // Filtered refresh did not reload the unfiltered recent list; mark it
      // stale so clearing filters later refetches instead of showing old rows.
      defaultListStale = true;
    }
    serverQuery = r.source === "live" ? q : "";
    serverType = r.source === "live" ? tf : "all";
    serverTime = r.source === "live" ? tm : "all";
    newCount = 0; // freshly loaded — nothing new yet
    render();
    const b = $("#dsc-new");
    if (b) b.style.display = "none";
  }

  // A knowledge_write landed — bump the badge in place (no re-render, so a search
  // in progress keeps its focus). The badge element is always present (hidden at
  // 0), so this just flips it visible and updates the count.
  function notifyNew() {
    newCount += 1;
    defaultListStale = true;
    const b = $("#dsc-new");
    if (b) { b.textContent = newCount + " new · refresh"; b.style.display = "inline-flex"; }
  }
  window.Discoveries = { load, notifyNew };
})();
