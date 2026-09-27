import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Risk section — the time axis risk previously lacked. Reads
// DATA.riskTrend() (/v1/governance/trend: trailing-7-day series computed from
// core.agent_state, the governance.*.7d definitions), DATA.recentAgents() (any
// agent that checked in recently, not only residents) and DATA.agentHistory().
// Shapes verified against the live server on 2026-09-27: 60 daily points,
// fleet-mean risk ~0.034–0.058, guide ~8,000/week, pause single digits; the
// history endpoint returned decimated check-ins carrying `risk`.
//
// These tests pin the render, the chart wiring that theme/adapter constraints
// depend on, and — deliberately — the two honesty lines. A produced pause is
// not a delivered enforcement action, and the charted risk is the persisted
// pre-adjustment value; if either caveat is ever edited away, this fails.

const riskSource = readFileSync(
  new URL("../redesign/sections/risk.js", import.meta.url),
  "utf8",
);

const TREND = {
  windowDays: 60,
  risk: [
    { ts: "2026-08-25T08:00:00Z", value: 0.0512 },
    { ts: "2026-08-26T08:00:00Z", value: 0.0634 },
    { ts: "2026-08-27T08:00:00Z", value: 0.066 },
  ],
  pause: [
    { ts: "2026-08-25T08:00:00Z", value: 12 },
    { ts: "2026-08-26T08:00:00Z", value: 15 },
    { ts: "2026-08-27T08:00:00Z", value: 17 },
  ],
  guide: [
    { ts: "2026-08-25T08:00:00Z", value: 8800 },
    { ts: "2026-08-26T08:00:00Z", value: 9100 },
    { ts: "2026-08-27T08:00:00Z", value: 9402 },
  ],
};

const AGENTS = [
  { id: "uuid-lumen", name: "Lumen", risk: 0.2992, coherence: 0.4725 },
  { id: "uuid-watcher", name: "Watcher", risk: 0.3, coherence: 0.4853 },
  { id: "uuid-chron", name: "Chronicler", risk: 0.0, coherence: 0.4967 },
  // An agent whose latest check-in carried no risk. Must be excluded from
  // spread/highest, but still selectable.
  { id: "uuid-doctor", name: "Doctor", risk: null, coherence: null },
];

const HISTORY = [
  { t: "2026-08-01T10:00:00Z", risk: 0.05, coherence: 0.47, action: "approve", verdict: "safe" },
  { t: "2026-08-02T10:00:00Z", risk: 0.31, coherence: 0.48, action: "risk_pause", verdict: "high-risk" },
  { t: "2026-08-03T10:00:00Z", risk: 0.12, coherence: 0.47, action: "guide", verdict: "caution" },
  // A row with no risk (state written before the column was populated) must be
  // dropped rather than charted as a zero.
  { t: "2026-08-04T10:00:00Z", risk: null, coherence: 0.47, action: "guide" },
  // A row predating the action-write: no action recorded. Must NOT be read as
  // approve — that would invent a clean record.
  { t: "2026-08-05T10:00:00Z", risk: 0.08, coherence: 0.47 },
];

