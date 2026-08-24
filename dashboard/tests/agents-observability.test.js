import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const sectionSource = readFileSync(
  new URL("../redesign/sections/agents.js", import.meta.url),
  "utf8",
);

describe("agents observability semantics", () => {
  it("renders persisted state, observation provenance, and soft silence honestly", async () => {
    const dom = new JSDOM('<div id="ag-mount"></div>', {
      runScripts: "outside-only",
      url: "https://governance.test/#agents",
    });
    const last = new Date(Date.now() - 14 * 60 * 60 * 1000).toISOString();
    dom.window.DATA = {
      agents: async () => ({
        source: "live",
        data: {
          list: [{
            agent_id: "Claude_Code_20260809_7abd8537",
            label: "claude-unitares#33c592ca",
            status: "active",
            tier: "emerging",
            updates: 42,
            last,
            tags: [],
            presence: { status: "unknown", signals: [] },
            metrics: {
              E: 0.70, I: 0.74, S: 0.28, V: -0.04,
              coherence: 0.48, risk: 0, riskSource: "resolved",
              phiRiskCurrent: 0.88, verdict: "safe",
              source: "persisted_state", recordedAt: last,
              rollingMetricsAvailable: false,
            },
          }],
          summary: {
            total: 1, active: 1, observed: 1, unobserved: 0,
            live: 0, presenceUnknown: 1, presenceUnavailable: 0, archived: 0,
          },
        },
      }),
      residentFreshness: async () => ({ source: "live", data: {} }),
      stuckAgents: async () => ({
        source: "live",
        data: [{
          id: "Claude_Code_20260809_7abd8537",
          reason: "cadence_silence",
          soft: true,
          details: "Possibly finished or silent; verify.",
        }],
      }),
      agentHistory: async () => ({
        source: "live",
        data: {
          total: 42,
          observationSummary: {
            state_rows: 42,
            agent_reports: 0,
            substrate_rows: 42,
            other_rows: 0,
            telemetry_envelopes: 0,
          },
          points: [{
            t: last,
            E: 0.70, I: 0.74, S: 0.28, V: -0.04,
            coherence: 0.48, risk: 0,
            epistemic_class: "substrate_interpretation",
            telemetry_available: false,
          }],
        },
      }),
      residentLiveness: () => "down",
    };
    dom.window.Chart = class FakeChart {
      constructor(_canvas, config) { this.data = config.data; }
      destroy() {}
    };

    dom.window.eval(sectionSource);
    await dom.window.Agents.load();

    let text = dom.window.document.getElementById("ag-mount").textContent;
    expect(text).toContain("State rows");
    expect(text).toContain("observed");
    expect(text).toContain("Decision risk");
    expect(text).not.toContain("Raw risk telemetry");

    dom.window.document.querySelector(".ag-row").dispatchEvent(
      new dom.window.Event("click", { bubbles: true }),
    );
    await new Promise((resolve) => dom.window.setTimeout(resolve, 0));

    text = dom.window.document.getElementById("ag-mount").textContent;
    expect(text).toContain("Possible cadence silence");
    expect(text).not.toContain("Flagged stuck");
    expect(text).toContain("persisted");
    expect(text).toContain("rolling metrics unavailable");
    expect(text).toContain("registry lifecycle");
    expect(text).toContain("runtime presence");
    expect(text).toContain("unknown · no live binding/lease");
    expect(text).toContain("state rows");
    expect(text).toContain("Last observation");
    expect(text).not.toContain("Last seen");
    expect(text).toContain("state observations of 42");
    expect(text).toContain("0 authored");
    expect(text).toContain("42 automatic");
    expect(text).toContain("telemetry legacy/missing");
    expect(text).toContain("decision risk 0.00");
    expect(text).toContain("Φ risk 0.88");
  });

  it("keeps positive presence independent from archived lifecycle and state rows", async () => {
    const dom = new JSDOM('<div id="ag-mount"></div>', {
      runScripts: "outside-only",
      url: "https://governance.test/#agents",
    });
    dom.window.DATA = {
      agents: async () => ({
        source: "live",
        data: {
          list: [{
            agent_id: "archived-but-bound",
            label: "Resident",
            status: "archived",
            updates: 0,
            tags: [],
            presence: { status: "unknown", signals: [] },
            metrics: {},
          }],
          summary: {
            total: 1, active: 0, archived: 1, observed: 0, unobserved: 1,
            live: 1, presenceUnknown: 0, presenceUnavailable: 0,
          },
        },
      }),
      residentFreshness: async () => ({
        source: "live",
        data: { Resident: { silence: 5, status: "healthy", coherence: 0.5 } },
      }),
      residentLiveness: () => "reporting",
      stuckAgents: async () => ({ source: "live", data: [] }),
    };

    dom.window.eval(sectionSource);
    await dom.window.Agents.load();

    const text = dom.window.document.getElementById("ag-mount").textContent;
    expect(text).toContain("No state observations — 1");
    expect(text).toContain("Resident");
    expect(text).toContain("live");
    expect(text).not.toContain("not live · registry archived");
  });
});
