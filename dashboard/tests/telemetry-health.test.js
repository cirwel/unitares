import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const dataSource = readFileSync(
  new URL("../redesign/data.js", import.meta.url),
  "utf8",
);
const sectionSource = readFileSync(
  new URL("../redesign/sections/telemetry-health.js", import.meta.url),
  "utf8",
);
const appSource = readFileSync(
  new URL("../redesign/app.html", import.meta.url),
  "utf8",
);

function report(windowDays = 30, envelopes = 80) {
  return {
    success: true,
    schema: "eisv.telemetry-health.v1",
    window_days: windowDays,
    summary: {
      states: 100, agents: 10, envelope_rows: envelopes, envelopes, envelope_agents: envelopes ? 8 : 0,
      coverage_rate: envelopes / 100, agent_coverage_rate: envelopes ? 0.8 : 0,
      invalid_envelopes: 0, invalid_envelope_rate: envelopes ? 0 : null,
      first_envelope_at: envelopes ? "2026-08-01T00:00:00Z" : null,
      behavioral_primary: envelopes ? 50 : 0,
      behavioral_primary_rate: envelopes ? 0.625 : null,
      ode_fallback: envelopes ? 30 : 0,
      ode_fallback_rate: envelopes ? 0.375 : null,
      measurement_ready: envelopes ? 50 : 0,
      measurement_ready_rate: envelopes ? 0.625 : null,
      maturity_eligible: envelopes ? 3 : 0,
      maturity_eligible_rate: envelopes ? 0.0375 : null,
      maturity_would_defer: envelopes ? 2 : 0,
      maturity_would_defer_rate: envelopes ? 0.025 : null,
      maturity_confirmed: envelopes ? 1 : 0,
      maturity_actuation_enabled: 0,
      maturity_actuation_ready: 0,
      maturity_actuation_applied: 0,
      missing: envelopes ? 10 : 0, missing_rate: envelopes ? 0.125 : null,
      contract_violation_rows: envelopes ? 1 : 0,
      contract_checked_rows: envelopes, contract_violation_rate: envelopes ? 0.0125 : null,
      enforcement_requested: envelopes ? 4 : 0,
      enforcement_applied: envelopes ? 1 : 0,
      enforcement_delivered: envelopes ? 1 : 0,
      enforcement_delivery_rate: envelopes ? 0.25 : null,
    },
    timeline: [
      { day: "2026-08-08", states: 20, envelopes: envelopes ? 10 : 0, coverage_rate: envelopes ? 0.5 : 0 },
      { day: "2026-08-09", states: 20, envelopes: envelopes ? 16 : 0, coverage_rate: envelopes ? 0.8 : 0 },
    ],
    primary_sources: envelopes ? [
      { source: "behavioral", observations: 50, rate: 0.625 },
      { source: "ode_fallback", observations: 30, rate: 0.375 },
    ] : [],
    measurement_sources: envelopes ? [
      { source: "behavioral_sensor", observations: 42, rate: 0.525 },
      { source: "physical", observations: 8, rate: 0.1 },
      { source: "ode_fallback", observations: 30, rate: 0.375 },
    ] : [],
    warmup: envelopes ? [
      { phase: "baselined", observations: 60, rate: 0.75 },
      { phase: "warming", observations: 20, rate: 0.25 },
    ] : [],
    missing_inputs: envelopes ? [
      { input: "outcome_history", observations: 10, rate: 0.125 },
    ] : [],
    contract_checks: {
      checked_rows: envelopes,
      violation_rows: envelopes ? 1 : 0,
      violations: envelopes ? 1 : 0,
      by_type: envelopes ? [{ type: "policy_risk_mismatch", observations: 1 }] : [],
      note: "Same-row serialization invariants only.",
    },
    maturity_gate: {
      strata: envelopes ? [
        { outcome: "ineligible", observations: 77 },
        { outcome: "shadow_would_defer", observations: 2 },
        { outcome: "shadow_confirmed", observations: 1 },
      ] : [],
      ineligibility_reasons: envelopes ? [
        { reason: "policy_not_risk_pause", observations: 77 },
        { reason: "none", observations: 3 },
      ] : [],
      reset_reasons: envelopes ? [
        { reason: "policy_not_risk_pause", observations: 77 },
        { reason: "first_identity_observation", observations: 2 },
        { reason: "none", observations: 1 },
      ] : [],
      note: "Shadow-only confirmation maturity; no pause was suppressed.",
    },
    enforcement: {
      strata: envelopes ? [
        { stratum: "not_requested", observations: 76 },
        { stratum: "requested_not_applied", observations: 3 },
        { stratum: "applied", observations: 1 },
      ] : [],
      bases: envelopes ? [
        { basis: "advisory_policy", observations: 76 },
        { basis: "phi_cold_start_unconfirmed_shadow", observations: 2 },
        { basis: "phi_cold_start_confirmed", observations: 1 },
        { basis: "non_cold_start_policy", observations: 1 },
      ] : [],
      note: "Intervention-conditioned delivery counts; not causal.",
    },
    risk_vocabularies: [
      { surface: "behavioral_verdict", bands: [
        { label: "safe", minimum: 0, maximum_exclusive: 0.35 },
        { label: "caution", minimum: 0.35, maximum_exclusive: 0.6 },
      ] },
      { surface: "experience_summary", bands: [
        { label: "low", minimum: 0, maximum_exclusive: 0.4 },
        { label: "elevated", minimum: 0.4, maximum_exclusive: 0.7 },
      ] },
      { surface: "health_status", bands: [
        { label: "healthy", minimum: 0, maximum_exclusive: 0.45 },
        { label: "moderate", minimum: 0.45, maximum_exclusive: 0.7 },
      ] },
    ],
    calibration: {
      status: envelopes ? "inconclusive" : "awaiting_envelope",
      strict_outcomes: 14, fixtures_excluded: 2, with_prior_state: 12,
      with_envelope: envelopes ? 8 : 0, clusters: envelopes ? 7 : 0,
      bad_clusters: envelopes ? 2 : 0,
      bins: [
        { band: "0.0-0.2", outcomes: 4, clusters: 4, bad_clusters: 0, bad_cluster_rate: 0, evidence_status: "sparse" },
        { band: "0.2-0.4", outcomes: 4, clusters: 3, bad_clusters: 2, bad_cluster_rate: 2 / 3, evidence_status: "sparse" },
      ],
      note: "Strict external outcomes only; descriptive and clustered.",
    },
  };
}