function mount(overrides = {}) {
  const dom = new JSDOM(`<div id="risk-mount"></div>`, {
    runScripts: "outside-only",
    url: "https://governance.test/",
  });
  const built = [];
  const trendCalls = [];
  const historyCalls = [];
  class ChartStub {
    constructor(ctx, cfg) { this.config = cfg; built.push(cfg); }
    destroy() { this.destroyed = true; }
    update() {}
  }
  dom.window.Chart = ChartStub;
  dom.window.HTMLCanvasElement.prototype.getContext = () => ({});
  dom.window.DATA = {
    riskTrend: async (days) => {
      trendCalls.push(days);
      return { source: overrides.source || "live", data: overrides.trend !== undefined ? overrides.trend : TREND };
    },
    recentAgents: async () => ({ source: "live", data: { agents: overrides.agents || AGENTS, coverageStart: null } }),
    agentHistory: async (id, opts) => {
      historyCalls.push({ id, opts });
      return { source: "live", data: { points: overrides.history || HISTORY, total: 31715 } };
    },
  };
  dom.window.eval(riskSource);
  return { dom, built, trendCalls, historyCalls, win: dom.window,
    el: (id) => dom.window.document.getElementById(id) };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

describe("risk history section", () => {
  it("renders the four stat cards from live trend + recent agents", async () => {
    const m = mount();
    await m.win.Risk.load();
    const cards = [...m.el("risk-stats").querySelectorAll(".card")];
    const byLabel = (l) => cards.find((c) => c.querySelector("h3")?.textContent === l);
    const numOf = (l) => byLabel(l).querySelector(".num").textContent;
    const subOf = (l) => byLabel(l).querySelector(".sub").textContent;

    expect(cards).toHaveLength(4);
    // Latest day, not the first or a mean of the window.
    expect(numOf("Fleet mean risk")).toBe("0.066");
    expect(numOf("Pause verdicts")).toBe("17");
    // Highest is Watcher (0.30) over Lumen (0.2992) — a naive string sort or a
    // reversed comparator would pick Lumen.
    expect(numOf("Highest-risk agent")).toBe("0.300");
    expect(subOf("Highest-risk agent")).toBe("Watcher");
    // Doctor (null risk) is excluded from both the spread and the count.
    expect(numOf("Agent spread")).toBe("0.00–0.30");
    expect(subOf("Agent spread")).toBe("3 agents checked in recently");
  });

  it("labels the pause card as produced, never delivered", async () => {
    const m = mount();
    await m.win.Risk.load();
    const cards = [...m.el("risk-stats").querySelectorAll(".card")];
    const pause = cards.find((c) => c.querySelector("h3")?.textContent === "Pause verdicts");
    expect(pause.querySelector(".sub").textContent).toMatch(/not deliveries/);
    // The gap-suppression caveat is load-bearing, not decoration.
    const text = m.el("risk-mount").textContent;
    expect(text).toMatch(/Produced, not delivered/);
    expect(text).toMatch(/>150s inter-check-in gap/);
  });

  it("states that charted risk is decision risk, pre-adjustment", async () => {
    const m = mount();
    await m.win.Risk.load();
    const text = m.el("risk-mount").textContent.replace(/\s+/g, " ");
    expect(text).toMatch(/decision.{0,3} risk/i);
    // Phi telemetry must stay named apart from decision risk.
    expect(text).toMatch(/Φ-derived risk telemetry/);
    expect(text).toMatch(/never recovery authority/);
    // And the response-envelope vs state-row seam must stay stated.
    expect(text).toMatch(/±0\.15/);
  });

  it("draws the trend on a category axis with MM-DD labels", async () => {
    const m = mount();
    await m.win.Risk.load();
    const trend = m.built[0];
    expect(trend.data.datasets[0].data).toEqual([0.0512, 0.0634, 0.066]);
    expect(trend.data.labels).toEqual(["08-25", "08-26", "08-27"]);
    // app.html loads chart.umd WITHOUT chartjs-adapter-date-fns: a time scale
    // silently renders a blank axis.
    expect(trend.options.scales.x.type).toBeUndefined();
    expect(trend.options.scales.y.beginAtZero).toBe(true);
  });

  it("puts guide on a second axis so pause is not flattened", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pressure = m.built[1];
    expect(pressure.data.datasets.map((d) => d.yAxisID)).toEqual(["y", "y1"]);
    expect(pressure.data.datasets[1].type).toBe("line");
    // Guide runs ~550x pause; without the right axis the bars vanish.
    expect(pressure.options.scales.y1.position).toBe("right");
    expect(pressure.options.scales.y1.grid.drawOnChartArea).toBe(false);
  });

  it("charts one agent's risk, dropping rows with no risk", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-lumen";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();

    expect(m.historyCalls).toEqual([{ id: "uuid-lumen", opts: { mode: "all", limit: 200 } }]);
    const agent = m.built[2];
    expect(agent.data.datasets[0].label).toBe("decision risk");
    // The null-risk row is dropped, not charted as 0.
    expect(agent.data.datasets[0].data).toEqual([0.05, 0.31, 0.12, 0.08]);
    // Coherence is a dashed reference, never a second risk signal.
    expect(agent.data.datasets[1].borderDash).toEqual([5, 4]);
    expect(agent.options.scales.y.min).toBe(0);
    expect(agent.options.scales.y.max).toBe(1);
    // Honest about decimation: 3 of the agent's real total.
    expect(m.el("risk-agent-meta").textContent).toBe(
      "4 of 31715 check-ins · 1 approve · 1 guide · 1 pause produced · 1 no action recorded",
    );
  });

  it("keeps the operator's agent selection across a reload", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-watcher";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();
    await m.win.Risk.load();
    expect(m.el("risk-agent-pick").value).toBe("uuid-watcher");
  });

  it("refetches the trend at the selected window", async () => {
    const m = mount();
    await m.win.Risk.load();
    expect(m.trendCalls).toEqual([60]);
    const sel = m.el("risk-window");
    sel.value = "14";
    sel.dispatchEvent(new m.win.Event("change"));
    await settle();
    expect(m.trendCalls).toEqual([60, 14]);
  });

  it("rebuilds every chart on retheme so tokens are re-read", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-lumen";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();
    const before = m.built.length;
    m.win.Risk.retheme();
    expect(m.built.length).toBe(before + 3);
  });

  it("shows an honest empty state when no check-in landed in the window", async () => {
    const m = mount({ trend: { windowDays: 60, risk: [], pause: [], guide: [] } });
    await m.win.Risk.load();
    expect(m.built).toHaveLength(0);
    expect(m.el("risk-trend-empty").textContent).toMatch(/No check-ins with a risk reading in the last 60 days/);
    expect(m.el("risk-pressure-empty").textContent).toMatch(/No verdicts recorded/);
    // Cards still render, with em-dashes rather than fabricated zeros.
    expect(m.el("risk-stats").querySelectorAll(".card")).toHaveLength(4);
  });

  it("badges the snapshot fallback rather than passing it off as live", async () => {
    const m = mount({ source: "snapshot" });
    await m.win.Risk.load();
    const badge = m.el("risk-src");
    expect(badge.textContent).toBe("snapshot");
    expect(badge.className).toBe("src-badge snapshot");
  });

  it("escapes agent names in the picker", async () => {
    const m = mount({ agents: [{ id: "x", name: '<img src=x onerror="boom">', risk: 0.1 }] });
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    expect(pick.querySelectorAll("img")).toHaveLength(0);
    expect(pick.options[1].textContent).toContain("<img src=x");
  });

  it("marks only the hard actions on the trajectory", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-lumen";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();
    const risk = m.built[2].data.datasets[0];
    // approve, risk_pause, guide, (no action) -> only risk_pause is marked.
    // Guide is ~69% of live rows; marking it would paint the whole line.
    expect(risk.pointRadius).toEqual([0, 4, 0, 0]);
    const marked = risk.pointBackgroundColor;
    expect(marked[1]).not.toBe(marked[0]);
    expect(marked[0]).toBe(marked[2]);
    expect(marked[3]).toBe(marked[0]);
  });

  it("names the produced action in the tooltip without implying delivery", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-lumen";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();
    const cb = m.built[2].options.plugins.tooltip.callbacks.afterBody;
    expect(cb([{ dataIndex: 1 }])).toBe(
      "action: risk_pause · high-risk (produced, not delivered)",
    );
    expect(cb([{ dataIndex: 0 }])).toBe("action: approve · safe");
    // A row with no action must say so, not default to approve.
    expect(cb([{ dataIndex: 3 }])).toBe("action: no action recorded");
  });

  it("counts an unrecorded action as its own class, never as approve", async () => {
    const m = mount({ history: [
      { t: "2026-08-01T10:00:00Z", risk: 0.1, coherence: 0.47 },
      { t: "2026-08-02T10:00:00Z", risk: 0.2, coherence: 0.47 },
    ] });
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-lumen";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();
    const meta = m.el("risk-agent-meta").textContent;
    expect(meta).toMatch(/2 no action recorded/);
    expect(meta).not.toMatch(/approve/);
    expect(m.el("risk-agent-body").textContent).toMatch(/predate the action-write/);
  });

  it("uses the same pause definition as governance.pause.7d", async () => {
    const m = mount();
    await m.win.Risk.load();
    const pick = m.el("risk-agent-pick");
    pick.value = "uuid-lumen";
    pick.dispatchEvent(new m.win.Event("change"));
    await settle();
    const legend = m.el("risk-agent-body").textContent.replace(/\s+/g, " ");
    expect(legend).toMatch(/neither approve nor guide/);
    expect(legend).toMatch(/governance\.pause\.7d/);
    expect(legend).toMatch(/produced/);
  });
});

