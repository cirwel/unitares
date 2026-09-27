import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Every section on a served page whose server is not answering. The bundled
// snapshot does not back the fallback there (see data.js `S`), so each data
// accessor degrades to an empty fallback and each section must still render,
// not reject and leave its pane blank. Before, several fallbacks dereferenced
// nested snapshot fields (`S().dialectic.sessions`, `S().metrics.catalog`), a
// path only a bearer-only operator used to reach.

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const dataSource = read("../redesign/data.js");

const SECTIONS = [
  ["Agents", "agents", "ag-mount"],
  ["Discoveries", "discoveries", "dsc-mount"],
  ["Dialectic", "dialectic", "dlc-mount"],
  ["Activity", "activity", "act-mount"],
  ["EISV", "eisv", "eisv-mount"],
  ["Risk", "risk", "risk-mount"],
  ["TelemetryHealth", "telemetry-health", "telemetry-health-mount"],
  ["Metrics", "metrics", "met-mount"],
  ["Residents", "residents", "res-mount"],
  ["Automations", "automations", "auto-mount"],
  ["Adjudication", "adjudication", "adj-mount"],
  ["Enforcement", "enforcement", "enforcement-mount"],
  ["Security", "security", "security-mount"],
];

function boot(mountId) {
  const dom = new JSDOM(`<div id="${mountId}"></div>`, {
    runScripts: "outside-only",
    url: "https://gov.example/dashboard",
  });
  // The server is not answering: every request fails the way a restart does.
  dom.window.fetch = async () => { throw new TypeError("Failed to fetch"); };
  dom.window.WebSocket = class { constructor() { throw new Error("no socket"); } };
  class FakeChart { constructor() { this.data = { datasets: [] }; this.options = {}; } update() {} destroy() {} }
  dom.window.Chart = FakeChart;
  // The bundle loaded, as it does for a loopback caller — it must still not be used.
  dom.window.SNAPSHOT = { dialectic: { sessions: [{ id: "bundled" }], counts: {} }, metrics: { catalog: [{ name: "bundled" }], series: {} } };
  dom.window.eval(dataSource);
  return dom;
}

describe("sections on a served page whose server is not answering", () => {
  for (const [global, file, mountId] of SECTIONS) {
    it(`${global} renders instead of rejecting`, async () => {
      const dom = boot(mountId);
      dom.window.eval(read(`../redesign/sections/${file}.js`));
      const mod = dom.window[global];
      expect(mod && typeof mod.load).toBe("function");
      await expect(Promise.resolve().then(() => mod.load())).resolves.not.toThrow();
      const mount = dom.window.document.getElementById(mountId);
      expect(mount.textContent).not.toContain("bundled");
    });
  }
});
