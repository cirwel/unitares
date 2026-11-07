import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
const source = readFileSync(new URL("../redesign/sections/connectors.js", import.meta.url), "utf8");
const settings = { connectors: [], server_url: "https://server.example/mcp/", authorization_endpoint: "https://server.example/authorize", token_endpoint: "https://server.example/token", scopes: "mcp:tools", token_auth_method: "client_secret_basic" };
function boot(overrides = {}) {
  const dom = new JSDOM('<div id="connectors-mount"></div>', { runScripts: "outside-only", url: "https://server.example/#connectors" });
  dom.window.DATA = { connectors: vi.fn().mockResolvedValue(settings), createConnector: vi.fn().mockResolvedValue({ client_id: "new-client", client_secret: "one-time-secret" }), ...overrides };
  dom.window.eval(source);
  return dom;
}
async function submit(dom) {
  dom.window.document.getElementById("connector-name").value = "My app";
  dom.window.document.getElementById("connector-callback").value = "https://app.example/callback/";
  dom.window.document.getElementById("connector-form").dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(dom.window.document.getElementById("connector-add").disabled).toBe(false));
}
describe("connector setup", () => {
  it("shows copyable settings, preserves exact callbacks and clears one-time credentials", async () => {
    const dom = boot();
    await dom.window.Connectors.load();
    await submit(dom);
    expect(dom.window.DATA.createConnector).toHaveBeenCalledWith({ name: "My app", redirect_uris: ["https://app.example/callback/"] });
    const result = dom.window.document.getElementById("connector-result");
    expect(result.querySelector('[data-secret]').value).toBe("one-time-secret");
    expect(result.textContent).toContain("HTTP Basic");
    expect(dom.window.localStorage.length).toBe(0);
    dom.window.Connectors.clearCredentials();
    expect(result.innerHTML).toBe("");
  });
  it.each(["unavailable", "denied"])("allows manual secret copy when clipboard access is %s", async (mode) => {
    const dom = boot();
    if (mode === "denied") {
      Object.defineProperty(dom.window.navigator, "clipboard", {
        value: { writeText: vi.fn().mockRejectedValue(new Error("Permission denied")) },
      });
    }
    await dom.window.Connectors.load();
    await submit(dom);
    const input = dom.window.document.querySelector("[data-secret]");
    expect(input.type).toBe("password");
    input.parentElement.querySelector("[data-copy]").click();
    await vi.waitFor(() => expect(input.type).toBe("text"));
    expect(dom.window.document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
    expect(dom.window.localStorage.length).toBe(0);
    dom.window.document.getElementById("connector-done").click();
    expect(dom.window.document.querySelector("[data-secret]")).toBeNull();
  });
  it("shows a failed save without a success card", async () => {
    const dom = boot({ createConnector: vi.fn().mockRejectedValue(new Error("Storage unavailable")) });
    await dom.window.Connectors.load();
    await submit(dom);
    expect(dom.window.document.getElementById("connector-status").textContent).toBe("Storage unavailable");
    expect(dom.window.document.getElementById("connector-result").hidden).toBe(true);
  });
  it("renders client names and callbacks as text", async () => {
    const dom = boot({ connectors: vi.fn().mockResolvedValue({ ...settings, connectors: [{ client_id: "id", name: '<img src=x onerror="alert(1)">', redirect_uris: ['https://app.example/<script>'], revoked_at: null }] }) });
    await dom.window.Connectors.load();
    expect(dom.window.document.querySelector("#connector-list img")).toBeNull();
    expect(dom.window.document.querySelector("#connector-list script")).toBeNull();
  });
});
