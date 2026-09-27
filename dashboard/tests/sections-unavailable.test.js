import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Every core section on a served page whose server is not answering (the
// deployment-extension sections carry their own copy of this spec). The bundled
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

describe("what a failed read is called", () => {
  async function sourceOf(url) {
    const dom = new JSDOM("<div></div>", { runScripts: "outside-only", url });
    dom.window.fetch = async () => { throw new TypeError("Failed to fetch"); };
    dom.window.SNAPSHOT = { dialectic: { sessions: [], counts: {} } };
    dom.window.eval(dataSource);
    return (await dom.window.DATA.dialectic()).source;
  }

  it("is unavailable on a served page — the producer did not answer", async () => {
    expect(await sourceOf("https://gov.example/dashboard")).toBe("unavailable");
  });

  it("is snapshot only where the bundled snapshot backs it", async () => {
    expect(await sourceOf("file:///tmp/app.html")).toBe("snapshot");
    expect(await sourceOf("https://gov.example/dashboard?snapshot=1")).toBe("snapshot");
  });

  for (const [global, file, mountId] of [["Discoveries", "discoveries", "dsc-mount"], ["Dialectic", "dialectic", "dlc-mount"]]) {
    it(`${global} says the server is not answering, not that nothing matched`, async () => {
      const dom = boot(mountId);
      dom.window.eval(read(`../redesign/sections/${file}.js`));
      await dom.window[global].load();
      const text = dom.window.document.getElementById(mountId).textContent;
      expect(text).toContain("Server not answering");
      expect(text).toContain("unavailable");
      expect(text).not.toContain("No matches");
    });
  }
});
