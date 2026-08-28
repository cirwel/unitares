import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The Fleet Coherence card — first card on the default page.
//
// It shipped with `cls: "up"` hardcoded, the only one of nine cards that did
// not derive its own state, so it painted green unconditionally. Live on
// 2026-08-28 it read green while its own subtitle said "1 not checking in" and
// the attention band beside it said "Doctor past check-in threshold".
//
// Neutral is the honest default: the number is a fleet mean of a metric whose
// between-agent sd is ~0.008 over 39k rows, so it cannot move enough to earn a
// health colour, and eisv.js states the standing policy — "a neutral surface
// rather than converting observations into red/green verdicts". Cadence is the
// one thing the card genuinely knows, so that is what the colour reports.

const landingSource = readFileSync(
  new URL("../redesign/sections/landing.js", import.meta.url),
  "utf8",
);

const R = (over) => Object.assign({
  name: "R", status: "healthy", coherence: 0.48, risk: 0.1, verdict: "proceed",
  silence: 10, silenceThreshold: 3600, eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
}, over);

async function coherenceCard(residents) {
  const dom = new JSDOM(`
    <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
    <div id="stats"></div><div id="serverStat"></div>
    <div id="pulseWho"></div><div id="pulseFresh"></div>
    <div id="riskVal"></div><div id="riskFill"></div>
    <div id="pulseVerdict"><span></span><span></span></div>
    <div id="eisv"></div><div id="foot"></div>
  `, { runScripts: "outside-only", url: "https://governance.test/" });
  dom.window.DATA = {
    residentLiveness: (r) => (r.status === "healthy" && r.coherence != null ? "reporting" : "down"),
    health: async () => ({ source: "live", data: { version: "t", uptime: "1h", db: "ok" } }),
    residents: async () => ({ source: "live", data: residents }),
    stats: async () => ({ source: "live", data: {
      agentsActive: 1, agentsLive: 1, agentsPresenceUnknown: 0, agentsPresenceUnavailable: 0,
      agentsTotal: 1, stuck: 0, stuckHard: 0, stuckSoft: 0, stuckList: [], degraded: 0,
    } }),
    automationsSummary: async () => ({ source: "live", data: {
      summary: { total: 0, by_kind: {}, needs_attention: [] }, ungated: 0,
    } }),
  };
  dom.window.eval(landingSource);
  await dom.window.Landing.render();
  const el = [...dom.window.document.querySelectorAll(".card")]
    .find((c) => c.querySelector("h3")?.textContent === "Fleet Coherence");
  return { sub: el.querySelector(".sub"), win: dom.window };
}

describe("landing fleet coherence card", () => {
  it("is never green — the metric cannot move enough to earn one", async () => {
    const { sub } = await coherenceCard([R({ name: "A" }), R({ name: "B" })]);
    expect(sub.className).not.toContain("up");
    expect(sub.textContent).toContain("2 of 2 in cadence");
  });

  it("goes amber when a resident stops checking in", async () => {
    // The exact live shape: healthy residents plus one that is down.
    const { sub } = await coherenceCard([
      R({ name: "A" }), R({ name: "B" }),
      R({ name: "Doctor", status: "silent", coherence: null }),
    ]);
    expect(sub.className).toContain("down");
    expect(sub.textContent).toContain("not checking in");
  });

  it("stays neutral while every resident is in cadence", async () => {
    const { sub } = await coherenceCard([R({ name: "A" })]);
    expect(sub.className.trim()).toBe("sub");
  });

  it("keeps the colour in step when a resident returns, not just on first render", async () => {
    // updateFleetCoherence rewrites the card in place on a live WS check-in
    // (applyEvent), which is the ONLY in-place path — the 10s tick deliberately
    // refreshes just the residents strip and pulse. Before the fix that updater
    // rewrote the number and subtitle but not the class, so a resident coming
    // back would clear the words while the amber stayed frozen on.
    const { sub, win } = await coherenceCard([
      R({ name: "A" }),
      R({ name: "Doctor", status: "silent", coherence: null }),
    ]);
    expect(sub.className).toContain("down");

    const handled = win.Landing.applyEvent({
      type: "eisv_update", agent_name: "Doctor", coherence: 0.48,
      eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
    });
    expect(handled).toBe(true);

    const after = [...win.document.querySelectorAll(".card")]
      .find((c) => c.querySelector("h3")?.textContent === "Fleet Coherence")
      .querySelector(".sub");
    expect(after.className).not.toContain("down");
    expect(after.textContent).toContain("2 of 2 in cadence");
  });

});
