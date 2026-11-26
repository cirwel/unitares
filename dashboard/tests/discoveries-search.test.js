import { describe, expect, it } from "vitest";
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

describe("Discoveries server-side search", () => {
  it("submits live search on Enter/Search without firing partial prefixes and preserves ranking", async () => {
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
    const btn = dom.window.document.getElementById("dsc-search-btn");
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Recent unrelated discovery");

    // Partial typing must NOT hit the server (avoids recall_misses.jsonl prefix pollution).
    input.value = "spi disp";
    input.dispatchEvent(new dom.window.Event("input"));
    await flushMicrotasks();
    expect(calls.filter((c) => c.body?.arguments?.query)).toHaveLength(0);
    expect(dom.window.document.getElementById("dsc-status").textContent).toContain("Enter");

    // Explicit submit queries the server.
    input.value = "spi display triage";
    input.dispatchEvent(new dom.window.Event("input"));
    btn.click();
    await flushMicrotasks();

    const searchCalls = calls.filter((c) => c.body?.arguments?.query === "spi display triage");
    expect(searchCalls).toHaveLength(1);
    // Input element was not replaced during in-place update.
    expect(dom.window.document.getElementById("dsc-search")).toBe(input);
    // Stats should not be re-fetched on search submission.
    expect(calls.filter((c) => c.body?.arguments?.action === "stats")).toHaveLength(1);

    const cards = Array.from(dom.window.document.querySelectorAll("#dsc-list .panel"));
    expect(cards).toHaveLength(2);
    // Server relevance order preserved (older June entry stays rank 1 above newer November entry).
    expect(cards[0].textContent).toContain("TFT display triage runbook for embedded controller");
    expect(cards[1].textContent).toContain("Newer entry ranked second by server relevance");
    expect(cards[0].innerHTML).toContain("<mark>display</mark>");
    expect(dom.window.document.getElementById("dsc-status").textContent).toContain("hybrid_rrf · 2 results");

    // Clearing restores the cached recent list without another server round-trip when fresh.
    input.value = "";
    input.dispatchEvent(new dom.window.Event("input"));
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Recent unrelated discovery");
    expect(dom.window.document.getElementById("dsc-status").style.display).toBe("none");
  });

  it("refetches the default list after clearing filters when a refresh occurred under a filter", async () => {
    let version = 1;
    const { dom } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return {
          success: true,
          stats: { total_discoveries: 2, by_status: { open: 2 }, by_type: { bug_found: 1, note: 1 } },
        };
      }
      if (body?.arguments?.discovery_type === "bug_found") {
        return {
          success: true,
          discoveries: [{ id: "2026-11-25T10:00:00Z", type: "bug_found", status: "open", summary: "Filtered bug v" + version }],
        };
      }
      return {
        success: true,
        discoveries: [{ id: "2026-11-25T11:00:00Z", type: "note", status: "open", summary: "Default list v" + version }],
      };
    });

    await dom.window.Discoveries.load();
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Default list v1");

    // Select a type chip, receive a live write notification, and click refresh.
    dom.window.document.querySelector('.dsc-type[data-type="bug_found"]').click();
    await flushMicrotasks();
    version = 2;
    dom.window.Discoveries.notifyNew();
    dom.window.document.getElementById("dsc-new").click();
    await flushMicrotasks();
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Filtered bug v2");

    // Clearing back to "all" must refetch v2 instead of restoring stale v1.
    dom.window.document.querySelector('.dsc-type[data-type="all"]').click();
    await flushMicrotasks();
    expect(dom.window.document.getElementById("dsc-list").textContent).toContain("Default list v2");
  });

  it("reports page-cap truncation honestly on both text searches and type filters", async () => {
    const thirtyRows = Array.from({ length: 30 }, (_, i) => ({
      id: `2026-11-25T10:${String(i).padStart(2, "0")}:00Z`,
      type: "insight",
      status: "open",
      summary: `Insight entry ${i}`,
    }));
    const { dom } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return {
          success: true,
          stats: { total_discoveries: 500, by_status: { open: 500 }, by_type: { insight: 500 } },
        };
      }
      return {
        success: true,
        search_mode_used: body?.arguments?.query ? "hybrid_rrf" : "indexed_filters",
        _more_available: "Results may be limited to 30.",
        discoveries: thirtyRows,
      };
    });

    await dom.window.Discoveries.load();

    // Type-only filter reports capped status instead of silently hiding the 30-row cap.
    dom.window.document.querySelector('.dsc-type[data-type="insight"]').click();
    await flushMicrotasks();
    expect(dom.window.document.getElementById("dsc-status").textContent).toContain("showing latest 30 (capped at 30)");

    // Text search reports top-30 cap rather than claiming "30 matches".
    const input = dom.window.document.getElementById("dsc-search");
    input.value = "insight";
    input.dispatchEvent(new dom.window.Event("input"));
    input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));
    await flushMicrotasks();
    const statusText = dom.window.document.getElementById("dsc-status").textContent;
    expect(statusText).toContain("hybrid_rrf · showing top 30 (capped at 30)");
    expect(statusText).not.toContain("30 matches");
  });

  it("highlights terms without corrupting HTML entities or injecting markup", async () => {
    const { dom } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return { success: true, stats: { total_discoveries: 1, by_status: { open: 1 }, by_type: { note: 1 } } };
      }
      return {
        success: true,
        search_mode_used: "hybrid_rrf",
        discoveries: [{
          id: "2026-11-25T12:00:00Z",
          type: "note",
          status: "open",
          summary: 'Retry & recover when count < 5 and amp > "0"',
        }],
      };
    });

    await dom.window.Discoveries.load();
    const input = dom.window.document.getElementById("dsc-search");
    input.value = "error & amp";
    input.dispatchEvent(new dom.window.Event("input"));
    input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));
    await flushMicrotasks();

    const summaryEl = dom.window.document.querySelector("#dsc-list .panel");
    expect(summaryEl.innerHTML).toContain("<mark>&amp;</mark>");
    expect(summaryEl.innerHTML).toContain("<mark>amp</mark>");
    expect(summaryEl.innerHTML).not.toContain("<mark>&</mark>amp;");
    // Decoded textContent stays intact without literal "&amp;" / "&lt;" artifacts.
    expect(summaryEl.textContent).toContain('Retry & recover when count < 5 and amp > "0"');
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

  it("does not send unsubmitted draft text on type chip, time select, or refresh clicks", async () => {
    const { dom, calls } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return {
          success: true,
          stats: { total_discoveries: 10, by_status: { open: 10 }, by_type: { bug_found: 5, note: 5 } },
        };
      }
      return {
        success: true,
        discoveries: [{ id: "2026-11-25T12:00:00Z", type: "bug_found", status: "open", summary: "Filtered bug" }],
      };
    });

    await dom.window.Discoveries.load();
    const input = dom.window.document.getElementById("dsc-search");
    input.value = "disp";
    input.dispatchEvent(new dom.window.Event("input"));

    dom.window.document.querySelector('.dsc-type[data-type="bug_found"]').click();
    await flushMicrotasks();

    const timeSelect = dom.window.document.getElementById("dsc-time");
    timeSelect.value = "7d";
    timeSelect.dispatchEvent(new dom.window.Event("change"));
    await flushMicrotasks();

    dom.window.Discoveries.notifyNew();
    dom.window.document.getElementById("dsc-new").click();
    await flushMicrotasks();

    expect(calls.filter((c) => c.body?.arguments?.query)).toHaveLength(0);
  });

  it("shows unavailable instead of stale results on search failure and recovers when cleared", async () => {
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

    // Clearing the box restores the cached live default list instead of staying stuck.
    input.value = "";
    input.dispatchEvent(new dom.window.Event("input"));
    const recovered = dom.window.document.getElementById("dsc-list").textContent;
    expect(recovered).toContain("Initial row");
    expect(recovered).not.toContain("Server not answering");
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

  it("renders canonical corpus total, cold/terminal lifecycle statuses, and clickable type chips from live stats", async () => {
    const { dom, calls } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        // Real live shape returned by knowledge(action="stats"):
        // top-level total_discoveries/by_status are lifecycle-managed only,
        // while canonical_current_total + raw_current_counts cover the full corpus.
        return {
          success: true,
          stats: {
            total_discoveries: 2600,
            by_status: { open: 911, resolved: 137, archived: 705, cold: 847 },
            canonical_current_total: 2774,
            by_type: { note: 1229, insight: 288, bug_found: 281, rule: 182, custom_rare: 1, custom_common: 50 },
            raw_current_counts: {
              total_discoveries: 2774,
              by_status: { open: 911, resolved: 137, archived: 705, cold: 847, superseded: 155, closed: 19 },
              by_type: { note: 1229, insight: 288, bug_found: 281, rule: 182, custom_rare: 1, custom_common: 50 },
            },
          },
        };
      }
      return {
        success: true,
        discoveries: [{ id: "2026-11-25T12:00:00Z", type: "rule", status: "open", summary: "Canonical rule entry" }],
      };
    });

    await dom.window.Discoveries.load();
    const statsText = dom.window.document.getElementById("dsc-stats").textContent;
    expect(statsText).toContain("open 911");
    expect(statsText).toContain("cold 847");
    expect(statsText).toContain("superseded 155");
    expect(statsText).toContain("closed 19");
    expect(statsText).not.toContain("other");

    const chips = Array.from(dom.window.document.querySelectorAll(".dsc-type"));
    const chipTypes = chips.map((c) => c.dataset.type);
    expect(chipTypes[0]).toBe("all");
    expect(chips[0].textContent).toContain("2774");
    expect(chipTypes).toContain("rule");
    // Unlisted custom types tie-break by count descending.
    expect(chipTypes.indexOf("custom_common")).toBeLessThan(chipTypes.indexOf("custom_rare"));

    // Clicking a type chip from the live stats legend filters server-side using
    // the same full-corpus scope (including archived/cold/imported) as the badge count.
    dom.window.document.querySelector('.dsc-type[data-type="rule"]').click();
    await flushMicrotasks();
    const chipCall = calls.find((c) => c.body?.arguments?.discovery_type === "rule");
    expect(chipCall).toBeTruthy();
    expect(chipCall.body.arguments.include_archived).toBe(true);
    expect(chipCall.body.arguments.include_cold).toBe(true);
    expect(chipCall.body.arguments.authority_mode).toBe("all");

    // Time-window filtering under "All" uses the same widened corpus scope so
    // "All + 30d" stays a superset of "<Type> + 30d".
    dom.window.document.querySelector('.dsc-type[data-type="all"]').click();
    await flushMicrotasks();
    const timeSelect = dom.window.document.getElementById("dsc-time");
    timeSelect.value = "30d";
    timeSelect.dispatchEvent(new dom.window.Event("change"));
    await flushMicrotasks();
    const timeOnlyCall = calls.find(
      (c) => !c.body?.arguments?.discovery_type && !c.body?.arguments?.query && c.body?.arguments?.created_after,
    );
    expect(timeOnlyCall).toBeTruthy();
    expect(timeOnlyCall.body.arguments.include_archived).toBe(true);
    expect(timeOnlyCall.body.arguments.include_cold).toBe(true);
    expect(timeOnlyCall.body.arguments.authority_mode).toBe("all");

    // Text search under a type chip keeps default active-first ranking
    // (server handles damped archived/cold widening when active hits < 3).
    dom.window.document.querySelector('.dsc-type[data-type="rule"]').click();
    await flushMicrotasks();
    const input = dom.window.document.getElementById("dsc-search");
    input.value = "canonical";
    input.dispatchEvent(new dom.window.Event("input"));
    input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));
    await flushMicrotasks();
    const queryCall = calls.find((c) => c.body?.arguments?.discovery_type === "rule" && c.body?.arguments?.query === "canonical");
    expect(queryCall).toBeTruthy();
    expect(queryCall.body.arguments.include_archived).toBeUndefined();
    expect(queryCall.body.arguments.include_cold).toBeUndefined();
    expect(queryCall.body.arguments.authority_mode).toBeUndefined();
  });

  it("accounts for unknown statuses in the other bucket and reads raw_current_counts.by_type fallback", async () => {
    const { dom } = bootLive((url, body) => {
      if (body && body.arguments && body.arguments.action === "stats") {
        return {
          success: true,
          stats: {
            total_discoveries: 10,
            canonical_current_total: 15,
            by_status: { open: 10 },
            raw_current_counts: {
              total_discoveries: 15,
              by_status: { open: 10, draft: 5 },
              by_type: { observation: 15 },
            },
          },
        };
      }
      return { success: true, discoveries: [] };
    });

    await dom.window.Discoveries.load();
    const statsText = dom.window.document.getElementById("dsc-stats").textContent;
    expect(statsText).toContain("open 10");
    expect(statsText).toContain("other 5");
    expect(dom.window.document.querySelector('.dsc-type[data-type="observation"]')).toBeTruthy();
  });

  it("DATA.stats prefers canonical_current_total over lifecycle total_discoveries with fallback", async () => {
    const { dom } = bootLive((url, body) => {
      if (url.includes("/health")) return { status: "healthy", status_breakdown: { healthy: 1 } };
      if (body?.name === "knowledge" && body?.arguments?.action === "stats") {
        return {
          success: true,
          stats: { total_discoveries: 2600, canonical_current_total: 2774 },
        };
      }
      return { success: true, sessions: [], summary: { total: 1, by_status: { active: 1 } } };
    });

    const live = await dom.window.DATA.stats();
    expect(live.data.discoveries).toBe(2774);

    // Fallback for servers that only emit total_discoveries.
    const { dom: legacyDom } = bootLive((url, body) => {
      if (url.includes("/health")) return { status: "healthy", status_breakdown: { healthy: 1 } };
      if (body?.name === "knowledge" && body?.arguments?.action === "stats") {
        return { success: true, stats: { total_discoveries: 42 } };
      }
      return { success: true, sessions: [], summary: { total: 1, by_status: { active: 1 } } };
    });
    const legacy = await legacyDom.window.DATA.stats();
    expect(legacy.data.discoveries).toBe(42);
  });
});