describe("risk trend accessor (#2502)", () => {
  it("keeps a successful empty risk series live, with its verdict pressure", async () => {
    const dom = new JSDOM("", { runScripts: "outside-only", url: "https://governance.test/" });
    dom.window.fetch = async () => ({ ok: true, status: 200, json: async () => ({
      success: true, window_days: 14, risk: [], guide: [{ ts: "2026-09-27T00:00:00Z", value: 4 }], pause: [] }) });
    dom.window.eval(readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8"));
    const r = await dom.window.DATA.riskTrend(14);
    expect(r.source).toBe("live");
    expect(r.data.risk).toEqual([]);
    expect(r.data.guide[0].value).toBe(4);
  });
});

describe("check-in activity accessor (#2502)", () => {
  it("uses the server's exact window totals over summed buckets", async () => {
    const dom = new JSDOM("", { runScripts: "outside-only", url: "https://governance.test/" });
    dom.window.fetch = async () => ({ ok: true, status: 200, json: async () => ({
      success: true, window_minutes: 60, bucket_minutes: 5, coverage_start: 1,
      buckets: [{ proceed: 1, guide: 0, pause: 0 }], totals: { proceed: 3, guide: 2, pause: 1 } }) });
    dom.window.eval(readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8"));
    const r = await dom.window.DATA.checkinActivity();
    expect(r.data).toMatchObject({ proceed: 3, guide: 2, pause: 1, total: 6 });
  });
});
