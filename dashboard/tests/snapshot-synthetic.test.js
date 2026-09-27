import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// snapshot.js is the offline fixture (a page opened from a file, or
// ?snapshot=1). Until 2026-09-27 it was a real capture of one deployment's
// fleet; it is synthetic now. These pin that it stays synthetic, covers every
// key data.js reads, and still drives every core section offline.

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const dataSource = read("../redesign/data.js");
const snapshotSource = read("../redesign/snapshot.js");

function loadSnapshot() {
  const dom = new JSDOM("", { runScripts: "outside-only" });
  dom.window.eval(snapshotSource);
  return dom.window.SNAPSHOT;
}

describe("offline snapshot", () => {
  it("declares itself synthetic", () => {
    expect(loadSnapshot().synthetic).toBe(true);
    expect(snapshotSource).toMatch(/SYNTHETIC offline fixture/);
  });

  it("covers every top-level key data.js falls back to", () => {
    const S = loadSnapshot();
    const keys = new Set([...dataSource.matchAll(/S\(\)\.([a-zA-Z]+)/g)].map((m) => m[1]));
    expect(keys.size).toBeGreaterThan(10);
    for (const k of keys) expect(S[k], k).toBeDefined();
  });

  const SECTIONS = [
    ["Agents", "agents", "ag-mount"], ["Discoveries", "discoveries", "dsc-mount"],
    ["Dialectic", "dialectic", "dlc-mount"], ["Activity", "activity", "act-mount"],
    ["EISV", "eisv", "eisv-mount"], ["Risk", "risk", "risk-mount"],
  ];
  for (const [global, file, mountId] of SECTIONS) {
    it(`${global} renders from it with no server`, async () => {
      const dom = new JSDOM(`<div id="${mountId}"></div>`, { runScripts: "outside-only", url: "file:///tmp/app.html" });
      dom.window.fetch = async () => { throw new TypeError("Failed to fetch"); };
      dom.window.WebSocket = class { constructor() { throw new Error("no socket"); } };
      dom.window.Chart = class { constructor() { this.data = { datasets: [] }; this.options = {}; } update() {} destroy() {} };
      dom.window.HTMLCanvasElement.prototype.getContext = () => ({});
      dom.window.eval(snapshotSource);
      dom.window.eval(dataSource);
      dom.window.eval(read(`../redesign/sections/${file}.js`));
      await dom.window[global].load();
      const text = dom.window.document.getElementById(mountId).textContent;
      expect(text.length).toBeGreaterThan(0);
      expect(text).not.toMatch(/Server not answering/);
    });
  }
});
