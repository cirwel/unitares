import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const activitySource = readFileSync(
  new URL("../redesign/sections/activity.js", import.meta.url),
  "utf8",
);
const dataSource = readFileSync(
  new URL("../redesign/data.js", import.meta.url),
  "utf8",
);

describe("activity dual-log view", () => {
  it("loads the operational read model through the shared data seam", async () => {
    const dom = new JSDOM("", {
      runScripts: "outside-only",
      url: "https://governance.test/#activity",
    });
    const payloads = {
      "/api/events?limit=40": { events: [] },
      "/api/activity?window=60&bucket=5": { buckets: [], window_minutes: 60, bucket_minutes: 5 },
      "/v1/runtime/activity?window_hours=24&limit=1000": {
        success: true,
        window_hours: 24,
        summary: { processes: 1 },
        processes: [{ process_id: "agent:slot" }],
        semantics: { operational: "never EISV" },
      },
    };
    dom.window.SNAPSHOT = { activity: {} };
    dom.window.fetch = async (path) => ({
      ok: true,
      status: 200,
      json: async () => payloads[path],
    });

    dom.window.eval(dataSource);
    const result = await dom.window.DATA.activity();

    expect(result.source).toBe("live");
    expect(result.data.operational.available).toBe(true);
    expect(result.data.operational.source).toBe("live");
    expect(result.data.operational.summary.processes).toBe(1);
    expect(result.data.operational.processes[0].process_id).toBe("agent:slot");
  });

  it("keeps the existing activity stream live when runtime evidence is unavailable", async () => {
    const dom = new JSDOM("", {
      runScripts: "outside-only",
      url: "https://governance.test/#activity",
    });
    dom.window.SNAPSHOT = { activity: {} };
    dom.window.fetch = async (path) => {
      if (path.startsWith("/v1/runtime/activity")) {
        return { ok: false, status: 503, json: async () => ({}) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => path.startsWith("/api/events")
          ? { events: [] }
          : { buckets: [], window_minutes: 60, bucket_minutes: 5 },
      };
    };

    dom.window.eval(dataSource);
    const result = await dom.window.DATA.activity();

    expect(result.source).toBe("live");
    expect(result.data.operational.available).toBe(false);
    expect(result.data.operational.source).toBe("unavailable");
  });

  it("keeps host observations distinct from agent-authored check-ins", async () => {
    const dom = new JSDOM('<div id="act-mount"></div>', {
      runScripts: "outside-only",
      url: "https://governance.test/#activity",
    });
    dom.window.DATA = {
      activity: async () => ({
        source: "live",
        data: {
          events: [],
          buckets: [{ p: 2, g: 1, x: 0 }],
          windowMin: 60,
          bucketMin: 5,
          operational: {
            available: true,
            source: "live",
            windowHours: 24,
            summary: {
              processes: 1,
              observed_slots: 1,
              agents: 1,
              recent_processes: 1,
              recent_tool_activity_slots: 1,
              recent_host_heartbeat_slots: 1,
              observations: 5,
              slots_without_agent_report: 1,
            },
            processes: [{
              agent_id: "86ae619f-87e0-4040-8f29-eacece0c7904",
              agent_label: "Codex runtime",
              slot_hash: "abcdef123456",
              host_family: "codex",
              execution_mode: "automation",
              execution_mode_source: "explicit_env",
              model: "gpt-5.4",
              latest_kind: "activity_rollup",
              operational_recent: true,
              tool_activity_recent: true,
              host_heartbeat_recent: true,
              host_process_alive: true,
              last_operational_at: new Date().toISOString(),
              last_tool_activity_at: new Date().toISOString(),
              last_host_observation_at: new Date().toISOString(),
              last_reflection_at: null,
              last_agent_report_at: null,
              last_interpretation_at: new Date().toISOString(),
              substrate_interpretation_count: 1,
              bootstrap_count: 1,
              state_update_profile: "substrate_only",
              operational_after_reflection: false,
              tool_activity_after_agent_report: false,
              host_observation_after_agent_report: false,
              tool_count: 42,
              tools_in_window: 7,
              restoration_capsule: {
                host_observation: { event_id: "8f4bb851-dfed-4e12-b5b9-33820df47274" },
                reflection: { context: { task_label: "weekly release notes", task_outcome: "drafted" } },
                continuity: {
                  relationship: "host_observation_only",
                  missing: [],
                  restore_basis: "host_observation_only",
                },
              },
            }],
          },
        },
      }),
    };

    dom.window.eval(activitySource);
    await dom.window.Activity.load();

    const text = dom.window.document.getElementById("act-mount").textContent;
    expect(text).toContain("Host evidence and check-ins");
    expect(text).toContain("never agent runtime or EISV");
    expect(text).toContain("no agent-authored check-in");
    expect(text).toContain("1 automatic turn summary");
    expect(text).toContain("1 initialization row");
    expect(text).toContain("tool activity observed");
    expect(text).toContain("substrate only");
    expect(text).toContain("automation · explicit_env · gpt-5.4");
    expect(text).toContain("Restoration capsule");
    expect(text).toContain("weekly release notes · drafted");
    expect(text).toContain("audit 8f4bb851");
    expect(text).toContain("Governance state updates");
    expect(text).toContain("provenance varies");
    expect(text).toContain("host observations excluded");
    expect(text).not.toContain("process is active");
  });
});
