import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The Timeline tab (sections/timeline.js) over a /v1/timeline response:
// rows bounded and ordered by recency, the filter, the detail panel, the
// empty and unavailable states, and the app.html wiring.

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const sectionSource = read("../redesign/sections/timeline.js");
const dataSource = read("../redesign/data.js");

const END = Date.parse("2026-10-23T18:00:00Z");
const iso = (minAgo) => new Date(END - minAgo * 60e3).toISOString();
const uid = (i) => `${String(i).padStart(8, "0")}-0000-4000-8000-000000000000`;

function fixture(nAgents = 30) {
  const agents = [];
  const events = [];
  for (let i = 0; i < nAgents; i++) {
    agents.push({ agent_id: uid(i), label: `agent-${i}`, model_type: null, trust_tier: "emerging", parent_agent_id: null });
    // agent-0 is the most recent, agent-(n-1) the oldest.
    events.push({ kind: "checkin", ts: iso(10 + i * 5), agent_id: uid(i), authored_by: "agent", action: "approve", verdict: "safe", risk_score: 0.1 });
  }
  events.push({ kind: "checkin", ts: iso(5), agent_id: uid(0), authored_by: "agent", action: "guide", verdict: "safe", risk_score: 0.3, report_excerpt: "Ready to merge; tests green." });
  events.push({ kind: "checkin", ts: iso(4), agent_id: uid(0), authored_by: "substrate", action: "approve", verdict: "safe", risk_score: null });
  events.push({ kind: "outcome", ts: iso(3), agent_id: uid(0), outcome_id: "o-1", outcome_type: "test_failed", is_bad: true, reported_confidence: 0.9, verification_source: "external_signal" });
  events.push({ kind: "lifecycle", ts: iso(120), agent_id: uid(1), event_type: "lifecycle_paused", reason: "risk over threshold", event_id: "l-1" });
  events.push({ kind: "lifecycle", ts: iso(90), agent_id: uid(1), event_type: "lifecycle_resumed", reason: "self_recovery", event_id: "l-2" });
  events.push({ kind: "lineage", ts: iso(60), agent_id: uid(29), parent_agent_id: uid(0), successor_class: "ephemeral", event_id: "g-1" });
  if (nAgents === 30) Object.assign(agents[29], { spawn_reason: "subagent", parent_agent_id: uid(0) });
  events.sort((a, b) => a.ts.localeCompare(b.ts));
  return {
    hours: 6, window: { start: iso(360), end: iso(0), hours: 6 }, events, agents,
    truncated: [], counts: { checkin: nAgents + 2, outcome: 1, dialectic: 0 }, limitPerKind: 500,
  };
}

async function mount(response, source = "live") {
  const dom = new JSDOM(`<div id="tl-mount"></div>`, { runScripts: "outside-only", url: "https://gov.example/dashboard" });
  const calls = [];
  dom.window.DATA = { timeline: async (opts) => { calls.push(opts); return { source, data: response }; } };
  dom.window.eval(sectionSource);
  await dom.window.Timeline.load();
  const doc = dom.window.document;
  const rows = () => [...doc.querySelectorAll(".tl-row:not(.tl-axis)")];
  const names = () => rows().map((r) => r.querySelector(".tl-name").textContent);
  return { dom, doc, calls, rows, names, text: () => doc.getElementById("tl-mount").textContent };
}

