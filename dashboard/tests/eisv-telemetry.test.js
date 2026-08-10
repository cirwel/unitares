import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const dataSource = readFileSync(
  new URL("../redesign/data.js", import.meta.url),
  "utf8",
);
const eisvSource = readFileSync(
  new URL("../redesign/sections/eisv.js", import.meta.url),
  "utf8",
);

function events() {
  return [
    {
      type: "eisv_update",
      timestamp: "2026-08-09T18:01:00Z",
      eisv: { E: 0.2, I: 0.8, S: 0.1, V: -0.3 },
      coherence: 0.5,
      risk: 0.1,
      eisv_telemetry: {
        measurement_source: "physical",
        primary_source: "behavioral",
        behavioral_confidence: 0.9,
        missing_inputs: [],
        enforcement_requested: false,
        enforcement_applied: false,
      },
    },
    {
      type: "eisv_update",
      timestamp: "2026-08-09T18:01:30Z",
      eisv: { E: 0.8, I: 0.6, S: 0.4, V: 0.2 },
      coherence: 0.45,
      risk: 0.2,
      eisv_telemetry: {
        measurement_source: "behavioral_sensor",
        primary_source: "behavioral",
        behavioral_confidence: 0.7,
        missing_inputs: ["outcome_history"],
        enforcement_requested: true,
        enforcement_applied: false,
      },
    },
  ];
}

describe("EISV telemetry source separation", () => {
  it("buckets a selected source without averaging another instrument into it", () => {
    const dom = new JSDOM("", { runScripts: "outside-only", url: "https://governance.test/#eisv" });
    dom.window.SNAPSHOT = {};
    dom.window.eval(dataSource);

    const physical = dom.window.DATA.bucketEisv(events(), "physical");
    const mixed = dom.window.DATA.bucketEisv(events(), "all");
    const lanes = dom.window.DATA.summarizeEisvSources(events());

    expect(physical).toHaveLength(1);
    expect(physical[0].E).toBe(0.2);
    expect(mixed[0].E).toBe(0.5);
    expect(lanes.map((lane) => lane.source)).toEqual(["behavioral_sensor", "physical"]);
    expect(lanes[0].missingObservations).toBe(1);
    expect(lanes[0].enforcementRequested).toBe(1);
    expect(lanes[0].enforcementApplied).toBe(0);
  });

  it("renders neutral measurement lanes and keeps the source selector live", async () => {
    const dom = new JSDOM('<div id="eisv-mount"></div>', {
      runScripts: "outside-only",
      url: "https://governance.test/#eisv",
    });
    dom.window.SNAPSHOT = {};
    dom.window.eval(dataSource);
    const raw = events();
    dom.window.DATA.eisv = async () => ({
      source: "live",
      data: {
        raw,
        series: dom.window.DATA.bucketEisv(raw),
        sourceLanes: dom.window.DATA.summarizeEisvSources(raw),
        coherenceEq: 0.5,
      },
    });
    dom.window.DATA.residents = async () => ({
      source: "live",
      data: [{
        id: "agent-1", name: "Agent 1",
        eisv: { E: 0.5, I: 0.7, S: 0.2, V: -0.1 }, coherence: 0.5,
      }],
    });
    dom.window.DATA.agentHistory = async () => ({ source: "live", data: { points: [] } });

    const charts = [];
    dom.window.Chart = class FakeChart {
      constructor(_canvas, config) {
        this.data = config.data;
        charts.push(this);
      }
      destroy() {}
      update() {}
    };

    dom.window.eval(eisvSource);
    await dom.window.EISV.load();

    const mount = dom.window.document.getElementById("eisv-mount");
    expect(mount.textContent).toContain("Measurement lanes");
    expect(mount.textContent).toContain("sources never averaged together here");
    expect(mount.textContent).toContain("Fleet readings");
    expect(mount.textContent).toContain("neutral display");
    expect(mount.textContent).not.toContain("green = healthy");

    const selector = dom.window.document.getElementById("eisv-source-filter");
    expect(Array.from(selector.options).map((option) => option.value)).toEqual([
      "all", "behavioral_sensor", "physical",
    ]);
    selector.value = "physical";
    selector.dispatchEvent(new dom.window.Event("change", { bubbles: true }));

    expect(dom.window.document.getElementById("eisv-window-label").textContent).toContain("physical");
    expect(charts[0].data.datasets[0].data).toEqual([0.2]);
  });
});
