import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

/*
 * The residentless install — CLAUDE.md: "the default, so it is the case to
 * test." Covers the data.js MAPPER half of residentPanels(), which
 * residents-live-state.test.js explicitly leaves out ("it needs a fetch-level
 * harness — named follow-up on the PR"). So this spec drives data.js through a
 * stubbed fetch rather than hand-building a `panels` object.
 *
 * Defect: Watcher, Sentinel and Vigil build from their OWN summary endpoints,
 * all three of which are roster-independent and answer on a fresh install. None
 * of their creation guards consulted /v1/residents, so an adopter with no
 * residents (or a differently-named roster) got three panels under the
 * "Always-on fleet" eyebrow and a `live` source badge, claiming residents the
 * deployment does not have.
 *
 * FIXTURE PROVENANCE — every body below was produced by EXECUTING the repo's
 * own aggregators against a residentless server (agent_metadata = {}), not
 * hand-written. Each constant names the server file and function it mirrors.
 * /health/deep is deliberately absent: its shape could not be derived without
 * standing up src/services/health_snapshot's cached probe, so it is modelled as
 * unreachable (data.js already `.catch(() => null)`s it) instead of invented.
 */

const dataSource = readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8");
const residentsSource = readFileSync(
  new URL("../redesign/sections/residents.js", import.meta.url),
  "utf8",
);

// src/http_routes/residents.py :: _resolve_resident_labels -> ([], "none")
// (precedence path 4) and http_residents, whose row loop iterates `labels` —
// so `residents` is empty and `configured` is empty. Executed, not assumed.
const RESIDENTS_EMPTY = { success: true, configured: [], residents: [], source: "none" };

// src/http_routes/watcher.py :: _watcher_summary_from_rows([]) plus the
// success/findings_path keys http_watcher_summary adds. Roster-independent:
// the handler reads findings.jsonl, and its docstring states
// "absence = empty summary (not an error)" — so rows=[] on a fresh install.
// The 30 zero-buckets are elided to one representative day; `timeline` is not
// read by residentPanels(), only its presence matters to `if (w)`.
const WATCHER_EMPTY = {
  total: 0,
  by_status: {},
  by_severity_open: {},
  patterns: [],
  timeline: [{ day: "2026-08-11", detected: 0, confirmed: 0, dismissed: 0 }],
  window_days: 30,
  generated_at: "2026-09-09T18:00:07.183203+00:00",
  success: true,
  findings_path: "/root/.unitares/watcher/findings.jsonl",
};

// src/http_routes/sentinel.py :: _sentinel_summary_from_events([]) plus the
// success/source keys http_sentinel_summary adds. Roster-independent: it reads
// audit.events (or the in-memory ring on fallback), never the roster.
const SENTINEL_EMPTY = {
  total: 0,
  by_severity: {},
  by_violation_class: [],
  recent: [],
  window_hours: 24,
  generated_at: "2026-09-09T18:00:07.183759+00:00",
  success: true,
  source: "audit_durable",
};

// src/http_routes/vigil.py :: http_vigil_summary's `if not agent_id:` branch,
// selected by executing _vigil_agent_id(server with agent_metadata={}) -> None.
// Roster-independent: it scans agent_metadata for a label "vigil", not the
// roster. `stats` is {} — truthy, which is why `if (vg && vg.stats)` passed.
const VIGIL_EMPTY = {
  success: true,
  agent_id: null,
  window_hours: 72,
  stats: {},
  cycles: [],
  recent_writes: [],
  generated_at: "2026-09-09T18:00:07.183900+00:00",
  note: "no Vigil agent found in metadata",
};

