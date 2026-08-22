import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Enforcement section renders the produced-vs-delivered honesty meter from
// GET /v1/enforcement/divergence (payload keys verified against the live
// endpoint 2026-08-21; server-side contract tests are in flight as PR
// #1801). These tests pin the render: the four meters, the suppression
// percentage, the divide-by-zero guard, the note passthrough (escaped),
// and the empty states.

const enforcementSource = readFileSync(
  new URL("../redesign/sections/enforcement.js", import.meta.url),
  "utf8",
);

function render(payload, source = "live") {
  const dom = new JSDOM(`<div id="enforcement-mount"></div>`, {
    runScripts: "outside-only",
    url: "https://governance.test/",
  });
  const calls = [];
  dom.window.DATA = {
    enforcementDivergence: async (days) => {
      calls.push(days);
      return { source, data: payload };
    },
  };
  dom.window.eval(enforcementSource);
  return dom.window.Enforcement.load().then(() => ({
    dom,
    calls,
    mount: dom.window.document.getElementById("enforcement-mount"),
  }));
}

const PAYLOAD = {
  window_days: 90,
  posture: "advisory",
  produced_pauses: 4,
  gap_suppressed: 2,
  delivered_pauses: 1,
  last_delivered_at: new Date(Date.now() - 2.5 * 86400000).toISOString(),
  weekly: [
    { week: "08-04", produced: 3, delivered: 0 },
    { week: "08-11", produced: 1, delivered: 1 },
  ],
  note: "A produced pause verdict is not a delivered enforcement <action>.",
};

describe("enforcement divergence section", () => {
  it("renders all four meters, the suppression pct, and the weekly bars", async () => {
    const { mount, calls } = await render(PAYLOAD);
    expect(calls).toEqual([90]);
    const cards = [...mount.querySelectorAll(".card")];
    const byLabel = (label) =>
      cards.find((c) => c.querySelector("h3")?.textContent === label);
    // Exact .num values — containment would let 4→40 or 12d→2d slip through.
    const numOf = (label) => byLabel(label).querySelector(".num").textContent;

    expect(numOf("Produced pauses")).toBe("4");
    // Only the window figure is load-bearing in the sub — exact-copy pins
    // have already caused a red build here (tests.yml notes on #1707).
    expect(byLabel("Produced pauses").querySelector(".sub").textContent)
      .toContain("90d");
    expect(numOf("Gap-suppressed")).toBe("2 (50%)");
    expect(numOf("Delivered pauses")).toBe("1");
    expect(numOf("Last delivered")).toBe("2d ago");

    const bars = mount.querySelectorAll(".enf-col");
    expect(bars.length).toBe(2);
    expect(bars[0].getAttribute("title")).toBe("08-04: 3 produced, 0 delivered");
    // Pin the bar SEMANTICS, not just the tooltip: amber (first) tracks
    // produced, red (second) tracks delivered — a height swap inverts the
    // honesty meter while every text assertion stays green.
    const [amber, red] = bars[0].querySelectorAll(".enf-bars div");
    expect(amber.getAttribute("style")).toContain("height:64px"); // produced 3 = max
    expect(red.getAttribute("style")).toContain("height:0px");    // delivered 0
  });

  it("escapes the server NOTE field (numeric fields are separate, see PR)", async () => {
    // Scope honesty: this pins esc() on the note only. The numeric
    // interpolations (stat values, weekly produced/delivered in the bar
    // title) are NOT escaped by the section — a non-numeric payload value
    // would land as markup. Documented follow-up; hardening enforcement.js
    // is a runtime change that doesn't belong in this diff.
    const { mount } = await render(PAYLOAD);
    // The note reaches the operator as text…
    expect(mount.textContent).toContain(
      "a delivered enforcement <action>",
    );
    // …never as live markup.
    expect(mount.innerHTML).not.toContain("<action>");
  });

  it("guards the percentage against zero produced pauses", async () => {
    const { mount } = await render({
      ...PAYLOAD,
      produced_pauses: 0,
      gap_suppressed: 0,
      delivered_pauses: 0,
      weekly: [],
    });
    const gap = [...mount.querySelectorAll(".card")]
      .find((c) => c.querySelector("h3")?.textContent === "Gap-suppressed");
    expect(gap.querySelector(".num").textContent).toBe("0 (0%)");
    expect(mount.textContent).not.toContain("NaN");
  });

  it("shows the empty states: no payload, and no delivered pause on record", async () => {
    // data.js's withFallback can't actually emit {source:"live", data:null}
    // (a null live result falls to snapshot) — label the fixture snapshot so
    // the defensive guard is tested under a seam state that can occur.
    const none = await render(null, "snapshot");
    expect(none.mount.textContent).toContain("no data");

    const never = await render({ ...PAYLOAD, last_delivered_at: null });
    const last = [...never.mount.querySelectorAll(".card")]
      .find((c) => c.querySelector("h3")?.textContent === "Last delivered");
    expect(last.querySelector(".num").textContent).toBe("never");
    expect(last.querySelector(".sub").textContent)
      .toContain("no delivered pause");
  });
});
