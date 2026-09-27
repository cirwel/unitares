import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// What a fresh, residentless install without passkeys sees on the tabs that
// used to assume a configured fleet or one maintainer's machine.

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const dataSource = read("../redesign/data.js");

describe("EISV trajectory prompt", () => {
  async function eisv(residents) {
    const dom = new JSDOM('<div id="eisv-mount"></div>', {
      runScripts: "outside-only", url: "https://governance.test/#eisv",
    });
    dom.window.eval(dataSource);
    dom.window.DATA.eisv = async () => ({ source: "live", data: { raw: [], series: [], sourceLanes: [], coherenceEq: 0.5 } });
    dom.window.DATA.residents = async () => ({ source: "live", data: residents });
    dom.window.Chart = class { constructor(_c, config) { this.data = config.data; } update() {} destroy() {} };
    dom.window.eval(read("../redesign/sections/eisv.js"));
    await dom.window.EISV.load();
    return dom.window.document.getElementById("eisv-trajectory").textContent;
  }

  it("does not point at a heatmap an install without residents never draws", async () => {
    const text = await eisv([]);
    expect(text).not.toMatch(/click a resident|in the heatmap/);
    expect(text).toMatch(/configures, and there are none/);
    expect(text).toMatch(/Risk tab/);
  });

  it("asks for a row click when Fleet readings has rows", async () => {
    const text = await eisv([{ id: "a-1", name: "Agent 1", eisv: { E: 0.5, I: 0.7, S: 0.2, V: 0 }, coherence: 0.5 }]);
    expect(text).toMatch(/click a row above/);
    expect(text).not.toMatch(/there are none/);
  });
});

describe("Security tab on a server without passkeys", () => {
  function boot(fetchImpl) {
    const dom = new JSDOM('<div id="security-mount"></div>', {
      runScripts: "outside-only", url: "https://governance.test/#security",
    });
    dom.window.localStorage.setItem("unitares_api_token", "tok");
    dom.window.fetch = fetchImpl;
    dom.window.eval(dataSource);
    dom.window.eval(read("../redesign/sections/security.js"));
    return dom;
  }
  const json = (status, body) => ({ ok: status < 400, status, json: async () => body });

  it("shows the setup hint, not a red session error, when the RP id is unset", async () => {
    const fix = "set UNITARES_DASHBOARD_RP_ID to the domain the dashboard is served from";
    const fetch = vi.fn(async (url) => String(url) === "/auth/enroll"
      ? json(503, { error: "passkey sign-in is not configured", fix })
      : json(403, { error: "dashboard session and X-Unitares-Csrf: 1 required" }));
    const dom = boot(fetch);
    await dom.window.Security.load();
    const status = dom.window.document.getElementById("securityStatus");
    expect(status.className).not.toContain("error");
    expect(status.textContent).toContain("Passkey sign-in is off on this server");
    expect(status.textContent).toContain(fix);
    expect(status.textContent).not.toMatch(/403/);
    expect(dom.window.document.getElementById("mintCode").disabled).toBe(true);
  });

  it("keeps the session error when passkeys are configured", async () => {
    const fetch = vi.fn(async (url) => String(url) === "/auth/enroll"
      ? json(403, { error: "valid enrollment code required" })
      : json(403, { error: "dashboard session and X-Unitares-Csrf: 1 required" }));
    const dom = boot(fetch);
    await dom.window.Security.load();
    const status = dom.window.document.getElementById("securityStatus");
    expect(status.className).toContain("error");
    expect(status.textContent).toMatch(/requires an active dashboard session/);
  });

  it("does not guess when the configuration probe cannot answer", async () => {
    const fetch = vi.fn(async (url) => {
      if (String(url) === "/auth/enroll") throw new TypeError("Failed to fetch");
      return json(403, {});
    });
    const dom = boot(fetch);
    await dom.window.Security.load();
    expect(dom.window.document.getElementById("securityStatus").className).toContain("error");
  });

  it("reads another 503 as unknown, not as unconfigured", async () => {
    const dom = boot(vi.fn(async () => json(503, { error: "authentication storage unavailable" })));
    expect(await dom.window.DATA.passkeyConfig()).toBeNull();
  });
});

describe("host-neutral and maintainer-neutral text", () => {
  it("names no host or maintainer in core copy", () => {
    expect(read("../redesign/sections/activity.js")).not.toMatch(/Codex usually/);
    expect(read("../redesign/auth/enroll.html")).not.toMatch(/Kenny/);
    expect(read("../redesign/sections/risk.js")).not.toMatch(/resident cadence/);
  });
});