describe("Timeline tab", () => {
  it("shows the 25 most recent agents first, then all on request", async () => {
    const t = await mount(fixture(30));
    // agent-29 is nested under agent-0 as its subagent, so it follows it.
    expect(t.names().slice(0, 3)).toEqual(["agent-0", "agent-29", "agent-1"]);
    expect(t.rows()).toHaveLength(25);
    const all = t.doc.getElementById("tl-all");
    expect(all.textContent).toContain("show all 30 agents");
    all.click();
    expect(t.rows()).toHaveLength(30);
  });

  it("nests a subagent under its parent and says so", async () => {
    const t = await mount(fixture(30));
    const child = t.rows()[1];
    expect(child.classList.contains("child")).toBe(true);
    expect(child.querySelector(".tl-sub").textContent).toContain("subagent of agent-0");
    expect(t.rows()[0].querySelector(".tl-mk.lineage")).not.toBeNull();
  });

  it("labels a succession as a successor, not a subagent", async () => {
    const r = fixture(30);
    r.agents[29].spawn_reason = "explicit";
    const t = await mount(r);
    const child = t.rows()[1];
    expect(child.querySelector(".tl-sub").textContent).toContain("successor of agent-0");
    expect(child.querySelector(".tl-sub").textContent).not.toContain("subagent");
    t.rows()[0].querySelector(".tl-mk.lineage").click();
    const detail = t.doc.getElementById("tl-detail").textContent;
    expect(detail).toContain("Handed off to a successor");
    expect(detail).toContain("succession (handoff)");
  });

  it("does not call an unregistered spawn reason a subagent", async () => {
    const r = fixture(30);
    r.agents[29].spawn_reason = null;
    const t = await mount(r);
    expect(t.rows()[1].querySelector(".tl-sub").textContent).toContain("declared lineage from agent-0");
  });

  it("draws lineage marks without stray text", async () => {
    const t = await mount(fixture(30));
    expect(t.rows()[0].querySelector(".tl-mk.lineage").textContent).toBe("");
  });

  it("keeps a recently active subagent of a quiet parent inside the row cap", async () => {
    const r = fixture(30);
    // agent-28 is the 29th most recent root; give it a subagent busier than anyone.
    r.agents.push({ agent_id: uid(99), label: "busy-child", spawn_reason: "subagent", parent_agent_id: uid(28) });
    r.events.push({ kind: "checkin", ts: iso(1), agent_id: uid(99), authored_by: "agent", action: "approve" });
    const t = await mount(r);
    expect(t.names().slice(0, 2)).toEqual(["agent-28", "busy-child"]);
  });

  it("filters rows by label or id", async () => {
    const t = await mount(fixture(30));
    const input = t.doc.getElementById("tl-filter");
    input.value = "agent-2";
    input.dispatchEvent(new t.dom.window.Event("input"));
    expect(t.names().every((n) => n.includes("agent-2"))).toBe(true);
    expect(t.names()).toContain("agent-21");
    input.value = uid(7).slice(0, 8);
    input.dispatchEvent(new t.dom.window.Event("input"));
    expect(t.names()).toEqual(["agent-7"]);
    input.value = "nobody";
    input.dispatchEvent(new t.dom.window.Event("input"));
    expect(t.text()).toContain("No agent matches");
  });

  it("draws marks by kind and a pause band from the paused/resumed pair", async () => {
    const t = await mount(fixture(30));
    const first = t.rows()[0];
    expect(first.querySelector(".tl-mk.checkin.guide")).not.toBeNull();
    expect(first.querySelector(".tl-mk.checkin.substrate")).not.toBeNull();
    expect(first.querySelector(".tl-mk.outcome.bad").textContent).toBe("✕");
    const paused = t.rows().find((r) => r.querySelector(".tl-name").textContent === "agent-1");
    expect(paused.querySelector(".tl-band.pause")).not.toBeNull();
  });

  it("shows a check-in's report excerpt, or says it was not retained", async () => {
    const t = await mount(fixture(30));
    const detail = () => t.doc.getElementById("tl-detail").textContent;
    t.rows()[0].querySelector(".tl-mk.checkin.guide").click();
    expect(detail()).toContain("Ready to merge; tests green.");
    expect(detail()).toContain("UTC");
    t.rows()[0].querySelector(".tl-mk.checkin.proceed").click();
    expect(detail()).toContain("Report text not retained for this check-in.");
    t.rows()[0].querySelector(".tl-mk.checkin.substrate").click();
    expect(detail()).toContain("A hook, not the agent");
  });

  it("shows an outcome's reported confidence against its result", async () => {
    const t = await mount(fixture(30));
    t.rows()[0].querySelector(".tl-mk.outcome").click();
    const detail = t.doc.getElementById("tl-detail").textContent;
    expect(detail).toContain("0.90");
    expect(detail).toContain("failed");
  });

  it("names the kinds the server truncated", async () => {
    const r = fixture(3);
    r.truncated = ["checkin"];
    const t = await mount(r);
    expect(t.text()).toContain("checkin");
    expect(t.text()).toContain("Only the newest 500 rows");
  });

  it("renders an empty window as live and empty", async () => {
    const t = await mount({ hours: 6, window: null, events: [], agents: [], truncated: [], counts: {} });
    expect(t.text()).toContain("No agent events in the last 6h");
    expect(t.doc.getElementById("tl-src").textContent).toBe("live");
  });

  it("says the server is not answering when the source is unavailable", async () => {
    const t = await mount({ hours: 6, window: null, events: [], agents: [], truncated: [], counts: {} }, "unavailable");
    expect(t.text()).toContain("Server not answering");
    expect(t.text()).not.toContain("No agent events");
  });

  it("refetches on the live tick for short windows only", async () => {
    const t = await mount(fixture(3));
    await t.dom.window.Timeline.refresh();
    expect(t.calls).toHaveLength(2);
    t.doc.querySelector('.tl-win[data-h="24"]').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(t.calls.at(-1).hours).toBe(24);
    const n = t.calls.length;
    await t.dom.window.Timeline.refresh();
    expect(t.calls).toHaveLength(n);
  });

  it("renders only the newest request when windows are switched quickly", async () => {
    const dom = new JSDOM(`<div id="tl-mount"></div>`, { runScripts: "outside-only", url: "https://gov.example/dashboard" });
    const pending = [];
    dom.window.DATA = { timeline: (opts) => new Promise((resolve) => pending.push({ opts, resolve })) };
    dom.window.eval(sectionSource);
    const first = dom.window.Timeline.load();
    pending[0].resolve({ source: "live", data: fixture(3) });
    await first;
    const doc = dom.window.document;
    doc.querySelector('.tl-win[data-h="1"]').click();
    doc.querySelector('.tl-win[data-h="24"]').click();
    const [one, day] = pending.slice(1);
    day.resolve({ source: "live", data: Object.assign(fixture(3), { hours: 24 }) });
    await new Promise((r) => setTimeout(r, 0));
    one.resolve({ source: "live", data: Object.assign(fixture(3), { hours: 1 }) });
    await new Promise((r) => setTimeout(r, 0));
    expect(doc.getElementById("tl-counts").textContent).toContain("Last 24h");
    expect(doc.querySelector('.tl-win[aria-pressed="true"]').dataset.h).toBe("24");
  });

  it("keeps the selection across a refresh", async () => {
    const t = await mount(fixture(30));
    t.rows()[0].querySelector(".tl-mk.outcome").click();
    await t.dom.window.Timeline.refresh();
    expect(t.rows()[0].querySelector(".tl-mk.outcome").classList.contains("sel")).toBe(true);
  });

  it("escapes labels from the server", async () => {
    const r = fixture(1);
    r.agents[0].label = '<img src=x onerror="alert(1)">';
    const t = await mount(r);
    expect(t.doc.querySelector("#tl-mount img")).toBeNull();
    expect(t.names()[0]).toContain("<img");
  });
});

