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

function observation(primarySource = "behavioral") {
  return {
    t: "2026-09-13T12:00:00Z", E: 0.5, I: 0.7, S: 0.2, V: -0.1,
    telemetry: { primary_source: primarySource, measurement_source: primarySource },
    telemetry_envelope: {
      observed_at: "2026-09-13T12:00:00Z",
      measurement: {
        primary: { source: primarySource, values: { E: 0.5, I: 0.7, S: 0.2, V: -0.1 } },
        behavioral: {
          observation_source: "behavioral_sensor", raw_observation: { E: 0, I: 0.8, S: 0.1 },
          smoothed: { E: 0.3, I: 0.7, S: 0.2, V: -0.1 },
          updates: 2, warmup: { phase: "bootstrapping" }, v_formula_version: 2,
        },
      },
      derivation: {
        missing_inputs: [],
        inputs: { features: { tool_error_rate: 0, calibration_error: null } },
        calibration_signal: {
          schema: "eisv.calibration-signal.v1", policy_applied: false,
          deployed: { scope: "fleet", calibration_error: 0.2, sample_count: 30,
            eligible_sample_count: 25, eligible_bin_count: 3,
            freshness_status: "unknown" },
          agent_candidate: { scope: "agent", evidence_status: "available",
            calibration_error: 0.1, sample_count: 6, eligible_sample_count: 6,
            eligible_bin_count: 1, freshness_status: "recent", age_days: 1,
            sample_window: "lifetime", freshness_rule: "per_bin_recent_activity",
            freshness_note: "Recent activity does not imply all supporting samples are recent." },
        },
        components: {
          schema: "behavioral.components.v1", policy_applied: false,
          dimensions: {
            E: { value: 0.3, base_value: 0.2,
              components: [
                { name: "tool outcomes", source: "tool_usage", value: 0, weight: 0.15, weighted_contribution: 0, observed: true },
                { name: "unavailable calibration", source: "calibration", value: null, weight: 0.3, weighted_contribution: null, observed: false, default_reason: "No calibration sample" },
              ],
              adjustments: [{ name: "continuity", source: "reported_structure", input_value: 0.7, input_weight: 0.2, retained_weight: 0.8, output_value: 0.3, observed: true }],
            },
          },
        },
      },
    },
  };
}

async function section(historyResult, raw = events()) {
  const dom = new JSDOM('<div id="eisv-mount"></div>', {
    runScripts: "outside-only", url: "https://governance.test/#eisv",
  });
  dom.window.Date.now = () => Date.parse("2026-09-13T12:05:00Z");
  dom.window.eval(dataSource);
  dom.window.DATA.eisv = async () => ({ source: "live", data: { raw, series: [], sourceLanes: [], coherenceEq: 0.5 } });
  dom.window.DATA.residents = async () => ({ source: "live", data: [
    { id: "agent-1", name: "Agent 1", eisv: { E: 0.5, I: 0.7, S: 0.2, V: -0.1 } },
    { id: "agent-2", name: "Agent 2", eisv: { E: 0.4, I: 0.6, S: 0.1, V: -0.1 } },
  ] });
  const calls = [], charts = [];
  dom.window.DATA.agentHistory = async (id, options) => {
    calls.push({ id, options });
    return typeof historyResult === "function" ? historyResult(id, options) : historyResult;
  };
  dom.window.Chart = class FakeChart {
    constructor(canvas, config) { this.canvas = canvas; this.data = config.data; this.options = config.options; charts.push(this); }
    destroy() { this.destroyed = true; }
    update() {}
  };
  dom.window.eval(eisvSource);
  await dom.window.EISV.load();
  return { dom, calls, charts };
}

async function click(dom, selector) {
  dom.window.document.querySelector(selector).click();
  await new Promise((resolve) => dom.window.setTimeout(resolve, 0));
}