describe("EISV telemetry health dashboard", () => {
  it("loads the durable report through the shared data seam", async () => {
    const dom = new JSDOM("", {
      runScripts: "outside-only",
      url: "https://governance.test/#telemetry-health",
    });
    let requested = null;
    dom.window.SNAPSHOT = { eisvTelemetryHealth: report() };
    dom.window.fetch = async (path) => {
      requested = path;
      return { ok: true, status: 200, json: async () => report(90) };
    };
    dom.window.eval(dataSource);

    const result = await dom.window.DATA.eisvTelemetryHealth(90);

    expect(result.source).toBe("live");
    expect(result.data.window_days).toBe(90);
    expect(requested).toBe("/v1/eisv/telemetry-health?days=90");
  });

  it("renders rollout, contract, calibration, and actuator evidence in place", async () => {
    const dom = new JSDOM('<div id="telemetry-health-mount"></div>', {
      runScripts: "outside-only",
      url: "https://governance.test/#telemetry-health",
    });
    const calls = [];
    dom.window.DATA = {
      eisvTelemetryHealth: async (days) => {
        calls.push(days);
        return { source: "live", data: report(days) };
      },
    };
    const charts = [];
    dom.window.Chart = class FakeChart {
      constructor(_canvas, config) {
        this.data = config.data;
        this.options = config.options;
        charts.push(this);
      }
      destroy() {}
      update() {}
    };

    dom.window.eval(sectionSource);
    await dom.window.TelemetryHealth.load();

    const mount = dom.window.document.getElementById("telemetry-health-mount");
    const selector = dom.window.document.getElementById("telemetry-health-days");
    expect(mount.textContent).toContain("Instrumentation, not judgment");
    expect(mount.textContent).toContain("policy_risk_mismatch");
    expect(mount.textContent).toContain("One risk number, three vocabularies");
    expect(mount.textContent).toContain("Strict external outcomes only");
    expect(mount.textContent).toContain("Cold-start decision maturity");
    expect(mount.textContent).toContain("shadow_would_defer");
    expect(mount.textContent).toContain("phi_cold_start_unconfirmed_shadow");
    expect(mount.textContent).toContain("no pause was suppressed");
    expect(mount.textContent).toContain("of requests applied; not causal effect");
    expect(mount.textContent).toContain("do not score agents or establish machine experience");
    expect(charts).toHaveLength(2);
    expect(charts[0].data.datasets[0].data).toEqual([50, 80]);
    expect(charts[1].data.datasets[0].data).toEqual([0, (2 / 3) * 100]);

    selector.value = "7";
    selector.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await new Promise((resolve) => dom.window.setTimeout(resolve, 0));
    expect(calls).toEqual([30, 7]);
    expect(dom.window.document.getElementById("telemetry-health-days")).toBe(selector);
    expect(selector.value).toBe("7");
  });

  it("shows a live zero-envelope cohort without borrowing snapshot values", async () => {
    const dom = new JSDOM('<div id="telemetry-health-mount"></div>', {
      runScripts: "outside-only",
      url: "https://governance.test/#telemetry-health",
    });
    dom.window.DATA = {
      eisvTelemetryHealth: async () => ({ source: "live", data: report(30, 0) }),
    };
    dom.window.Chart = class FakeChart {
      constructor(_canvas, config) { this.data = config.data; }
      destroy() {}
      update() {}
    };
    dom.window.eval(sectionSource);

    await dom.window.TelemetryHealth.load();

    const text = dom.window.document.getElementById("telemetry-health-mount").textContent;
    expect(text).toContain("Awaiting envelope rollout: 0 of 100 measured state rows");
    expect(text).toContain("No backfill is inferred");
    expect(text).not.toContain("80 / 100");
  });

  it("is wired as a lazy dashboard section with theme refresh", () => {
    expect(appSource).toContain('data-section="telemetry-health"');
    expect(appSource).toContain('data-pane="telemetry-health"');
    expect(appSource).toContain('src="./sections/telemetry-health.js"');
    expect(appSource).toContain('id === "telemetry-health" && window.TelemetryHealth');
    expect(appSource).toContain("window.TelemetryHealth.retheme()");
  });

  it("is NOT on the 10s auto-refresh tick, and has a manual control instead", () => {
    // The endpoint scans a 30-DAY cohort and buckets by DAY, so a 10s tick can
    // show nothing new — while its 30s cache means every third tick pays the
    // recompute: 2.6ms on a cache hit, 1.64s on a miss (measured 2026-08-28).
    // That was a 1.6s database scan every 30 seconds for as long as the tab
    // stayed open, buying zero fresh data. Same reasoning app.html already
    // applies to Metrics, Automations and Risk.
    expect(appSource).not.toContain('"telemetry-health": () => window.TelemetryHealth');

    // Removing the tick without a manual control would have stranded the
    // operator: this section shipped with no refresh affordance at all, so
    // re-navigating was the only way to update it.
    expect(sectionSource).toContain('id="telemetry-health-refresh"');
    expect(sectionSource).toContain('thRefresh.addEventListener("click"');
  });
});
