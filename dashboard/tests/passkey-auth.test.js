import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const dataSource = readFileSync(new URL("../redesign/data.js", import.meta.url), "utf8");
const passkeySource = readFileSync(new URL("../redesign/auth/passkey.js", import.meta.url), "utf8");
const wsSource = readFileSync(new URL("../redesign/ws.js", import.meta.url), "utf8");

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  };
}

function dataContext(fetch) {
  const redirects = [];
  const context = {
    fetch,
    history: { replaceState: vi.fn() },
    localStorage: storage(),
    location: {
      hash: "",
      pathname: "/",
      search: "",
      assign: (path) => redirects.push(path),
    },
    URLSearchParams,
    window: { SNAPSHOT: {} },
  };
  vm.createContext(context);
  vm.runInContext(dataSource, context);
  return { context, redirects };
}

describe("dashboard REST auth seam", () => {
  it("redirects a cookieless 401 to passkey sign-in when no bearer is stored", async () => {
    const fetch = vi.fn(async () => ({ ok: false, status: 401 }));
    const { context, redirects } = dataContext(fetch);

    await expect(context.window.DATA.passkeySecurity()).rejects.toThrow("sign-in required");
    expect(redirects).toEqual(["/auth/signin"]);
    expect(fetch.mock.calls[0][1].credentials).toBe("same-origin");
  });

  it("sends cookie credentials and CSRF on direct adjudication writes", async () => {
    const fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }));
    const { context } = dataContext(fetch);

    await context.window.DATA.adjudicate("fingerprint", "confirmed", null);
    const options = fetch.mock.calls[0][1];
    expect(options.credentials).toBe("same-origin");
    expect(options.headers["X-Unitares-Csrf"]).toBe("1");
  });
});

describe("passkey enrollment handoff", () => {
  it("keeps the typed one-time code in a header and session storage, never the URL", async () => {
    const sessionStorage = storage();
    const document = { open: vi.fn(), write: vi.fn(), close: vi.fn() };
    const fetch = vi.fn(async () => ({ ok: true, text: async () => "<html>enroll</html>" }));
    const history = { replaceState: vi.fn() };
    const context = {
      atob,
      btoa,
      document,
      fetch,
      history,
      localStorage: storage(),
      navigator: { credentials: {} },
      sessionStorage,
      window: { PublicKeyCredential: function () {} },
    };
    vm.createContext(context);
    vm.runInContext(passkeySource, context);

    await context.window.PasskeyUI.openEnrollment("abcde-fghij");
    expect(fetch).toHaveBeenCalledWith("/auth/enroll", expect.objectContaining({
      headers: { "X-Unitares-Enroll-Code": "ABCDEFGHIJ" },
    }));
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/auth/enroll");
    expect(sessionStorage.getItem("unitares_enroll_code")).toBe("ABCDEFGHIJ");
    expect(fetch.mock.calls[0][0]).not.toContain("ABCDEFGHIJ");
  });
});

describe("dashboard WebSocket auth order", () => {
  let instances;
  let scheduled;

  beforeEach(() => {
    instances = [];
    scheduled = [];
  });

  function socketContext(token) {
    class FakeWebSocket {
      constructor(url) {
        this.url = url;
        instances.push(this);
      }
      close() {
        if (this.onclose) this.onclose();
      }
    }
    const context = {
      Date,
      JSON,
      WebSocket: FakeWebSocket,
      encodeURIComponent,
      location: { host: "gov.cirwel.org", protocol: "https:" },
      setTimeout: (fn) => { scheduled.push(fn); return scheduled.length; },
      window: { DATA: { apiToken: () => token } },
    };
    vm.createContext(context);
    vm.runInContext(wsSource, context);
    return context;
  }

  it("tries the implicit session cookie before exposing the bearer in a fallback URL", () => {
    const context = socketContext("break-glass");
    context.window.GovSocket.make(vi.fn(), vi.fn());
    expect(instances[0].url).toBe("wss://gov.cirwel.org/ws/eisv");

    instances[0].onerror();
    expect(instances).toHaveLength(2);
    expect(instances[1].url).toBe("wss://gov.cirwel.org/ws/eisv?token=break-glass");

    instances[1].onerror();
    expect(instances).toHaveLength(2);
    expect(scheduled).toHaveLength(1);
  });

  it("never appends a query token when no break-glass token exists", () => {
    const context = socketContext(null);
    context.window.GovSocket.make(vi.fn(), vi.fn());
    instances[0].onerror();
    expect(instances).toHaveLength(1);
    expect(instances[0].url).not.toContain("token=");
    expect(scheduled).toHaveLength(1);
  });
});
