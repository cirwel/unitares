import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Boots the real app.html shell script against stubbed DATA/Landing so the
// extension seam is exercised end to end: manifest -> injected script ->
// nav link + pane + mount -> lazy load on navigation.
const appSource = readFileSync(new URL("../redesign/app.html", import.meta.url), "utf8");
const inline = appSource.match(/<script>\n\/\/ theme toggle[\s\S]*?<\/script>/)[0]
  .replace(/^<script>/, "").replace(/<\/script>$/, "");
const body = appSource.slice(appSource.indexOf("<body"), appSource.indexOf("<script src=\"./snapshot.js\""));

function boot(manifest, scripts, hash = "") {
  const dom = new JSDOM(`<!doctype html><html data-theme="ink">${body}</body></html>`, {
    // "dangerously" so an injected <script> element executes, as in a browser.
    runScripts: "dangerously",
    url: "http://localhost/dashboard" + hash,
  });
  const w = dom.window;
  w.Landing = { render: vi.fn(), refresh: vi.fn(), refreshStats: vi.fn(), applyEvent: vi.fn(), tickSilence: vi.fn() };
  w.DATA = {
    extManifest: vi.fn(async () => manifest),
    extScript: vi.fn(async (p) => {
      if (!(p in scripts)) throw new Error(p + " -> 404");
      return scripts[p];
    }),
  };
  w.eval(inline);
  return dom;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("dashboard extensions", () => {
  it("ships only the core sections", () => {
    const dom = boot(null, {});
    const ids = [...dom.window.document.querySelectorAll("#nav a[data-section]")].map((a) => a.dataset.section);
    expect(ids).toEqual(["overview", "agents", "discoveries", "dialectic", "activity", "eisv", "risk", "security"]);
  });

  it("adds nothing when no manifest is served", async () => {
    const dom = boot(null, {});
    await flush();
    expect(dom.window.document.querySelectorAll("#nav a.ext").length).toBe(0);
  });

  it("registers a manifest section and loads it on navigation", async () => {
    const manifest = {
      scripts: ["data-ext.js"],
      sections: [{ id: "queue", label: "Queue", title: "Label queue", global: "Queue", script: "sections/queue.js", mount: "q-mount" }],
    };
    const dom = boot(manifest, {
      "data-ext.js": "window.DATA.extra = 1;",
      "sections/queue.js": "window.Queue = { load: () => { document.getElementById('q-mount').textContent = 'loaded'; } };",
    });
    await flush();
    const doc = dom.window.document;
    const link = doc.querySelector('#nav a[data-section="queue"]');
    expect(link.textContent).toBe("Queue");
    expect(dom.window.DATA.extra).toBe(1);
    const pane = doc.querySelector('[data-pane="queue"]');
    expect(pane.hidden).toBe(true);
    expect(pane.querySelector("h2").textContent).toBe("Label queue");
    link.click();
    expect(pane.hidden).toBe(false);
    expect(doc.getElementById("q-mount").textContent).toBe("loaded");
  });

  it("opens a deep link to an extension pane once the manifest has loaded", async () => {
    const manifest = { sections: [{ id: "queue", global: "Queue", script: "q.js" }] };
    const dom = boot(manifest, { "q.js": "window.Queue = { load: () => { document.getElementById('queue-mount').textContent = 'deep'; } };" }, "#queue");
    await flush();
    expect(dom.window.document.getElementById("queue-mount").textContent).toBe("deep");
  });

  it("never shadows a core section and skips malformed, unloadable, broken or global-reusing entries", async () => {
    const manifest = {
      sections: [
        { id: "agents", global: "Evil", script: "evil.js" },
        { id: "Bad Id", global: "X", script: "x.js" },
        { id: "gone", global: "Gone", script: "missing.js" },
        { id: "throws", global: "Throws", script: "throws.js" },
        { id: "noload", global: "NoLoad", script: "noload.js" },
        { id: "ok", global: "Ok", script: "ok.js" },
        { id: "dup", global: "Ok", script: "dup.js" },
        { id: "core-global", global: "Landing", script: "core.js" },
      ],
    };
    const dom = boot(manifest, {
      "evil.js": "window.Evil = 1;",
      "x.js": "",
      "throws.js": "throw new Error('boom'); window.Throws = { load() {} };",
      "noload.js": "window.NoLoad = {};",
      "ok.js": "window.Ok = { load() {} };",
      "dup.js": "throw new Error('never defines its own');",
      "core.js": "",
    });
    dom.window.console.warn = () => {};
    await flush();
    const ext = [...dom.window.document.querySelectorAll("#nav a.ext")].map((a) => a.dataset.section);
    expect(ext).toEqual(["ok"]);
    expect(dom.window.Evil).toBeUndefined();
  });

  it("keeps each tab bound to the module its own script defined", async () => {
    const manifest = { sections: [
      { id: "a", global: "A", script: "a.js" },
      { id: "b", global: "B", script: "b.js" },
    ] };
    const dom = boot(manifest, {
      "a.js": "window.A = { load: () => { document.getElementById('a-mount').textContent = 'A'; } };",
      // B's script also clobbers A after A registered.
      "b.js": "window.B = { load() {} }; window.A = { load: () => { document.getElementById('a-mount').textContent = 'hijacked'; } };",
    });
    await flush();
    dom.window.document.querySelector('#nav a[data-section="a"]').click();
    expect(dom.window.document.getElementById("a-mount").textContent).toBe("A");
  });
});
