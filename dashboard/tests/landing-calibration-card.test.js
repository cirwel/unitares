import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The landing "Calibration" card. Its name promises the calibration verdict,
// so the verdict is what it must lead with. Before this, the card rendered
// `trajectory_health` — a DIFFERENT quantity from the same calibration(check)
// response — and coloured itself green at >= 0.8, while the server was
// answering calibrated=false / calibration_status="miscalibrated" /
// tactical_signal_status="stale". Live on 2026-08-28 that was 0.784: a reader
// saw "Calibration 0.78" and inferred a status the system was not reporting,
// and a 0.016 drift would have painted the card outright OK.

const landingSource = readFileSync(
  new URL("../redesign/sections/landing.js", import.meta.url),
  "utf8",
);

const RESIDENT = {
  name: "Sentinel", status: "healthy", coherence: 0.5, risk: 0.1,
  verdict: "proceed", silence: 10, silenceThreshold: 3600,
  eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
};

async function card(statsOverrides) {
  const dom = new JSDOM(`
    <div id="resSrc"></div><div id="residents"></div><div id="attn"></div>
    <div id="stats"></div><div id="serverStat"></div>
    <div id="pulseWho"></div><div id="pulseFresh"></div>
    <div id="riskVal"></div><div id="riskFill"></div>
    <div id="pulseVerdict"><span></span><span></span></div>
    <div id="eisv"></div><div id="foot"></div>
  `, { runScripts: "outside-only", url: "https://governance.test/" });
  dom.window.DATA = {
    residentLiveness: () => "reporting",
    health: async () => ({ source: "live", data: { version: "t", uptime: "1h", db: "ok" } }),
    residents: async () => ({ source: "live", data: [RESIDENT] }),
    stats: async () => ({
      source: "live",
      data: Object.assign({
        agentsActive: 1, agentsLive: 1, agentsPresenceUnknown: 0,
        agentsPresenceUnavailable: 0, agentsTotal: 1,
        stuck: 0, stuckHard: 0, stuckSoft: 0, stuckList: [], degraded: 0,
      }, statsOverrides),
    }),
    automationsSummary: async () => ({
      source: "live",
      data: { summary: { total: 0, by_kind: {}, needs_attention: [] }, ungated: 0 },
    }),
  };
  dom.window.eval(landingSource);
  await dom.window.Landing.render();
  const el = [...dom.window.document.querySelectorAll(".card")]
    .find((c) => c.querySelector("h3")?.textContent === "Calibration");
  return {
    num: el.querySelector(".num").textContent.trim(),
    sub: el.querySelector(".sub").textContent.trim(),
    // `cls` is applied to the .sub element, not the card wrapper.
    cls: el.querySelector(".sub").className,
    text: el.textContent,
  };
}

describe("landing calibration card", () => {
  it("leads with the verdict, not the trajectory-health number", async () => {
    // The exact live shape on 2026-08-28.
    const c = await card({
      calibration: 0.7836879432624113,
      calibrated: false,
      calibrationStatus: "miscalibrated",
      calibrationSignal: "stale",
    });
    expect(c.num).toBe("miscalibrated");
    // The number survives, demoted to where it cannot be read as the status.
    expect(c.sub).toContain("trajectory health 0.78");
    expect(c.sub).toContain("tactical signal stale");
  });

  it("never paints green on a healthy number while the server says miscalibrated", async () => {
    // 0.81 clears the OLD >= 0.8 green threshold. The verdict still governs.
    const c = await card({
      calibration: 0.81, calibrated: false,
      calibrationStatus: "miscalibrated", calibrationSignal: "stale",
    });
    expect(c.cls).not.toContain("up");
    expect(c.cls).toContain("down");
    expect(c.num).toBe("miscalibrated");
  });

  it("goes green only when the server actually says calibrated", async () => {
    const c = await card({
      calibration: 0.42, calibrated: true,
      calibrationStatus: "calibrated", calibrationSignal: "fresh",
    });
    expect(c.num).toBe("calibrated");
    expect(c.cls).toContain("up");
    // A fresh tactical signal is the quiet default — no need to say it.
    expect(c.sub).not.toContain("tactical signal");
    // Even a low trajectory_health must not flip a calibrated verdict.
    expect(c.sub).toContain("trajectory health 0.42");
  });

  it("stays neutral when the verdict is unavailable, never green", async () => {
    // Degraded fetch: the number arrived, the verdict did not. Falling back to
    // the number is fine; colouring it OK on that basis is not.
    const c = await card({ calibration: 0.95, calibrated: null });
    expect(c.num).toBe("0.95");
    expect(c.cls).not.toContain("up");
    expect(c.cls).not.toContain("down");
  });

  it("says unavailable when nothing came back", async () => {
    const c = await card({ calibration: null, calibrated: null });
    expect(c.num).toBe("—");
    expect(c.sub).toBe("unavailable");
  });
});
