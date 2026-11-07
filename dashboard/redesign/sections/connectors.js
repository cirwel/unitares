/* Operator-managed OAuth connectors. Live-only; credentials never enter storage. */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  let settings = null;
  const el = (id) => document.getElementById(id);
  function message(text, error) {
    el("connector-status").textContent = text;
    el("connector-status").className = "security-status" + (error ? " error" : "");
  }
  function field(label, value, secret) {
    return `<label class="security-secret-label">${esc(label)}
      <span class="security-actions security-secret-row">
        <input class="security-secret" readonly type="${secret ? "password" : "text"}"
          value="${esc(value)}" autocomplete="off" spellcheck="false" ${secret ? 'data-secret="1"' : ""}>
        <button class="security-btn" type="button" data-copy>Copy ${esc(label.toLowerCase())}</button>
      </span></label>`;
  }
  function clearSecret() {
    const result = el("connector-result");
    if (result) { result.replaceChildren(); result.hidden = true; }
  }
  async function refresh() {
    try {
      settings = await window.DATA.connectors();
      el("connector-list").innerHTML = settings.connectors.length ? settings.connectors.map((item) =>
        `<div class="security-item"><strong>${esc(item.name)}</strong>
          <span class="tag">${item.revoked_at ? "disabled" : "enabled"}</span>
          <div class="security-item-meta">${item.redirect_uris.map(esc).join("<br>")}</div>
          ${item.revoked_at ? "" : `<button class="security-btn danger" type="button" data-revoke="${esc(item.client_id)}">Disable</button>`}
        </div>`).join("") : '<p class="empty">No connectors added yet.</p>';
      el("connector-legacy").textContent = settings.legacy_client_configured
        ? "A client configured through the server environment is also available. It is managed separately." : "";
      el("connector-add").disabled = false;
    } catch (error) {
      settings = null;
      el("connector-add").disabled = true;
      el("connector-list").textContent = "Connector list unavailable.";
      message(error.message, true);
    }
  }
  async function create(event) {
    event.preventDefault();
    clearSecret();
    el("connector-add").disabled = true;
    try {
      const created = await window.DATA.createConnector({
        name: el("connector-name").value.trim(),
        redirect_uris: el("connector-callback").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
      });
      const result = el("connector-result");
      result.innerHTML = `<h2 tabindex="-1">Paste these into your app</h2>
        <p>Your connector is saved and ready to authorize. The client secret is shown only here; save it before leaving.</p>
        ${field("Server URL", settings.server_url)}
        ${field("Client ID", created.client_id)}
        ${field("Client secret", created.client_secret, true)}
        ${field("Token authentication method", settings.token_auth_method)}
        <p class="security-note">Choose HTTP Basic if your app uses that name.</p>
        <details><summary>Only if your app asks: endpoints and scopes</summary>
        ${field("Authorization endpoint", settings.authorization_endpoint)}
        ${field("Token endpoint", settings.token_endpoint)}${field("Scopes", settings.scopes)}</details>
        <p class="security-note">Return to your app and start its connection again. If you lose the secret, disable this connector and add a replacement.</p>
        <button class="security-btn" type="button" id="connector-done">Done — clear credentials</button>`;
      result.hidden = false;
      result.querySelector("h2").focus();
      result.scrollIntoView?.({ behavior: "smooth", block: "start" });
      el("connector-done").addEventListener("click", clearSecret);
      event.target.reset();
      message("Connector saved. No server restart is needed.");
      await refresh();
    } catch (error) { message(error.message, true); }
    finally { el("connector-add").disabled = !settings; }
  }
  async function click(event) {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const input = copy.parentElement.querySelector("input");
      try { await navigator.clipboard.writeText(input.value); message("Copied."); }
      catch {
        // Password inputs cannot be copied manually in some browsers.
        input.type = "text";
        input.focus();
        input.select();
        message("Clipboard unavailable. The field is visible and selected; copy it manually, then choose Done to clear credentials.");
      }
    }
    const button = event.target.closest("[data-revoke]");
    if (button && window.confirm("Disable this connector? Its existing tokens will stop working too.")) {
      button.disabled = true;
      try {
        await window.DATA.revokeConnector(button.dataset.revoke);
        clearSecret();
        await refresh();
        message("Connector disabled.");
      } catch (error) { button.disabled = false; message(error.message, true); }
    }
  }
  async function load() {
    const mount = el("connectors-mount");
    if (!mount) return;
    mount.innerHTML = `<p class="security-intro">Connect an app to this UNITARES server. Each connector gets its own credentials and can be disabled separately.</p>
      <p class="security-note">Sign in recently to manage connectors, or provide your operator credential in <a href="#security">Security</a>.
        <a href="/auth/signin?stepup=1">Sign in again</a> · <button class="security-btn" id="connector-refresh" type="button">Refresh</button></p>
      <div class="security-grid"><section class="panel"><h2>Add connector</h2>
        <form id="connector-form">
          <label class="security-secret-label" for="connector-name">Name</label>
          <input class="security-secret" id="connector-name" required maxlength="100" placeholder="A name you will recognize">
          <label class="security-secret-label" for="connector-callback">Callback URL</label>
          <textarea class="security-secret" id="connector-callback" required rows="3" spellcheck="false" placeholder="https://your-app.example/oauth/callback"></textarea>
          <p class="security-note">Copy the redirect or callback URL shown by the app you are connecting. This is the app’s return address, not your UNITARES server URL. Paste it exactly, including any trailing slash. Use one URL per line if the app supplies several.</p>
          <button class="security-btn" id="connector-add" type="submit" disabled>Create credentials</button>
        </form></section>
        <section class="panel"><h2>Saved connectors</h2><div id="connector-list" class="security-list"></div>
          <p class="security-note" id="connector-legacy"></p></section></div>
      <div id="connector-status" class="security-status" role="status" aria-live="polite"></div>
      <section class="panel" id="connector-result" hidden></section>`;
    el("connector-form").addEventListener("submit", create);
    el("connector-refresh").addEventListener("click", () => { message(""); refresh(); });
    mount.addEventListener("click", click);
    await refresh();
  }
  window.addEventListener("hashchange", () => { if (location.hash !== "#connectors") clearSecret(); });
  window.addEventListener("pagehide", clearSecret);
  window.Connectors = { load, clearCredentials: clearSecret };
})();
