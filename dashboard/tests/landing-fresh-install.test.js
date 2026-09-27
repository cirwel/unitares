import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The Overview as a new operator first sees it: a fresh Docker install, no
// residents, no automations, and a few agents from a host plugin that checks
// in but never holds a binding/lease. Observed on 2026-09-26 against a fresh
// stack after three Hermes sessions: "0 of 0 residents awaiting first
// check-in", "Agents 0 / 3", "Calibration miscalibrated" while the server said
// "unassessed", and a red "stale" Automations card. None of those describe the
// install. The Calibration and Automations cards have since left the Overview
// (2026-09-26); the cards that remain say what the server actually reported.

const landingSource = readFileSync(
  new URL("../redesign/sections/landing.js", import.meta.url),
  "utf8",
);

const RESIDENT = {
  name: "Sentinel", status: "healthy", coherence: 0.5, risk: 0.1,
  verdict: "proceed", silence: 10, silenceThreshold: 3600,
  eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
};

// The live shape of a fresh stack after three plugin sessions.
const FRESH_STATS = {
  agentsActive: 3, agentsLive: 0, agentsPresenceUnknown: 3,
  agentsPresenceUnavailable: 0, agentsTotal: 3,
  degraded: 0,
};

async function render({ residents = [], stats = {} } = {}) {
  const dom = new JSDOM(`
    <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
    <div id="stats"></div><div id="serverStat"></div>
    <div id="pulseWho"></div><div id="pulseFresh"></div>
    <div id="riskVal"></div><div id="riskFill"></div>
    <div id="pulseVerdict"><span></span><span></span></div>
    <div id="eisv"></div><div id="foot"></div>
  `, { runScripts: "outside-only", url: "https://governance.test/" });
  dom.window.DATA = {
    residentLiveness: (r) => (r.coherence == null ? "alive-no-eisv" : "reporting"),
    health: async () => ({ source: "live", data: { version: "t", uptime: "1h", db: "ok" } }),
    residents: async () => ({ source: "live", data: residents }),
    stats: async () => ({ source: "live", data: Object.assign({}, FRESH_STATS, stats) }),
  };
  dom.window.eval(landingSource);
  await dom.window.Landing.render();
  const doc = dom.window.document;
  const card = (title) => {
    const el = [...doc.querySelectorAll(".card")]
      .find((c) => c.querySelector("h3")?.textContent === title);
    return {
      // .num wraps the "/ total" span; read the headline without it.
      num: el.querySelector(".num").textContent
        .replace(el.querySelector(".of")?.textContent ?? "", "").trim(),
      of: el.querySelector(".of")?.textContent.trim() ?? "",
      sub: el.querySelector(".sub").textContent.trim(),
      cls: el.querySelector(".sub").className,
    };
  };
  return { doc, card };
}

describe("landing on a fresh install", () => {
  it("does not announce residents when none are configured", async () => {
    const { doc } = await render();
    const attn = doc.getElementById("attn");
    expect(attn.hidden).toBe(true);
    expect(attn.textContent).not.toContain("0 of 0");
  });

  it("still announces residents awaiting a first check-in when there are some", async () => {
    const quiet = { ...RESIDENT, coherence: null, eisv: null };
    const { doc } = await render({ residents: [quiet] });
    const attn = doc.getElementById("attn");
    expect(attn.hidden).toBe(false);
    expect(attn.textContent).toContain("awaiting first check-in");
  });

  it("headlines registry-active agents when no presence is knowable, not zero", async () => {
    const { card } = await render();
    const c = card("Agents");
    expect(c.num).toBe("3");
    expect(c.of).toBe("/ 3");
    expect(c.sub).toBe("registry active / total · 30d window · 3 presence unknown");
  });

  it("keeps the live count when presence is known to be zero", async () => {
    // Nothing unknown: zero live is a real answer, not a blind spot.
    const { card } = await render({ stats: { agentsLive: 0, agentsPresenceUnknown: 0 } });
    const c = card("Agents");
    expect(c.num).toBe("0");
    expect(c.sub).toBe("live binding/lease · 30d window");
  });

  it("keeps the live count as soon as one agent has a live signal", async () => {
    const { card } = await render({ stats: { agentsLive: 1, agentsPresenceUnknown: 2 } });
    const c = card("Agents");
    expect(c.num).toBe("1");
    expect(c.sub).toBe("live binding/lease · 30d window · 2 presence unknown");
  });

});

