import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The knowledge-flow panel inside the Discoveries pane: builder ← source agent
// pairs from /v1/knowledge/flow. Labels come from the server; an agent without
// one is shown by id prefix, never by a name the dashboard supplies.

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const dataSource = read("../redesign/data.js");
const sectionSource = read("../redesign/sections/knowledge-flow.js");
const appHtml = read("../redesign/app.html");

function boot(body, status = 200) {
  const dom = new JSDOM('<div id="kflow-mount"></div>', {
    runScripts: "outside-only",
    url: "https://gov.example/dashboard",
  });
  const calls = [];
  dom.window.fetch = async (url) => {
    calls.push(String(url));
    return { ok: status >= 200 && status < 300, status, json: async () => body, headers: { get: () => "application/json" } };
  };
  dom.window.eval(dataSource);
  dom.window.eval(sectionSource);
  return { dom, calls };
}

describe("knowledge flow panel", () => {
  it("renders live flows with server labels and id fallbacks", async () => {
    const { dom, calls } = boot({
      success: true, graph_available: true, limit: 25,
      flows: [
        { builder: "aaaaaaaa-1111", source: "bbbbbbbb-2222", links: 9, builder_label: "writer-a", source_label: null },
      ],
    });
    await dom.window.KnowledgeFlow.load();
    const text = dom.window.document.getElementById("kflow-mount").textContent;
    expect(calls.some((u) => u.includes("/v1/knowledge/flow?limit=25"))).toBe(true);
    expect(text).toContain("writer-a");
    expect(text).toContain("bbbbbbbb");
    expect(text).toContain("9");
    expect(text).toContain("live");
  });

  it("says when the server has no graph backend", async () => {
    const { dom } = boot({ success: true, graph_available: false, limit: 25, flows: [] });
    await dom.window.KnowledgeFlow.load();
    expect(dom.window.document.getElementById("kflow-mount").textContent).toContain("No graph backend");
  });

  it("shows a failed graph probe as an error, not as no graph backend", async () => {
    // Regression (#2946 review): the route answers a probe failure with 503.
    const { dom } = boot({ success: false, error: "knowledge graph unavailable" }, 503);
    await dom.window.KnowledgeFlow.load();
    const text = dom.window.document.getElementById("kflow-mount").textContent;
    expect(text).toContain("Knowledge flow unavailable");
    expect(text).toContain("unavailable");
    expect(text).not.toContain("No graph backend");
    expect(text).not.toContain("No finding links");
  });

  it("says an empty graph has no cross-agent links", async () => {
    const { dom } = boot({ success: true, graph_available: true, limit: 25, flows: [] });
    await dom.window.KnowledgeFlow.load();
    expect(dom.window.document.getElementById("kflow-mount").textContent).toContain("No finding links across agents yet");
  });

  it("is wired into the Discoveries pane", () => {
    expect(appHtml).toContain('<div id="kflow-mount"></div>');
    expect(appHtml).toContain('<script src="./sections/knowledge-flow.js"></script>');
    expect(appHtml).toContain("window.KnowledgeFlow.load()");
  });
});
