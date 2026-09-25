import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

/*
 * dialectic(action="list") now sends a ~280-char topic preview with
 * topic_truncated/topic_chars (src/mcp_handlers/dialectic/handlers.py,
 * _preview_topic): 50 untruncated topics made the default list 173 KB,
 * measured 2026-09-25. The card shows the preview; a "full topic" expander
 * fetches the text from dialectic(action="get"), which still returns it whole.
 */

const dataSource = readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8");
const dialecticSource = readFileSync(
  new URL("../redesign/sections/dialectic.js", import.meta.url),
  "utf8",
);

const FULL = "x".repeat(600);
const PREVIEW = "x".repeat(280) + "…";

const LIST = {
  success: true,
  session_count: 2,
  sessions: [
    { session_id: "long-one", phase: "resolved", topic: PREVIEW, topic_truncated: true,
      topic_chars: FULL.length, message_count: 0, created: "2026-09-25T00:00:00Z" },
    { session_id: "short-one", phase: "resolved", topic: "short topic",
      message_count: 0, created: "2026-09-25T00:00:00Z" },
  ],
};

function makeDom() {
  const dom = new JSDOM(`<div id="dlc-mount"></div>`, {
    runScripts: "outside-only",
    url: "https://governance.test/",
  });
  const calls = [];
  dom.window.fetch = async (path, opts) => {
    if (path !== "/v1/tools/call") return { ok: false, status: 404 };
    const req = JSON.parse(opts.body);
    calls.push(req);
    const result = req.arguments.action === "get"
      ? { success: true, topic: FULL, transcript: [] }
      : LIST;
    return { ok: true, status: 200, json: async () => ({ result }) };
  };
  dom.window.eval(dataSource);
  dom.window.eval(dialecticSource);
  return { dom, calls };
}

describe("dialectic list topic preview", () => {
  it("renders the preview and offers the full topic only for truncated rows", async () => {
    const { dom } = makeDom();
    await dom.window.Dialectic.load();
    const doc = dom.window.document;
    const expanders = doc.querySelectorAll(".dlc-topic");
    expanders.forEach((d) => expect(d.dataset.sid).toBe("long-one"));
    expect(expanders.length).toBe(1);
    expect(doc.querySelector("#dlc-mount").textContent).toContain(PREVIEW);
  });

  it("fetches the full topic from get on first expand", async () => {
    const { dom, calls } = makeDom();
    await dom.window.Dialectic.load();
    const d = dom.window.document.querySelector(".dlc-topic");
    d.open = true;
    d.dispatchEvent(new dom.window.Event("toggle"));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.some((c) => c.arguments.action === "get" && c.arguments.session_id === "long-one")).toBe(true);
    expect(d.querySelector(".dlc-topic-body").textContent).toBe(FULL);
  });
});