// System Health's detail line is built in data.js from /health/deep's
// status_breakdown. A fresh stack is "moderate" because one check is degraded
// (no embedding model loaded); the line used to count only ok/warn/err and so
// read "10 ok · 0 warn" under a "moderate" headline.
describe("system health detail", () => {
  const dataSource = readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8");

  async function healthCard(breakdown, status) {
    const dom = new JSDOM(`
      <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
      <div id="stats"></div><div id="serverStat"></div>
      <div id="pulseWho"></div><div id="pulseFresh"></div>
      <div id="riskVal"></div><div id="riskFill"></div>
      <div id="pulseVerdict"><span></span><span></span></div>
      <div id="eisv"></div><div id="foot"></div>
    `, { runScripts: "outside-only", url: "https://gov.example/" });
    dom.window.fetch = async (url) => {
      const u = String(url);
      const body = u.includes("/health") ? { status, version: "t", uptime: "1h", status_breakdown: breakdown, checks: {} }
        : u.includes("/v1/residents") ? { residents: [] }
        : u.includes("/api/automations") ? { summary: { total: 0, by_kind: {}, needs_attention: [] }, ungated: 0, unclassified: 0, stale: true }
        : { success: true };
      return { ok: true, status: 200, json: async () => body };
    };
    dom.window.eval(dataSource);
    dom.window.eval(landingSource);
    await dom.window.Landing.render();
    const el = [...dom.window.document.querySelectorAll(".card")]
      .find((c) => c.querySelector("h3")?.textContent === "System Health");
    return { num: el.querySelector(".num").textContent.trim(), sub: el.querySelector(".sub").textContent.trim() };
  }

  it("names the degraded check behind a moderate status", async () => {
    const c = await healthCard({ healthy: 10, warning: 0, degraded: 1, unavailable: 0, error: 0 }, "moderate");
    expect(c.num).toBe("moderate");
    expect(c.sub).toBe("10 ok · 0 warn · 1 degraded");
  });

  it("keeps the healthy line unchanged", async () => {
    const c = await healthCard({ healthy: 11, warning: 0, degraded: 0, unavailable: 0, error: 0 }, "healthy");
    expect(c.num).toBe("OK");
    expect(c.sub).toBe("11 ok · 0 warn");
  });

  it("names unavailable and errored checks too", async () => {
    const c = await healthCard({ healthy: 9, warning: 0, degraded: 0, unavailable: 1, error: 1 }, "critical");
    expect(c.sub).toBe("9 ok · 0 warn · 1 unavailable · 1 err");
  });
});

// The bundled snapshot is a capture of one deployment's fleet. On a page served
// by a UNITARES server, a failed live read must not put that fleet on screen as
// this server's (observed 2026-09-26: a fresh install showed the bundled
// residents after one /v1/residents failed during a restart).
describe("snapshot fallback on a served page", () => {
  const dataSource = readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8");
  const BUNDLED = { residents: [{ name: "BundledResident", status: "healthy", coherence: 0.5, risk: 0.1, verdict: "proceed", silence: 10, silenceThreshold: 3600, eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 } }],
                    health: { version: "0.0.0-bundled", uptime: "21h", db: "connected" } };

  function boot(url, { failResidents = true, failHealth = true } = {}) {
    const dom = new JSDOM(`
      <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
      <div id="stats"></div><div id="serverStat"></div>
      <div id="pulseWho"></div><div id="pulseFresh"></div>
      <div id="riskVal"></div><div id="riskFill"></div>
      <div id="pulseVerdict"><span></span><span></span></div>
      <div id="eisv"></div><div id="foot"></div>
    `, { runScripts: "outside-only", url });
    const state = { failResidents, failHealth, residentCalls: 0, healthCalls: 0 };
    dom.window.fetch = async (u) => {
      const s = String(u);
      if (s.endsWith("/health")) {
        state.healthCalls += 1;
        if (state.failHealth) throw new Error("ERR_EMPTY_RESPONSE");
      }
      if (s.includes("/v1/residents")) {
        state.residentCalls += 1;
        if (state.failResidents) throw new Error("ERR_EMPTY_RESPONSE");
      }
      const body = s.endsWith("/health") ? { version: "9.9.9", uptime: { formatted: "2m" }, database: { status: "connected" } }
        : s.includes("/health/deep") ? { status: "healthy", status_breakdown: {}, checks: {} }
        : s.includes("/v1/residents") ? { residents: [] }
        : s.includes("/api/automations") ? { summary: { total: 0, by_kind: {}, needs_attention: [] }, ungated: 0, unclassified: 0, stale: false, snapshot_age_seconds: 60 }
        : { success: true };
      return { ok: true, status: 200, json: async () => body };
    };
    dom.window.SNAPSHOT = BUNDLED;
    dom.window.eval(dataSource);
    dom.window.eval(landingSource);
    return { dom, state };
  }

  it("does not show the bundled fleet when this server's reads fail", async () => {
    const { dom } = boot("https://gov.example/dashboard");
    await dom.window.Landing.render();
    const D = dom.window.document;
    expect(D.getElementById("residents").textContent).not.toContain("BundledResident");
    expect(D.getElementById("serverStat").textContent).not.toContain("0.0.0-bundled");
    expect(D.getElementById("serverStat").textContent).toBe("server not answering");
    expect(D.getElementById("resSrc").textContent).toBe("unavailable");
  });

  it("still renders the bundled snapshot when opened from a file", async () => {
    const { dom } = boot("file:///tmp/app.html");
    await dom.window.Landing.render();
    expect(dom.window.document.getElementById("residents").textContent).toContain("BundledResident");
  });

  it("renders the bundled snapshot for an explicit ?snapshot=1 preview", async () => {
    const { dom } = boot("https://gov.example/dashboard?snapshot=1");
    await dom.window.Landing.render();
    expect(dom.window.document.getElementById("residents").textContent).toContain("BundledResident");
  });

  it("retries residents and health on the stats cadence until they answer live", async () => {
    const { dom, state } = boot("https://gov.example/dashboard");
    await dom.window.Landing.render();
    const before = state.residentCalls;
    state.failResidents = false; state.failHealth = false;   // the server is back
    await dom.window.Landing.refreshStats();
    expect(state.residentCalls).toBeGreaterThan(before);
    expect(dom.window.document.getElementById("serverStat").textContent).toContain("9.9.9");
    // Once both are live, the stats cadence stops re-reading health. (An
    // empty roster is re-read every tick regardless — existing behaviour.)
    const settled = state.healthCalls;
    await dom.window.Landing.refreshStats();
    expect(state.healthCalls).toBe(settled);
  });
});