describe("EISV telemetry source separation", () => {
  it("does not draw the startup source change as a behavioral V drop", async () => {
    // Representative values from the diagnosed third-update mechanism. No
    // agent identity or report text is retained in this synthetic history.
    const points = [
      ["ode_fallback", "ode_fallback", -0.003, 1],
      ["ode_fallback", "ode_fallback", -0.020, 1],
      ["behavioral", "continuity_fallback", -0.153137, 2],
      ["behavioral", "behavioral_sensor", -0.10, 3],
      ["behavioral", "behavioral_sensor", 0, 3],
      ["behavioral", "continuity_fallback", -0.12, 4],
    ].map(([primary, source, V, segment], i) => ({
      t: `2026-11-04T12:0${i}:00Z`, E: 0.3, I: 0.5, S: 0.2, V, coherence: 0.49,
      telemetry: { primary_source: primary, measurement_source: source,
        behavioral_source: i < 3 || i === 5 ? "continuity_fallback" : "behavioral_sensor",
        coherence_source: "legacy_tanh_v", coherence_role: "ode_control_feedback" },
      chart_segments: { eisv: segment, coherence: 1 },
    }));
    const { dom, charts } = await section({ source: "live", data: { points } });
    await click(dom, '[data-traj-id="agent-1"]');
    const lower = charts.find((c) => c.canvas.id === "eisv-traj-lower");
    const valence = lower.data.datasets.filter((d) => d.label.startsWith("Valence"));
    expect(valence.map((d) => d.data)).toEqual([
      [-0.003, -0.020, null, null, null, null],
      [null, null, -0.153137, null, null, -0.12],
      [null, null, null, -0.10, 0, null],
    ]);
    valence.forEach((d) => {
      expect(d.spanGaps).toBe(false);
      expect(d.fill).toBe(false);
      expect(d.tension).toBe(0);
      expect(d.pointRadius).toBeGreaterThan(0); // update 3 must not disappear
    });
    const upper = charts.find((c) => c.canvas.id === "eisv-traj-upper");
    const coherence = upper.data.datasets.filter((d) => d.label.startsWith("Coherence"));
    expect(coherence).toHaveLength(1);
    expect(coherence[0].label).toContain("ODE control feedback");
    expect(coherence[0].data).toEqual(points.map((p) => p.coherence));
    expect(lower.options.plugins.tooltip.callbacks.title([{ dataIndex: 2 }])).toBe(points[2].t);
    expect(lower.options.plugins.tooltip.callbacks.afterBody([{ dataIndex: 2 }]).join(" "))
      .toContain("Estimator: behavioral · consumed source: continuity_fallback");
    dom.window.EISV.retheme();
    expect(charts.at(-1).data.datasets.filter((d) => d.label.startsWith("Valence")).map((d) => d.data))
      .toEqual(valence.map((d) => d.data));
    expect(dom.window.document.getElementById("eisv-trajectory").textContent)
      .toContain("only where it supplied the reported values");
  });

  it("breaks same-source lines across hidden transitions and absent run provenance", async () => {
    const points = [1, 1, 3, undefined].map((segment, i) => ({
      ...observation(), V: i === 3 ? NaN : 0,
      telemetry: { primary_source: "behavioral", measurement_source: "behavioral_sensor" },
      chart_segments: { eisv: segment },
    }));
    const { dom, charts } = await section({ source: "live", data: { points } });
    await click(dom, '[data-traj-id="agent-1"]');
    const v = charts.at(-1).data.datasets.find((d) => d.label.startsWith("Valence"));
    const color = (a, b) => v.segment.borderColor({ p0DataIndex: a, p1DataIndex: b });
    expect(v.data).toEqual([0, 0, 0, null]);
    expect(color(0, 1)).not.toBe("transparent");
    expect(color(1, 2)).toBe("transparent"); // B omitted between two A samples
    expect(color(2, 3)).toBe("transparent"); // old server cannot establish continuity
  });

  it("shows missing and legacy-inferred producers as isolated observations", async () => {
    const points = [
      { t: "2026-11-04T12:00:00Z", E: 0, V: null },
      { t: "2026-11-04T12:01:00Z", E: 0.5, V: -0.2,
        telemetry: { schema: "eisv.telemetry.summary.legacy", primary_source: "ode_fallback", measurement_source: "ode_fallback" } },
    ];
    const { dom, charts } = await section({ source: "live", data: { points } });
    await click(dom, '[data-traj-id="agent-1"]');
    charts.slice(-2).flatMap((c) => c.data.datasets).forEach((d) => {
      expect(d.showLine).toBe(false);
      expect(d.label).toContain("source unrecorded");
    });
    expect(charts.at(-2).data.datasets[0].data).toEqual([0, null]);
  });

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

  it("distinguishes unknown input coverage from a recorded empty list and shows the newest time", async () => {
    const raw = [
      { ...events()[0], timestamp: "2026-09-13T12:04:00Z", eisv_telemetry: {} },
      { ...events()[0], timestamp: "2026-09-13T12:00:00Z", eisv_telemetry: {} },
      { ...events()[1], timestamp: "2026-09-13T12:03:00Z", eisv_telemetry: { measurement_source: "behavioral_sensor", missing_inputs: [] } },
    ];
    const { dom } = await section({ source: "live", data: { points: [] } }, raw);
    const lanes = dom.window.DATA.summarizeEisvSources(raw);
    const unknown = lanes.find((lane) => lane.source === "unknown");
    expect(unknown.unknownProvenance).toBe(2);
    expect(unknown.latest).toBe("2026-09-13T12:04:00Z");
    const text = dom.window.document.getElementById("eisv-source-lanes").textContent;
    expect(text).toContain("2 observation(s): input coverage unrecorded");
    expect(text).toContain("No missing inputs reported");
    expect(text).toContain("1m ago");
  });

  it("requests only the latest envelope through the data seam and preserves live empty data", async () => {
    const dom = new JSDOM("", { runScripts: "outside-only", url: "https://governance.test/#eisv" });
    const paths = [];
    dom.window.SNAPSHOT = { agentHistory: { "agent-1": [observation()] }, eisv: { raw: events() } };
    dom.window.fetch = async (path) => {
      paths.push(path);
      return { ok: true, status: 200, json: async () => path.includes("/history") ? {
        points: [], total: 0, telemetry_mode: "latest",
        trajectory_context: { observations: 0, policy_applied: false },
      } : { events: [] } };
    };
    dom.window.eval(dataSource);
    const history = await dom.window.DATA.agentHistory("agent-1", { limit: 1, includeTelemetry: "latest" });
    expect(paths[0]).toBe("/v1/agents/agent-1/history?limit=1&include_telemetry=latest");
    expect(history.source).toBe("live");
    expect(history.data.points).toEqual([]);
    expect(history.data.telemetryMode).toBe("latest");
    expect(history.data.trajectoryContext).toEqual({ observations: 0, policy_applied: false });
    const fleet = await dom.window.DATA.eisv();
    expect(fleet.source).toBe("live");
    expect(fleet.data.raw).toEqual([]);
    dom.window.SNAPSHOT = undefined;
    dom.window.fetch = async () => { throw new Error("offline"); };
    expect((await dom.window.DATA.eisv()).data.series).toEqual([]);
  });

  it("inspects on demand with its own snapshot badge and preserves charts and selection on refresh", async () => {
    const { dom, calls, charts } = await section({ source: "snapshot", data: { points: [observation()] } });
    await click(dom, '[data-traj-id="agent-1"]');
    expect(calls).toHaveLength(1);
    expect(calls[0].options.includeTelemetry).toBeUndefined();
    expect(dom.window.document.querySelector("#eisv-trajectory .src-badge").textContent).toBe("snapshot");
    await click(dom, "#eisv-inspect-latest");
    expect(calls[1].options).toEqual({ mode: "recent", limit: 1, includeTelemetry: "latest" });
    const inspector = dom.window.document.getElementById("eisv-observation-inspector");
    expect(inspector.querySelector(".src-badge").textContent).toBe("snapshot");
    expect(inspector.textContent).toContain("5m ago");
    expect(inspector.textContent).toContain("Input timestamps are unrecorded");
    expect(inspector.textContent).toContain("tool outcomes");
    expect(inspector.textContent).toContain("No calibration sample");
    expect(inspector.textContent).toContain("lifetime");
    expect(inspector.textContent).toContain("per_bin_recent_activity");
    expect(inspector.textContent).toContain("Recent activity does not imply all supporting samples are recent.");
    expect(inspector.textContent).toContain("continuity (adjustment)");
    expect(inspector.textContent).toContain("Calibration evidence");
    expect(inspector.textContent).toContain("agent candidate is not applied to policy");
    expect(inspector.textContent).toContain("recent");
    const contribution = Array.from(inspector.querySelectorAll("tr")).find((row) => row.textContent.includes("tool outcomes"));
    expect(contribution.cells[2].textContent).toBe("0.00");
    expect(contribution.cells[4].textContent).toBe("0.00");
    const missing = Array.from(inspector.querySelectorAll("tr")).find((row) => row.textContent.includes("unavailable calibration"));
    expect(missing.cells[2].textContent).toBe("—");
    expect(charts).toHaveLength(4);
    const selector = dom.window.document.getElementById("eisv-source-filter");
    await dom.window.EISV.load();
    expect(calls).toHaveLength(2);
    expect(charts).toHaveLength(4);
    expect(charts.every((chart) => !chart.destroyed)).toBe(true);
    expect(dom.window.document.getElementById("eisv-source-filter")).toBe(selector);
    expect(dom.window.document.getElementById("eisv-observation-inspector")).toBe(inspector);
    expect(dom.window.document.querySelector('#eisv-heatmap [data-traj-id="agent-1"]').style.outline).toContain("1.5px");
  });

  it("renders server-computed wall-clock trajectory context", async () => {
    const result = { source: "live", data: { points: [observation()], trajectoryContext: {
      observations: 8, points_returned: 5, elapsed_seconds: 72000,
      cadence: { median_seconds: 3600, max_seconds: 28800 },
      transitions: { measurement_source: 2, maturity_phase: 1 },
      slopes_per_hour: { E: 0.01, I: -0.02, S: 0, V: null },
    } } };
    const { dom } = await section(result);
    await click(dom, '[data-traj-id="agent-1"]');
    const text = dom.window.document.getElementById("eisv-trajectory").textContent;
    expect(text).toContain("8 observations over 20h; 5 plotted");
    expect(text).toContain("Median cadence 1h; max gap 8h");
    expect(text).toContain("Descriptive slopes/hour: E 0.01 · I -0.02 · S 0.00 · V —");
    expect(text).toContain("not outcomes");
  });

  it("keeps fallback primary values distinct from latent behavioral inputs", async () => {
    const point = observation("ode_fallback");
    delete point.telemetry_envelope.derivation.missing_inputs;
    point.telemetry_envelope.derivation.components.dimensions.E.components[0].name = '<img src="bad">';
    point.telemetry_envelope.derivation.components.dimensions.E.components[0].value = NaN;
    const { dom } = await section({ source: "live", data: { points: [point] } });
    await click(dom, '[data-traj-id="agent-1"]');
    await click(dom, "#eisv-inspect-latest");
    const inspector = dom.window.document.getElementById("eisv-observation-inspector");
    expect(inspector.textContent).toContain("ode_fallback");
    expect(inspector.textContent).toContain("latent behavioral observations below do not explain the primary values");
    expect(inspector.textContent).toContain("Input coverage unrecorded");
    expect(inspector.textContent).not.toContain("No missing inputs reported");
    expect(inspector.textContent).not.toContain("NaN");
    expect(inspector.querySelector("img")).toBeNull();
  });

  it("does not substitute an older rich envelope for the latest legacy observation", async () => {
    const latest = { t: "2026-09-13T12:04:00Z", E: 0.8 };
    const { dom } = await section({ source: "snapshot", data: { points: [observation(), latest] } });
    await click(dom, '[data-traj-id="agent-1"]');
    await click(dom, "#eisv-inspect-latest");
    const text = dom.window.document.getElementById("eisv-observation-inspector").textContent;
    expect(text).toContain("No derivation envelope is available for this latest state observation");
    expect(text).toContain("1m ago");
    expect(text).not.toContain("tool outcomes");
  });

  it("uses server order to resolve equal-time latest observations", async () => {
    const older = { t: "2026-09-13T12:00:00Z", E: 0.8 };
    const newest = observation();
    const { dom } = await section({ source: "live", data: { points: [older, newest] } });
    await click(dom, '[data-traj-id="agent-1"]');
    await click(dom, "#eisv-inspect-latest");
    expect(dom.window.document.getElementById("eisv-observation-inspector").textContent)
      .toContain("tool outcomes");
  });

  it("drops a late inspector response after selecting another agent", async () => {
    let resolveInspector;
    const { dom } = await section((_id, options) => options.includeTelemetry
      ? new Promise((resolve) => { resolveInspector = resolve; })
      : { source: "live", data: { points: [observation()] } });
    await click(dom, '[data-traj-id="agent-1"]');
    await click(dom, "#eisv-inspect-latest");
    await click(dom, '[data-traj-id="agent-2"]');
    resolveInspector({ source: "live", data: { points: [observation()] } });
    await new Promise((resolve) => dom.window.setTimeout(resolve, 0));
    expect(dom.window.document.getElementById("eisv-trajectory").textContent).toContain("Agent 2");
    expect(dom.window.document.getElementById("eisv-observation-inspector").textContent).toBe("");
  });
});