describe("DATA.timeline", () => {
  it("asks for the window and the substrate choice", async () => {
    const dom = new JSDOM("<div></div>", { runScripts: "outside-only", url: "https://gov.example/dashboard" });
    const urls = [];
    dom.window.fetch = async (url) => { urls.push(url); return { ok: true, status: 200, json: async () => ({ success: true, events: [], agents: [], truncated: [], counts: {} }) }; };
    dom.window.eval(dataSource);
    const r = await dom.window.DATA.timeline({ hours: 24, includeSubstrate: false });
    expect(urls[0]).toBe("/v1/timeline?hours=24&include_substrate=0");
    expect(r.source).toBe("live");
    expect(r.data.events).toEqual([]);
  });
});

describe("DATA.timeline offline", () => {
  it("applies the window and the hook toggle to the bundled snapshot", async () => {
    const dom = new JSDOM("<div></div>", { runScripts: "outside-only", url: "file:///tmp/app.html" });
    dom.window.fetch = async () => { throw new TypeError("Failed to fetch"); };
    dom.window.eval(read("../redesign/snapshot.js"));
    dom.window.eval(dataSource);
    const six = await dom.window.DATA.timeline({ hours: 6 });
    const one = await dom.window.DATA.timeline({ hours: 1 });
    const noHooks = await dom.window.DATA.timeline({ hours: 6, includeSubstrate: false });
    expect(six.source).toBe("snapshot");
    expect(one.data.events.length).toBeLessThan(six.data.events.length);
    expect(one.data.window.hours).toBe(1);
    const start = Date.parse(one.data.window.start);
    expect(one.data.events.every((e) => Date.parse(e.ts) >= start)).toBe(true);
    expect(six.data.events.some((e) => e.authored_by === "substrate")).toBe(true);
    expect(noHooks.data.events.some((e) => e.authored_by === "substrate")).toBe(false);
    expect(noHooks.data.counts.checkin).toBe(noHooks.data.events.filter((e) => e.kind === "checkin").length);
  });
});

