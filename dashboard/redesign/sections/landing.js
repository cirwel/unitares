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

  function badge(el, source) {
    el.className = "src-badge " + source;
    el.textContent = source === "live" ? "live" : "snapshot";
  }

  function renderResidents(residents, source) {
    badge($("resSrc"), source);
    $("residents").innerHTML = residents.map((r) => {
      const cls = r.status === "silent" ? "attention" : r.status === "dark" ? "dark" : "";
      const meta = r.coherence == null ? "no EISV" : "coh " + num(r.coherence);
      return `<span class="res ${cls}"><span class="pip"></span>`
        + `<span class="name">${r.name}</span>`
        + `<span class="meta">${meta} · ${fmtSil(r.silence)}</span></span>`;
    }).join("");

    // attention band — derive from real state, don't hardcode
    const flags = [];
    residents.forEach((r) => {
      const thr = r.silenceThreshold || 3600;
      if (r.silence != null && r.silence > thr) flags.push(`<b>${r.name}</b> silent ${fmtSil(r.silence)}`);
      else if (r.status === "dark" || (r.coherence == null && r.status !== "silent")) flags.push(`<b>${r.name}</b> reporting no EISV`);
    });
    const attn = $("attn");
    if (flags.length) {
      attn.hidden = false;
      attn.innerHTML = `<span class="glyph">⚠</span><span>${flags.join(" · ")} — past check-in threshold.</span>`;
    } else { attn.hidden = true; }
  }

  function renderStats(stats, residents, source) {
    const live = residents.filter((r) => r.coherence != null);
    const fleetCoh = live.length ? (live.reduce((a, r) => a + r.coherence, 0) / live.length) : null;
    const cards = [
      { h: "Fleet Coherence", num: num(fleetCoh), sub: `${live.length} of ${residents.length} residents reporting`, cls: "up", rule: true },
      { h: "Agents", num: stats.agentsActive, of: "/ " + stats.agentsTotal, sub: "active / total" },
      { h: "Stuck", num: stats.stuck, sub: stats.stuck ? "needs attention" : "none flagged", cls: stats.stuck ? "down" : "up" },
      { h: "Discoveries", num: stats.discoveries.toLocaleString(), sub: "+" + stats.discoveriesToday + " today" },
      { h: "Dialectic", num: stats.dialectic, sub: stats.dialectic ? "open sessions" : "no open sessions" },
      { h: "System Health", num: stats.systemHealth, sub: "db · ws · reaper" },
      { h: "Calibration", num: num(stats.calibration), sub: "trajectory health", cls: "up" },
      { h: "Anomalies", num: stats.anomalies, sub: stats.anomalies ? "1 active" : "clear", cls: stats.anomalies ? "down" : "up" },
    ];
    const tiers = stats.trustTiers || [];
    const max = Math.max(1, ...tiers.map((t) => t.n));
    const tierBars = tiers.map((t) => `<div class="t ${t.tier}" style="height:${Math.round((t.n / max) * 100)}%"></div>`).join("");

    $("stats").innerHTML = cards.map((s) =>
      `<div class="card ${s.rule ? "accent-rule" : ""}"><h3>${s.h}</h3>`
      + `<div class="num">${s.num}${s.of ? `<span class="of"> ${s.of}</span>` : ""}</div>`
      + `<div class="sub ${s.cls || ""}">${s.sub}</div></div>`
    ).join("")
      + `<div class="card wide"><h3>Trust Tiers</h3><div class="tiers">${tierBars}</div>`
      + `<div class="legend" style="margin-top:.5rem">`
      + `<span><i style="background:var(--eisv-i)"></i>strong</span>`
      + `<span><i style="background:var(--eisv-s)"></i>medium</span>`
      + `<span><i style="background:var(--faint)"></i>weak</span></div></div>`;
  }

  function renderPulse(residents) {
    // last check-in = smallest silence among reporting residents
    const reporting = residents.filter((r) => r.eisv);
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

  async function render() {
    const [health, residents, stats] = await Promise.all([DATA.health(), DATA.residents(), DATA.stats()]);
    if (health.data) {
      const h = health.data;
      $("serverStat").innerHTML = `v<b>${h.version}</b> · up <b>${h.uptime}</b> · db <b>${h.db}</b>`;
    }
    renderResidents(residents.data, residents.source);
    renderStats(stats.data, residents.data, stats.source);
    renderPulse(residents.data);

    const anyLive = [residents, stats, health].some((r) => r.source === "live");
    $("foot").innerHTML = anyLive
      ? "Redesign · served live · design system in <code>tokens.css</code> + <code>kit.css</code>."
      : "Redesign reference · rendering bundled snapshot (open served same-origin for live data) · "
        + "design system in <code>tokens.css</code> + <code>kit.css</code>. Toggle theme to reskin via one token swap.";
  }

  window.Landing = { render };
})();