// A rostered row, shaped after the http_residents docstring's response block
// (src/http_routes/residents.py, "residents": [{...}]). Only the fields
// residentPanels() reads are populated.
const rosterRow = (label) => ({
  label,
  agent_id: "a-" + label.toLowerCase(),
  status: "healthy",
  silence_seconds: 100,
  silence_threshold_seconds: 1800,
  last_checkin_at: "2026-09-09T17:00:00+00:00",
  last_checkin_source: "broadcaster_eisv",
  eisv: { E: 0.7, I: 0.8, S: 0.2, V: 0.1 },
  coherence: 0.72,
  risk_score: 0.18,
  verdict: "proceed",
  recent_writes: [],
  total_updates: 7,
});

// `residents` null models /v1/residents failing (data.js catches it to null);
// anything else is returned as the JSON body.
function makeDom({ residents }) {
  const dom = new JSDOM(`<div id="res-mount"></div>`, {
    runScripts: "outside-only",
    url: "https://governance.test/",
  });
  const body = {
    "/v1/watcher/summary": WATCHER_EMPTY,
    "/v1/sentinel/summary": SENTINEL_EMPTY,
    "/v1/vigil/summary": VIGIL_EMPTY,
    "/v1/residents": residents,
  };
  dom.window.fetch = async (path) => {
    // /health/deep is NOT modelled — see the provenance note above. An
    // unreachable probe is the honest stand-in and data.js already tolerates it.
    if (!(path in body) || body[path] == null) return { ok: false, status: 503 };
    return { ok: true, status: 200, json: async () => body[path] };
  };
  dom.window.eval(dataSource);
  return dom;
}

async function panelsOf(opts) {
  const dom = makeDom(opts);
  const r = await dom.window.DATA.residentPanels();
  return { dom, r, d: r.data || {} };
}

describe("residentPanels() on a residentless install", () => {
  it("renders no named-resident panel when the roster is empty", async () => {
    const { r, d } = await panelsOf({ residents: RESIDENTS_EMPTY });
    // Guard the premise: if this stopped being the live path the assertions
    // below would pass vacuously off the snapshot.
    expect(r.source).toBe("live");
    // The three that built from their own roster-independent summaries.
    expect(d.watcher).toBeFalsy();
    expect(d.sentinel).toBeFalsy();
    expect(d.vigil).toBeFalsy();
    // The three that were already correct, kept as a control.
    expect(d.chronicler).toBeNull();
    expect(d.steward).toBeNull();
    expect(d.lumen).toBeNull();
  });

  it("names no absent resident in the rendered pane", async () => {
    const { dom } = await panelsOf({ residents: RESIDENTS_EMPTY });
    dom.window.eval(residentsSource);
    await dom.window.Residents.load();
    const mount = dom.window.document.querySelector("#res-mount");
    expect(mount.querySelectorAll(".panel").length).toBe(0);
    for (const name of ["Watcher", "Sentinel", "Vigil", "Steward", "Chronicler", "Lumen"]) {
      expect(mount.textContent).not.toContain(name);
    }
  });

  it("suppresses per label, not per empty roster — a roster naming only Watcher keeps only Watcher", async () => {
    // The same defect hit any adopter whose UNITARES_RESIDENTS names a
    // different set, not just the empty one: the guards never looked at the
    // roster in any form.
    const { d } = await panelsOf({
      residents: { success: true, configured: ["Watcher"], residents: [rosterRow("Watcher")], source: "env" },
    });
    expect(d.watcher).toBeTruthy();
    expect(d.watcher.resident).toBeTruthy();
    expect(d.sentinel).toBeFalsy();
    expect(d.vigil).toBeFalsy();
  });

  it("a /v1/residents outage is not a de-rostering — panels still render", async () => {
    // res === null means the roster is UNKNOWN, not empty. Suppressing here
    // would delete three panels from a fully-rostered deployment on a blip,
    // and is the same distinction residents-live-state.test.js protects when
    // it asserts a missing row reads unknown rather than vanishing.
    const { d } = await panelsOf({ residents: null });
    expect(d.watcher).toBeTruthy();
    expect(d.sentinel).toBeTruthy();
    expect(d.vigil).toBeTruthy();
    expect(d.watcher.resident).toBeUndefined();
  });
});