describe("live stream frames", () => {
  const settle = (dom, ms = 30) => new Promise((r) => dom.window.setTimeout(r, ms));
  // JSDOM reports a hidden document unless told otherwise; a live tab is visible.
  const visible = (t) => Object.defineProperty(t.doc, "hidden", { value: false, configurable: true });

  it("swallows frames the tab never shows without refetching", async () => {
    const t = await mount(fixture(5));
    visible(t);
    const before = t.calls.length;
    for (let i = 0; i < 50; i++) {
      for (const type of ["knowledge_read", "identity_resolved", "dashboard_viewed"]) {
        expect(t.dom.window.Timeline.applyEvent({ type })).toBe(true);
      }
    }
    expect(t.dom.window.Timeline.applyEvent(null)).toBe(true);
    await settle(t.dom);
    expect(t.calls.length).toBe(before);
  });

  it("coalesces a burst of relevant frames into one refetch", async () => {
    const t = await mount(fixture(5));
    visible(t);
    const before = t.calls.length;
    for (const type of ["eisv_update", "eisv_update", "lifecycle_paused", "knowledge_write", "dialectic_resolved"]) {
      expect(t.dom.window.Timeline.applyEvent({ type, agent_id: uid(0) })).toBe(true);
    }
    await settle(t.dom);
    expect(t.calls.length).toBe(before + 1);
    // The next relevant frame inside the throttle window queues, it does not fire.
    t.dom.window.Timeline.applyEvent({ type: "eisv_update" });
    await settle(t.dom);
    expect(t.calls.length).toBe(before + 1);
  });

  it("leaves the on-demand 24h window alone", async () => {
    const t = await mount(fixture(5));
    visible(t);
    t.doc.querySelector('.tl-win[data-h="24"]').click();
    await settle(t.dom);
    expect(t.calls.at(-1).hours).toBe(24);
    const before = t.calls.length;
    t.dom.window.Timeline.applyEvent({ type: "eisv_update" });
    await settle(t.dom);
    expect(t.calls.length).toBe(before);
  });
});

describe("app.html wiring", () => {
  const html = read("../redesign/app.html");
  it("routes stream frames to the tab instead of the refetch doorbell", () => {
    expect(html).toContain("timeline: (msg) => window.Timeline && window.Timeline.applyEvent && window.Timeline.applyEvent(msg)");
  });
  it("has the nav link, pane, script, lazy load and live tick", () => {
    expect(html).toContain('<a href="#timeline" data-section="timeline">Timeline</a>');
    expect(html).toContain('data-pane="timeline"');
    expect(html).toContain('id="tl-mount"');
    expect(html).toContain('<script src="./sections/timeline.js"></script>');
    expect(html).toContain('id === "timeline" && window.Timeline');
    expect(html).toContain("timeline: () => window.Timeline && window.Timeline.refresh()");
  });
});
