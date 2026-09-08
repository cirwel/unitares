import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The landing "Anomalies" card. Zero anomalies is only an all-clear if the
// scan that produced it covered the fleet. The server's default scan stops at
// scan.scan_cap active agents (DEFAULT_ANOMALY_SCAN_CAP, 50) and reports
// scan.truncated; before this, the card read summary.total_anomalies alone and
// rendered a green "clear" for a fleet it had never finished looking at.
//
// Same failure as the Calibration card next door (see
// landing-calibration-card.test.js): a number shown without the qualifier the
// server sent alongside it, letting a reader infer a status the system never
// reported.

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
    .find((c) => c.querySelector("h3")?.textContent === "Anomalies");
  return {
    num: el.querySelector(".num").textContent.trim(),
    sub: el.querySelector(".sub").textContent.trim(),
    // `cls` is applied to the .sub element, not the card wrapper.
    cls: el.querySelector(".sub").className,
    text: el.textContent,
  };
}

describe("landing anomalies card", () => {
  it("never claims 'clear' on a scan that did not cover the fleet", async () => {
    const c = await card({
      anomalies: 0, anomaliesTruncated: true,
      anomaliesScanned: 50, anomaliesActive: 57,
    });
    expect(c.num).toBe("0");
    expect(c.sub).not.toContain("clear");
    expect(c.sub).toContain("none found");
    // The scope is stated, so a reader can see what the zero covers.
    expect(c.sub).toContain("scanned 50 of 57 agents");
  });

  it("does not paint a truncated zero green", async () => {
    const c = await card({
      anomalies: 0, anomaliesTruncated: true,
      anomaliesScanned: 50, anomaliesActive: 57,
    });
    expect(c.cls).not.toContain("up");
    expect(c.cls).not.toContain("down");
  });

  it("still says clear, in green, when the scan covered the fleet", async () => {
    const c = await card({
      anomalies: 0, anomaliesTruncated: false,
      anomaliesScanned: 12, anomaliesActive: 12,
    });
    expect(c.sub).toBe("clear");
    expect(c.cls).toContain("up");
  });

  it("reports scope alongside a non-zero count too", async () => {
    // A truncated scan that DID find something is still partial: there may be
    // more in the agents it never reached.
    const c = await card({
      anomalies: 3, anomaliesTruncated: true,
      anomaliesScanned: 50, anomaliesActive: 120,
    });
    expect(c.num).toBe("3");
    expect(c.sub).toContain("3 active");
    expect(c.sub).toContain("scanned 50 of 120 agents");
    // A real finding still reads as bad news.
    expect(c.cls).toContain("down");
  });

  it("renders as before against a server that sends no scan block", async () => {
    // Older server, or the offline snapshot capture: scope unknown, so the
    // card must not invent it — and must not regress into silence either.
    const clear = await card({
      anomalies: 0, anomaliesTruncated: null,
      anomaliesScanned: null, anomaliesActive: null,
    });
    expect(clear.sub).toBe("clear");
    expect(clear.cls).toContain("up");

    const active = await card({ anomalies: 2, anomaliesTruncated: null });
    expect(active.sub).toBe("2 active");
    expect(active.cls).toContain("down");
  });

  it("says unavailable when nothing came back", async () => {
    const c = await card({ anomalies: null, anomaliesTruncated: null });
    expect(c.num).toBe("—");
    expect(c.sub).toBe("unavailable");
  });
});
