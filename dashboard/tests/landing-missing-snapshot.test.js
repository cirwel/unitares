import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The Overview must render on live data when snapshot.js did not load.
//
// snapshot.js is auth-gated ON PURPOSE — it bundles resident ids, EISV vectors,
// coherence, risk and verdicts, the same data class /v1/eisv/* is gated for.
// app.html loads it with a plain <script src>, which sends COOKIES and not the
// bearer token. So an operator authenticated by bearer token gets 200 on every
// REST call and 401 on snapshot.js, and window.SNAPSHOT is never defined.
//
// DATA.stats() used to read `const snap = S().stats` as its FIRST statement,
// before any try. With no SNAPSHOT that throws TypeError, and because render()
// awaits Promise.all the whole Overview died before renderStats ran — while
// refresh() (health + residents, both lazily-fallen-back) kept painting the
// resident strip and pulse. Observed 2026-08-28 over the tunnel: resident fleet
// and pulse visible, all nine headline cards missing.
//
// The rule this encodes: an unreachable FALLBACK must never break the path that
// did not need it.

const dataSource = readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8");
const landingSource = readFileSync(new URL("../redesign/sections/landing.js", import.meta.url), "utf8");

function bootWithoutSnapshot() {
  const dom = new JSDOM(`
    <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
    <div id="stats"></div><div id="serverStat"></div>
    <div id="pulseWho"></div><div id="pulseFresh"></div>
    <div id="riskVal"></div><div id="riskFill"></div>
    <div id="pulseVerdict"><span></span><span></span></div>
    <div id="eisv"></div><div id="foot"></div>
  `, { runScripts: "outside-only", url: "https://gov.example/" });

  // Live REST succeeds (bearer token), exactly as over the tunnel.
  dom.window.fetch = async (url) => {
    const u = String(url);
    const body =
      u.includes("/health") ? { status: "healthy", version: "2.20.0", uptime: "1h",
                                status_breakdown: {}, checks: {} }
      : u.includes("/v1/residents") ? { residents: [{
          agent_id: "u1", label: "Watcher", status: "healthy", coherence: 0.48,
          risk_score: 0.15, verdict: "proceed", eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
          silence_seconds: 20, silence_threshold_seconds: 3600 }] }
      : u.includes("/api/automations") ? { summary: { total: 1, by_kind: {}, needs_attention: [] },
                                           ungated: 0, unclassified: 0, stale: false }
      : { success: true };
    // A plain object, not dom.window.Response — jsdom does not implement a
    // usable fetch Response, and a stub that silently fails every call would
    // make this test pass for the wrong reason.
    return { ok: true, status: 200, json: async () => body };
  };

  // snapshot.js is deliberately NOT evaluated — this is the whole point.
  dom.window.eval(dataSource);
  expect(dom.window.SNAPSHOT).toBeUndefined();
  dom.window.eval(landingSource);
  return dom;
}

describe("overview with no snapshot bundle", () => {
  it("still renders the headline cards on live data", async () => {
    const dom = bootWithoutSnapshot();
    await dom.window.Landing.render();
    // The regression was zero cards while residents/pulse painted.
    expect(dom.window.document.querySelectorAll(".card").length).toBeGreaterThan(5);
  });

  it("renders the resident strip and pulse too, not one at the cost of the other", async () => {
    const dom = bootWithoutSnapshot();
    await dom.window.Landing.render();
    const D = dom.window.document;
    expect(D.getElementById("residents").innerHTML.length).toBeGreaterThan(50);
    expect(D.getElementById("pulseWho").textContent.trim()).not.toBe("");
  });

  it("reads the snapshot only from inside the fallback, never eagerly", () => {
    // Structural guard: stats() must not touch S() before its try. A future
    // edit reintroducing an eager read would pass the behavioural tests above
    // whenever a snapshot happens to be present, and fail only over the tunnel.
    const stats = dataSource.slice(dataSource.indexOf("async stats()"));
    const head = stats.slice(0, stats.indexOf("withFallback"));
    expect(head).not.toMatch(/S\(\)/);
  });

  it("has a snapshot accessor that cannot throw", () => {
    expect(dataSource).toMatch(/const S = \(\) => window\.SNAPSHOT \|\| \{\}/);
  });
});
