/* Passkey and dashboard-session management. Live-only by design. */
(function () {
  "use strict";

  const mountSel = "#security-mount";
  let state = { credentials: [], sessions: [] };

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  function date(value) {
    if (!value) return "never";
    const parsed = new Date(value);
    if (isNaN(parsed)) return "unknown";
    return parsed.toLocaleString();
  }

  function shortId(value) {
    const text = String(value || "");
    return text.length > 14 ? text.slice(0, 8) + "…" + text.slice(-5) : text;
  }

  function credentialHtml(credential) {
    const nickname = credential.nickname || "Passkey";
    const transports = (credential.transports || []).join(", ") || "synced / discoverable";
    return `<div class="security-item" data-credential="${esc(credential.id)}">
      <div class="security-item-head">
        <span class="security-item-name">${esc(nickname)}</span>
        <span class="tag ok">active</span>
      </div>
      <div class="security-item-meta">${esc(shortId(credential.id))} · ${esc(transports)}<br>
        enrolled ${esc(date(credential.created_at))} · last used ${esc(date(credential.last_used_at))}</div>
      <div class="security-actions">
        <button class="security-btn danger" type="button" data-action="revoke-credential">Revoke passkey</button>
      </div>
    </div>`;
  }

  function sessionHtml(session) {
    return `<div class="security-item">
      <div class="security-item-head">
        <span class="security-item-name">${session.current ? "This browser" : "Dashboard session"}</span>
        ${session.current ? '<span class="tag ok">current</span>' : ""}
      </div>
      <div class="security-item-meta">${esc(session.user_agent || "Unknown browser")}<br>
        last seen ${esc(date(session.last_seen_at))} · expires ${esc(date(session.expires_at))}</div>
    </div>`;
  }

  function shell() {
    return `<p class="security-intro">Passkeys replace browser bearer and operator tokens. Review borrowed-device
      sessions here; revoking a passkey also revokes every session created by it.</p>
      <div class="security-grid">
        <section class="panel">
          <div class="panel-head"><h2>Passkeys</h2><span class="who" id="credentialCount"></span></div>
          <div class="security-list" id="credentialList"></div>
          <div class="security-actions">
            <button class="security-btn" id="mintCode" type="button">Mint 10-minute enrollment code</button>
            <a class="security-btn" href="/auth/signin?enroll=1" target="_blank" rel="noopener">Open enrollment</a>
          </div>
          <div id="enrollmentCode" hidden></div>
          <p class="security-note" id="operatorHint"></p>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Active sessions</h2><span class="who" id="sessionCount"></span></div>
          <div class="security-list" id="sessionList"></div>
          <div class="security-actions">
            <button class="security-btn" id="logout" type="button">Sign out here</button>
            <button class="security-btn danger" id="revokeAll" type="button">Revoke all sessions</button>
          </div>
        </section>
      </div>
      <div class="security-status" id="securityStatus" role="status" aria-live="polite"></div>`;
  }

  function message(text, kind) {
    const el = document.querySelector("#securityStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = "security-status" + (kind ? " " + kind : "");
  }

  function render() {
    const credentials = document.querySelector("#credentialList");
    const sessions = document.querySelector("#sessionList");
    document.querySelector("#credentialCount").textContent = state.credentials.length + " active";
    document.querySelector("#sessionCount").textContent = state.sessions.length + " active";
    credentials.innerHTML = state.credentials.length
      ? state.credentials.map(credentialHtml).join("")
      : '<div class="empty">No active passkeys.</div>';
    sessions.innerHTML = state.sessions.length
      ? state.sessions.map(sessionHtml).join("")
      : '<div class="empty">No active sessions.</div>';
    const opAvailable = !!window.DATA.operatorToken();
    document.querySelector("#mintCode").disabled = !opAvailable;
    document.querySelector("#operatorHint").textContent = opAvailable
      ? "Type the displayed code on the target device. It is never placed in a URL."
      : "An operator credential is required to mint a code. Existing codes can still be typed on the enrollment page.";
  }

  async function refresh() {
    try {
      state = await window.DATA.passkeySecurity();
      render();
      message("");
    } catch (error) {
      message("Passkey management requires an active dashboard session: " + String(error.message || error), "error");
      document.querySelectorAll("#security-mount button").forEach((button) => { button.disabled = true; });
    }
  }

  async function mintCode(button) {
    button.disabled = true;
    message("Minting a single-use code…");
    try {
      const result = await window.DATA.mintEnrollmentCode();
      const slot = document.querySelector("#enrollmentCode");
      slot.hidden = false;
      slot.innerHTML = `<div class="security-code">${esc(result.code)}</div>
        <p class="security-note">Expires in 10 minutes and is consumed by one successful enrollment.</p>`;
      message("Enrollment code ready. Send it out of band or type it directly on the target device.", "ok");
    } catch (error) {
      message(String(error.message || error), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function revokeCredential(button) {
    const card = button.closest("[data-credential]");
    const credentialId = card && card.getAttribute("data-credential");
    const last = state.credentials.length <= 1;
    const warning = last
      ? "This is the last passkey. A recent passkey sign-in or operator credential is required. Revoke it?"
      : "Revoke this passkey and every session created by it?";
    if (!credentialId || !window.confirm(warning)) return;
    button.disabled = true;
    try {
      await window.DATA.revokePasskey(credentialId);
      message("Passkey and its sessions revoked.", "ok");
      await refresh();
    } catch (error) {
      const text = String(error.message || error);
      if (last && text.includes("403")) {
        message("Verify your passkey again, then retry last-passkey revocation. Opening step-up sign-in…", "error");
        setTimeout(() => location.assign("/auth/signin?stepup=1"), 900);
      } else {
        message(text, "error");
        button.disabled = false;
      }
    }
  }

  async function endSessions(all) {
    const prompt = all ? "Revoke every active dashboard session, including this browser?" : "Sign out this browser?";
    if (!window.confirm(prompt)) return;
    try {
      if (all) await window.DATA.revokeAllDashboardSessions();
      else await window.DATA.logoutDashboardSession();
      location.assign("/auth/signin");
    } catch (error) {
      message(String(error.message || error), "error");
    }
  }

  function wire() {
    document.querySelector("#mintCode").addEventListener("click", (event) => mintCode(event.currentTarget));
    document.querySelector("#logout").addEventListener("click", () => endSessions(false));
    document.querySelector("#revokeAll").addEventListener("click", () => endSessions(true));
    document.querySelector("#credentialList").addEventListener("click", (event) => {
      const button = event.target.closest('[data-action="revoke-credential"]');
      if (button) revokeCredential(button);
    });
  }

  async function load() {
    const mount = document.querySelector(mountSel);
    if (!mount) return;
    mount.innerHTML = shell();
    wire();
    await refresh();
  }

  window.Security = { load };
})();
