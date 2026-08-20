import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const landingSource = readFileSync(
  new URL("../redesign/sections/landing.js", import.meta.url),
  "utf8",
);

describe("landing agent presence", () => {
  it("uses explicit live signals instead of registry-active lifecycle counts", async () => {
    const dom = new JSDOM(`
      <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
      <div id="stats"></div><div id="serverStat"></div>
      <div id="pulseWho"></div><div id="pulseFresh"></div>
      <div id="riskVal"></div><div id="riskFill"></div>
      <div id="pulseVerdict"><span></span><span></span></div>
      <div id="eisv"></div><div id="foot"></div>
    `, { runScripts: "outside-only", url: "https://governance.test/" });
    const resident = {
      name: "Sentinel", status: "healthy", coherence: 0.5, risk: 0.1,
      verdict: "proceed", silence: 10, silenceThreshold: 3600,
      eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
    };
    dom.window.DATA = {
      residentLiveness: (r) => r.status === "healthy" && r.coherence != null
        ? "reporting" : "down",
      health: async () => ({
        source: "live", data: { version: "test", uptime: "1h", db: "ok" },
      }),
      residents: async () => ({ source: "live", data: [resident] }),
      stats: async () => ({
        source: "live",
        data: {
          agentsActive: 9,
          agentsLive: 2,
          agentsPresenceUnknown: 7,
          agentsPresenceUnavailable: 1,
          agentsTotal: 10,
          stuck: 0,
          stuckHard: 0,
          stuckSoft: 0,
          stuckList: [],
          degraded: 0,
        },
      }),
      automations: async () => ({
        source: "live",
        data: { summary: { total: 0, by_kind: {}, needs_attention: [] }, automations: [] },
      }),
    };

    dom.window.eval(landingSource);
    await dom.window.Landing.render();

    const card = [...dom.window.document.querySelectorAll(".card")]
      .find((el) => el.querySelector("h3")?.textContent === "Agents");
    expect(card).toBeTruthy();
    expect(card.querySelector(".num").textContent).toContain("2");
    expect(card.querySelector(".of").textContent).toContain("/ 10");
    // Denominator labeling landed in #1707: the card now names the window it
    // counts over and says "presence unknown" rather than a bare "unknown".
    expect(card.querySelector(".sub").textContent).toBe(
      "live binding/lease · 30d window · 8 presence unknown",
    );
    expect(card.textContent).not.toContain("active / total");
  });
});
