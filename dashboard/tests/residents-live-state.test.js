import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Regression tests for #1753 (c86fe79e) — the SECTION half: given a
// well-formed residentPanels() object, widgets must render row values (not
// ring nulls as zeroes), recompute liveness per row, discriminate zero from
// null, and never default a missing row to healthy. The data.js mapper half
// of c86fe79e (raw-API renames, .resident attachment) is NOT covered here;
// it needs a fetch-level harness — named follow-up on the PR.

const residentsSource = readFileSync(
  new URL("../redesign/sections/residents.js", import.meta.url),
  "utf8",
);

function makeDom(panels) {
  const dom = new JSDOM(`<div id="res-mount"></div>`, {
    runScripts: "outside-only",
    url: "https://governance.test/",
  });
  dom.window.DATA = {
    // Byte-faithful to data.js residentLiveness: only silent/paused/archived
    // are "down"; unknown still reports. head() only branches on === "down".
    residentLiveness: (r) => {
      if (!r) return "down";
      if (["silent", "paused", "archived"].includes(r.status)) return "down";
      return r.coherence != null ? "reporting" : "alive-no-eisv";
    },
    residentPanels: async () => ({ source: "live", data: panels }),
  };
  dom.window.eval(residentsSource);
  return dom;
}

// Exact value of the stat block labeled `label` inside a panel — stat()
// renders value-div then label-div, so scope assertions to the pair instead
// of panel-wide substring matches (which let 19 satisfy a "9" check).
function statValue(panel, label) {
  const matches = [...panel.querySelectorAll("div")].filter(
    (el) => el.textContent === label && el.previousElementSibling,
  );
  // Fail loud on ambiguity or unexpected markup — an empty value would make
  // the wrapper div match the label and silently return the PREVIOUS stat.
  if (matches.length !== 1) {
    throw new Error(`statValue: ${matches.length} matches for "${label}"`);
  }
  const wrapper = matches[0].parentElement;
  if (wrapper.children.length !== 2 || wrapper.children[1] !== matches[0]) {
    throw new Error(`statValue: unexpected stat markup for "${label}"`);
  }
  return matches[0].previousElementSibling.textContent;
}

const baseRow = {
  status: "healthy",
  silence: 100,
  silenceThreshold: 1800,
  coherence: 0.72,
  verdict: "proceed",
  updates: 7,
  risk: 0.18,
  eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0.1 },
  recent: [],
};

describe("residents section reads live state", () => {
  it("vigil card shows the residents-row values, not the ring's nulls as zeroes", async () => {
    // The exact #1753 setup: /v1/vigil/summary ring fields are null because
    // Vigil checks in via the in-process path that never reaches the ring.
    const dom = makeDom({
      vigil: {
        resident: { ...baseRow },
        avgCoherence: null,
        lastVerdict: null,
        cycles24h: null,
        lastCycleAgeS: null,
        eisv: {},
      },
    });
    await dom.window.Residents.load();
    const panel = [...dom.window.document.querySelectorAll(".panel")]
      .find((el) => el.querySelector("h2")?.textContent === "Vigil");
    expect(panel).toBeTruthy();
    // Row values exactly — the old panel showed a fabricated "0.00" (null||0)
    // and a "—" verdict here.
    expect(statValue(panel, "coherence")).toBe("0.72");
    expect(statValue(panel, "verdict")).toBe("proceed");
    expect(statValue(panel, "check-ins")).toBe("7");
    expect(panel.textContent).not.toContain("cycles 24h"); // ring empty → stat absent
  });

  it("vigil falls back to ring values only when the row lacks them", async () => {
    const dom = makeDom({
      vigil: {
        resident: { ...baseRow, coherence: null, verdict: null },
        avgCoherence: 0.55,
        lastVerdict: "proceed",
        cycles24h: 9,
        lastCycleAgeS: 120,
        eisv: {},
      },
    });
    await dom.window.Residents.load();
    const panel = [...dom.window.document.querySelectorAll(".panel")]
      .find((el) => el.querySelector("h2")?.textContent === "Vigil");
    expect(statValue(panel, "coherence")).toBe("0.55");
    expect(statValue(panel, "verdict")).toBe("proceed");
    expect(statValue(panel, "cycles 24h")).toBe("9"); // ring count kept when real
  });

  it("liveness derives from each row: an overdue resident reads silent, nulls read as dashes", async () => {
    const dom = makeDom({
      sentinel: {
        // Row says healthy but its own numbers say overdue — statusOf must
        // recompute to silent (warn pip), never trust a stale literal.
        resident: { ...baseRow, silence: 8000, silenceThreshold: 3600 },
        total: 3,
        bySeverity: { high: 1, medium: 1 },
        byClass: [],
        recent: [],
      },
      lumen: {
        ...baseRow,
        coherence: null,
        verdict: null,
        updates: null,
        risk: null,
        eisv: null,
        recent: [],
      },
      // Genuine zeroes are the mirror case of #1753: they must render as
      // numbers, never as "—" (an `|| fallback` would fabricate absence).
      steward: { ...baseRow, coherence: 0, updates: 0, risk: 0 },
    });
    await dom.window.Residents.load();
    const doc = dom.window.document;

    const sentinelPip = [...doc.querySelectorAll(".panel")]
      .find((el) => el.querySelector("h2")?.textContent === "Sentinel")
      .querySelector(".dot-pip");
    expect(sentinelPip.getAttribute("style")).toContain("var(--warn)");

    const lumen = [...doc.querySelectorAll(".panel")]
      .find((el) => el.querySelector("h2")?.textContent === "Lumen");
    // Null row fields render as placeholders — never fabricated numbers.
    expect(statValue(lumen, "coherence")).toBe("—");
    expect(statValue(lumen, "verdict")).toBe("—");
    expect(statValue(lumen, "check-ins")).toBe("—");
    expect(statValue(lumen, "risk")).toBe("—");
    expect(lumen.textContent).not.toContain("NaN");

    const steward = [...doc.querySelectorAll(".panel")]
      .find((el) => el.querySelector("h2")?.textContent === "Steward");
    expect(statValue(steward, "coherence")).toBe("0.00");
    expect(statValue(steward, "check-ins")).toBe("0");
    expect(statValue(steward, "risk")).toBe("0.00");
  });

  it("a missing residents row reads unknown (muted pip), never green", async () => {
    // /v1/residents can 5xx while the summary endpoints stay up — data.js
    // catches per-endpoint, so watcher/sentinel/vigil arrive with
    // resident: null. statusOf(null) used to default to "healthy": three
    // green pips under a live badge, the exact false-reassurance class
    // c86fe79e removed elsewhere.
    const dom = makeDom({
      watcher: { resident: null, total: 0, byStatus: {}, patterns: [], openSev: {} },
      sentinel: { resident: null, total: 0, bySeverity: {}, byClass: [], recent: [] },
      vigil: {
        resident: null, avgCoherence: null, lastVerdict: null,
        cycles24h: null, lastCycleAgeS: null, eisv: {},
      },
    });
    await dom.window.Residents.load();
    for (const name of ["Watcher", "Sentinel", "Vigil"]) {
      const pip = [...dom.window.document.querySelectorAll(".panel")]
        .find((el) => el.querySelector("h2")?.textContent === name)
        .querySelector(".dot-pip");
      expect(pip.getAttribute("style"), name).not.toContain("var(--ok)");
      expect(pip.getAttribute("style"), name).toContain("var(--muted)");
    }
  });
});
