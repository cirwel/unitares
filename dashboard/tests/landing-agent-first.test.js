import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// The Overview leads with what every install has: agents checking in and the
// verdicts they get. Residents follow, and only when the deployment has some.
//
// Fleet Coherence left the headline row on 2026-09-27. It was a fleet mean
// whose between-agent spread is ~0.008, so it read 0.49–0.50 every time; the
// one thing it genuinely knew, resident cadence, now sits on the resident
// block's own summary line, and each resident's coherence stays in the strip.

const landingSource = readFileSync(new URL("../redesign/sections/landing.js", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../redesign/app.html", import.meta.url), "utf8");
const overviewPane = appSource.slice(appSource.indexOf('data-pane="overview"'), appSource.indexOf("<!-- Agents (increment 2) -->"));

const R = (over) => Object.assign({
  name: "R", status: "healthy", coherence: 0.48, risk: 0.1, verdict: "proceed",
  silence: 10, silenceThreshold: 3600, eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0 },
}, over);
const iso = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString();
const A = (id, secondsAgo, over) => Object.assign({
  id, name: "agent-" + id, ts: iso(secondsAgo), eisv: { E: 0.6, I: 0.7, S: 0.2, V: 0 },
  coherence: 0.5, risk: 0.2, action: "proceed", checkins: 3,
}, over);

async function boot({ residents = [], agents = [], coverageAgo = 7200, checkins, stats = {} } = {}) {
  const dom = new JSDOM(`<div id="serverStat"></div><div id="foot"></div>${overviewPane}`,
    { runScripts: "outside-only", url: "https://governance.test/" });
  const cover = Date.now() / 1000 - coverageAgo;
  dom.window.DATA = {
    residentLiveness: (r) => (r.status === "healthy" && r.coherence != null ? "reporting" : "down"),
    health: async () => ({ source: "live", data: { version: "t", uptime: "1h", db: "ok" } }),
    residents: async () => ({ source: "live", data: residents }),
    stats: async () => ({ source: "live", data: Object.assign({
      agentsActive: 1, agentsLive: 1, agentsPresenceUnknown: 0, agentsPresenceUnavailable: 0,
      agentsTotal: 40, dialectic: 0, dialecticRecent: 50, dialecticFailed: 23,
      discoveries: 10, systemHealth: "OK", degraded: 0,
    }, stats) }),
    recentAgents: async () => ({ source: "live", data: { agents, coverageStart: cover } }),
    checkinActivity: async () => ({ source: "live", data: Object.assign(
      { proceed: 5, guide: 2, pause: 0, total: 7, windowMin: 60, coverageStart: cover }, checkins) }),
  };
  dom.window.eval(landingSource);
  await dom.window.Landing.render();
  const doc = dom.window.document;
  const card = (title) => {
    const el = [...doc.querySelectorAll(".card")].find((c) => c.querySelector("h3")?.textContent === title);
    return el && { num: el.querySelector(".num").childNodes[0].textContent.trim(), sub: el.querySelector(".sub"), el };
  };
  return { dom, doc, card };
}

describe("overview headline row", () => {
  it("leads with agents and check-ins; no fleet coherence card", async () => {
    const { doc } = await boot();
    const titles = [...doc.querySelectorAll(".card h3")].map((h) => h.textContent);
    expect(titles).toEqual(["Agents", "Check-ins", "Dialectic", "Discoveries", "System Health"]);
  });

  it("counts agents that checked in within the hour, of the 30-day total", async () => {
    const { card } = await boot({ agents: [A("a", 30), A("b", 1800), A("c", 7000)] });
    const c = card("Agents");
    expect(c.num).toBe("2");
    expect(c.el.textContent).toContain("/ 40");
    expect(c.sub.textContent).toBe("checked in within the hour");
  });

  it("says when the server's check-in history covers less than the hour", async () => {
    const { card } = await boot({ agents: [A("a", 30)], coverageAgo: 600 });
    expect(card("Agents").sub.textContent).toContain("history covers 10m");
    expect(card("Check-ins").sub.textContent).toContain("history covers 10m");
  });

  it("splits check-ins by verdict and marks produced pauses", async () => {
    const quiet = (await boot()).card("Check-ins");
    expect(quiet.num).toBe("7");
    expect(quiet.sub.textContent).toContain("5 proceed · 2 guide · 0 pause");
    expect(quiet.sub.className).not.toContain("warn");
    const paused = (await boot({ checkins: { pause: 1, total: 8 } })).card("Check-ins");
    expect(paused.sub.className).toContain("warn");
  });

  it("leads the dialectic card with the failure rate, in warning colour", async () => {
    const c = (await boot()).card("Dialectic");
    expect(c.sub.textContent).toBe("23 of 50 recent failed (46%) · none open");
    expect(c.sub.className).toContain("warn");
    const clean = (await boot({ stats: { dialecticFailed: 0 } })).card("Dialectic");
    expect(clean.sub.textContent).toBe("none open · 50 recent");
    expect(clean.sub.className).not.toContain("warn");
  });

  it("falls back to the registry reading when the check-in ring does not answer", async () => {
    const { dom } = await boot();
    dom.window.DATA.recentAgents = async () => ({ source: "unavailable", data: { agents: [], coverageStart: null } });
    await dom.window.Landing.refresh();
    const c = [...dom.window.document.querySelectorAll(".card")].find((e) => e.querySelector("h3").textContent === "Agents");
    expect(c.querySelector(".sub").textContent).toContain("live binding/lease");
  });
});

describe("latest check-in", () => {
  it("shows the newest check-in of any agent, not only residents", async () => {
    const { doc } = await boot({ agents: [A("x", 5, { name: "ephemeral-x", action: "guide", risk: 0.4 }), A("r", 60)] });
    expect(doc.getElementById("pulseWho").textContent).toBe("ephemeral-x");
    expect(doc.getElementById("pulseVerdict").className).toContain("warn");
    expect(doc.getElementById("riskVal").textContent).toBe("0.40");
    const rows = [...doc.querySelectorAll("#recent tbody tr")].map((tr) => tr.children[0].textContent);
    expect(rows).toEqual(["ephemeral-x", "agent-r"]);
  });

  // The ring records sub_action, so a low-risk check-in arrives as "approve"
  // while its action is "proceed". The Overview speaks the server's verdict
  // vocabulary (proceed / guide / pause), and its risk ticks sit at the
  // default tier edges (config/governance_config.py: 0.45 guide, 0.70 pause).
  it("labels a recorded approve as proceed, and ticks the real tier edges", async () => {
    const { doc } = await boot({ agents: [A("x", 5, { action: "approve", risk: 0.1 }), A("y", 50, { action: "risk_pause", risk: 0.8 })] });
    const pill = doc.getElementById("pulseVerdict");
    expect(pill.textContent).toContain("proceed");
    expect(pill.textContent).not.toContain("approve");
    expect(pill.title).toBe("recorded as approve");
    const verdicts = [...doc.querySelectorAll("#recent tbody tr")].map((tr) => tr.children[1].textContent);
    expect(verdicts).toEqual(["proceed", "risk_pause"]);
    const ticks = [...doc.querySelectorAll(".ticks span")].map((t) => [t.textContent, t.style.left]);
    expect(ticks).toEqual([["guide", "45%"], ["pause", "70%"]]);
  });

  it("applies a pushed check-in in place: top of the feed, counted, no refetch", async () => {
    const { dom, doc, card } = await boot({ agents: [A("a", 30), A("b", 60)] });
    const handled = dom.window.Landing.applyEvent({
      type: "eisv_update", agent_id: "b", agent_name: "agent-b", risk: 0.7,
      eisv: { E: 0.5, I: 0.5, S: 0.3, V: -0.1 }, decision: { action: "pause" },
    });
    expect(handled).toBe(true);
    expect(doc.getElementById("pulseWho").textContent).toBe("agent-b");
    expect(doc.getElementById("pulseVerdict").className).toContain("danger");
    expect(card("Check-ins").num).toBe("8");
    expect(card("Check-ins").sub.className).toContain("warn");
    expect(doc.querySelector("#recent tbody tr").children[3].textContent).toBe("4");
  });

  it("says so when nothing has checked in yet", async () => {
    const { doc } = await boot();
    expect(doc.getElementById("pulseWho").textContent).toBe("no check-ins yet");
    expect(doc.querySelectorAll("#recent tr").length).toBe(0);
  });
});

describe("resident block", () => {
  it("is absent on an install with no residents", async () => {
    const { doc } = await boot();
    expect(doc.getElementById("resBlock").hidden).toBe(true);
  });

  it("carries cadence on its summary line, never as a health colour", async () => {
    const { doc } = await boot({ residents: [R({ name: "A" }), R({ name: "B" }), R({ name: "C", status: "silent", coherence: null })] });
    expect(doc.getElementById("resBlock").hidden).toBe(false);
    const summary = doc.getElementById("resSummary").textContent;
    expect(summary).toContain("2 of 3 in cadence");
    expect(summary).toContain("1 not checking in");
  });

  it("updates a resident's chip from a pushed check-in", async () => {
    const { dom, doc } = await boot({ residents: [R({ name: "A", coherence: 0.4 })] });
    dom.window.Landing.applyEvent({ type: "eisv_update", agent_id: "u", agent_name: "A", coherence: 0.47 });
    expect(doc.getElementById("residents").textContent).toContain("coh 0.47");
  });
});

describe("review follow-ups (#2502)", () => {
  it("reads a guided check-in's verdict from sub_action", async () => {
    const { dom, doc, card } = await boot({ agents: [A("a", 30)] });
    dom.window.Landing.applyEvent({ type: "eisv_update", agent_id: "a", agent_name: "agent-a",
      decision: { action: "proceed", sub_action: "guide" } });
    expect(doc.getElementById("pulseVerdict").textContent).toContain("guide");
    expect(card("Check-ins").sub.textContent).toContain("3 guide");
  });

  it("re-reads the hour on the stats cadence so pushed check-ins age out", async () => {
    const { dom, card } = await boot({ agents: [A("a", 30)] });
    dom.window.Landing.applyEvent({ type: "eisv_update", agent_id: "a", decision: { action: "proceed" } });
    expect(card("Check-ins").num).toBe("8");
    await dom.window.Landing.refreshStats();
    expect(card("Check-ins").num).toBe("7"); // the server's own hour, not a running tally
  });

  it("clears the latest-check-in detail when the ring empties", async () => {
    const { dom, doc } = await boot({ agents: [A("a", 30, { risk: 0.5, action: "pause" })] });
    expect(doc.getElementById("riskVal").textContent).toBe("0.50");
    dom.window.DATA.recentAgents = async () => ({ source: "live", data: { agents: [], coverageStart: Date.now() / 1000 } });
    await dom.window.Landing.refresh();
    expect(doc.getElementById("pulseWho").textContent).toBe("no check-ins yet");
    expect(doc.getElementById("riskVal").textContent).toBe("—");
    expect(doc.getElementById("pulseVerdict").className).toBe("verdict");
    expect(doc.getElementById("eisv").innerHTML).toBe("");
  });
});
