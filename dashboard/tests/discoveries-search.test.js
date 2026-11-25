import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const dataSource = read("../redesign/data.js");
const sectionSource = read("../redesign/sections/discoveries.js");

function bootLive(handler) {
  const dom = new JSDOM('<div id="dsc-mount"></div>', {
    runScripts: "outside-only",
    url: "https://gov.example/dashboard",
  });
  const calls = [];
  dom.window.fetch = async (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({ url: String(url), body });
    const resp = await handler(String(url), body, calls.length);
    if (resp instanceof Error) throw resp;
    return {
      ok: true,
      status: 200,
      json: async () => resp,
      headers: { get: () => "application/json" },
    };
  };
  dom.window.eval(dataSource);
  dom.window.eval(sectionSource);
  return { dom, calls };
}

function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Discoveries server-side search", () => {
  it("queries the server on live search input and preserves semantic/non-contiguous hits and ranking", async () => {
    vi.useFakeTimers();
    const { dom, calls } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return {
          success: true,
          stats: {
            total_discoveries: 2770,
            by_status: { open: 911, resolved: 137 },
            by_type: { insight: 500, bug_found: 200 },
          },
        };
      }
      const q = body && body.arguments && body.arguments.query;
      if (q === "spi display triage") {
        return {
          success: true,
          search_mode_used: "hybrid_rrf",
          low_confidence: false,
          discoveries: [
            {
              id: "2026-06-30T21:57:28.659025+00:00",
              type: "documentation",
              status: "open",
              by: "agent-a",
              summary: "TFT display triage runbook for embedded controller",
              details: "SPI hardware note (no contiguous query string)",
              tags: ["display"],
            },
            {
              id: "2026-11-12T10:30:08.200699+00:00",
              type: "observation",
              status: "open",
              by: "agent-b",
              summary: "Newer entry ranked second by server relevance",
              details: "",
              tags: ["display"],
            },
          ],
        };
      }
      return {
        success: true,
        discoveries: [
          {
            id: "2026-11-25T10:00:00+00:00",
            type: "note",
            status: "open",
            by: "agent-recent",
            summary: "Recent unrelated discovery",
            details: "",
            tags: [],
          },
        ],
      };
    });

    await dom.window.Discoveries.load();
    const input = dom.window.document.getElementById("dsc-search");
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Recent unrelated discovery");

    input.value = "spi display triage";
    input.dispatchEvent(new dom.window.Event("input"));

    // Debounced: no search request fires immediately on typing.
    const searchCallsBefore = calls.filter((c) => c.body?.arguments?.query === "spi display triage");
    expect(searchCallsBefore).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(260);

    const searchCalls = calls.filter((c) => c.body?.arguments?.query === "spi display triage");
    expect(searchCalls).toHaveLength(1);
    // Input element was not replaced during in-place update.
    expect(dom.window.document.getElementById("dsc-search")).toBe(input);

    // Stats should not be re-fetched on search keystrokes.
    const statsCalls = calls.filter((c) => c.body?.arguments?.action === "stats");
    expect(statsCalls).toHaveLength(1);

    const cards = Array.from(dom.window.document.querySelectorAll("#dsc-list .panel"));
    expect(cards).toHaveLength(2);
    // Server relevance order preserved (older June entry stays rank 1 above newer November entry).
    expect(cards[0].textContent).toContain("TFT display triage runbook for embedded controller");
    expect(cards[1].textContent).toContain("Newer entry ranked second by server relevance");
    // Individual query term highlighted.
    expect(cards[0].innerHTML).toContain("<mark>display</mark>");
    expect(dom.window.document.getElementById("dsc-status").textContent).toContain("hybrid_rrf · 2 matches");

    // Clearing restores the cached recent list without another server round-trip.
    input.value = "";
    input.dispatchEvent(new dom.window.Event("input"));
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Recent unrelated discovery");
    expect(dom.window.document.getElementById("dsc-status").style.display).toBe("none");
  });

  it("ignores stale out-of-order server responses", async () => {
    let resolveFirst;
    let resolveSecond;
    const { dom } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return { success: true, stats: { total_discoveries: 10, by_status: { open: 10 }, by_type: { note: 10 } } };
      }
      const q = body && body.arguments && body.arguments.query;
      if (q === "first") {
        return new Promise((resolve) => { resolveFirst = resolve; });
      }
      if (q === "second") {
        return new Promise((resolve) => { resolveSecond = resolve; });
      }
      return { success: true, discoveries: [] };
    });

    await dom.window.Discoveries.load();
    const input = dom.window.document.getElementById("dsc-search");

    input.value = "first";
    input.dispatchEvent(new dom.window.Event("input"));
    input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));

    input.value = "second";
    input.dispatchEvent(new dom.window.Event("input"));
    input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));

    resolveSecond({
      success: true,
      search_mode_used: "hybrid_rrf",
      discoveries: [{ id: "2026-11-25T12:00:00Z", type: "note", status: "open", summary: "Second query answer" }],
    });
    await flushMicrotasks();
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Second query answer");

    // Earlier slower response resolves later and must be discarded.
    resolveFirst({
      success: true,
      search_mode_used: "hybrid_rrf",
      discoveries: [{ id: "2026-11-25T11:00:00Z", type: "note", status: "open", summary: "Stale first answer" }],
    });
    await flushMicrotasks();
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Second query answer");
    expect(dom.window.document.getElementById("dsc-list").textContent).not.toContain("Stale first answer");
  });

  it("sends discovery_type and created_after filters to the server in live mode", async () => {
    const { dom, calls } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return {
          success: true,
          stats: { total_discoveries: 50, by_status: { open: 50 }, by_type: { bug_found: 20, note: 30 } },
        };
      }
      return {
        success: true,
        discoveries: [{ id: "2026-11-25T12:00:00Z", type: "bug_found", status: "open", summary: "Server filtered bug" }],
      };
    });

    await dom.window.Discoveries.load();
    const bugBtn = dom.window.document.querySelector('.dsc-type[data-type="bug_found"]');
    bugBtn.click();
    await flushMicrotasks();

    const typeCall = calls.find((c) => c.body?.arguments?.discovery_type === "bug_found");
    expect(typeCall).toBeTruthy();

    const timeSelect = dom.window.document.getElementById("dsc-time");
    timeSelect.value = "7d";
    timeSelect.dispatchEvent(new dom.window.Event("change"));
    await flushMicrotasks();

    const timeCall = calls.find((c) => c.body?.arguments?.discovery_type === "bug_found" && c.body?.arguments?.created_after);
    expect(timeCall).toBeTruthy();
  });

  it("shows unavailable instead of stale results when a live server search fails", async () => {
    let failSearch = false;
    const { dom } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return { success: true, stats: { total_discoveries: 1, by_status: { open: 1 }, by_type: { note: 1 } } };
      }
      if (failSearch) return new TypeError("Failed to fetch");
      return {
        success: true,
        discoveries: [{ id: "2026-11-25T12:00:00Z", type: "note", status: "open", summary: "Initial row" }],
      };
    });

    await dom.window.Discoveries.load();
    failSearch = true;
    const input = dom.window.document.getElementById("dsc-search");
    input.value = "outage";
    input.dispatchEvent(new dom.window.Event("input"));
    input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));
    await flushMicrotasks();

    const text = dom.window.document.getElementById("dsc-list").textContent;
    expect(text).toContain("Server not answering");
    expect(text).not.toContain("Initial row");
    expect(text).not.toContain("No matches");
  });

  it("keeps client-side filtering working in offline snapshot mode", async () => {
    const dom = new JSDOM('<div id="dsc-mount"></div>', {
      runScripts: "outside-only",
      url: "https://gov.example/dashboard?snapshot=1",
    });
    let fetchCount = 0;
    dom.window.fetch = async () => {
      fetchCount += 1;
      throw new TypeError("offline");
    };
    dom.window.SNAPSHOT = {
      discoveries: {
        total: 2,
        byStatus: { open: 2 },
        byType: { note: 2 },
        list: [
          { id: "2026-11-25T12:00:00Z", type: "note", status: "open", summary: "Alpha offline hit", details: "", tags: ["alpha"] },
          { id: "2026-11-25T11:00:00Z", type: "note", status: "open", summary: "Beta entry", details: "", tags: ["beta"] },
        ],
      },
    };
    dom.window.eval(dataSource);
    dom.window.eval(sectionSource);

    await dom.window.Discoveries.load();
    const afterLoadFetches = fetchCount;

    const input = dom.window.document.getElementById("dsc-search");
    input.value = "alpha";
    input.dispatchEvent(new dom.window.Event("input"));

    expect(fetchCount).toBe(afterLoadFetches);
    const listText = dom.window.document.getElementById("dsc-list").textContent;
    expect(listText).toContain("Alpha offline hit");
    expect(listText).not.toContain("Beta entry");
  });
});
