/* WebAuthn JSON/ArrayBuffer bridge shared by the sign-in and enrollment pages. */
(function () {
  "use strict";

  const ENROLL_STORAGE_KEY = "unitares_enroll_code";

  function decodeBase64url(value) {
    const base64 = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - base64.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    return bytes.buffer;
  }

  function encodeBase64url(value) {
    if (value == null) return null;
    const bytes = new Uint8Array(value);
    let binary = "";
    bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function decodeRequestOptions(options) {
    return Object.assign({}, options, {
      challenge: decodeBase64url(options.challenge),
      allowCredentials: (options.allowCredentials || []).map((item) => Object.assign({}, item, {
        id: decodeBase64url(item.id),
      })),
    });
  }

  function decodeCreationOptions(options) {
    return Object.assign({}, options, {
      challenge: decodeBase64url(options.challenge),
      user: Object.assign({}, options.user, { id: decodeBase64url(options.user.id) }),
      excludeCredentials: (options.excludeCredentials || []).map((item) => Object.assign({}, item, {
        id: decodeBase64url(item.id),
      })),
    });
  }

  function credentialJSON(credential) {
    const response = credential.response;
    const serialized = {
      id: credential.id,
      rawId: encodeBase64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: encodeBase64url(response.clientDataJSON),
      },
      clientExtensionResults: credential.getClientExtensionResults(),
    };
    if (credential.authenticatorAttachment) {
      serialized.authenticatorAttachment = credential.authenticatorAttachment;
    }
    if (response.attestationObject) {
      serialized.response.attestationObject = encodeBase64url(response.attestationObject);
      serialized.response.transports = response.getTransports ? response.getTransports() : [];
    } else {
      serialized.response.authenticatorData = encodeBase64url(response.authenticatorData);
      serialized.response.signature = encodeBase64url(response.signature);
      serialized.response.userHandle = response.userHandle
        ? encodeBase64url(response.userHandle)
        : null;
    }
    return serialized;
  }

  function normalizeEnrollmentCode(value) {
    return String(value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  }

  function operatorToken() {
    try { return localStorage.getItem("unitares_operator_token") || null; }
    catch { return null; }
  }

  function enrollmentCode() {
    try { return sessionStorage.getItem(ENROLL_STORAGE_KEY) || null; }
    catch { return null; }
  }

  function enrollmentHeaders(code) {
    const headers = {};
    const normalized = normalizeEnrollmentCode(code || enrollmentCode());
    if (normalized) headers["X-Unitares-Enroll-Code"] = normalized;
    const op = operatorToken();
    if (op) headers["X-Unitares-Operator"] = op;
    return headers;
  }

  async function requestJSON(path, options) {
    const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options || {}));
    const text = await response.text();
    let body;
    try { body = text ? JSON.parse(text) : {}; } catch { body = {}; }
    if (!response.ok) {
      const error = new Error(body.error || `${path} returned ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function authenticate() {
    const options = await requestJSON("/auth/webauthn/options", { method: "POST" });
    const credential = await navigator.credentials.get({
      publicKey: decodeRequestOptions(options),
    });
    if (!credential) throw new Error("No passkey response was returned.");
    return requestJSON("/auth/webauthn/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential: credentialJSON(credential) }),
    });
  }

  async function register(nickname) {
    const headers = enrollmentHeaders();
    const options = await requestJSON("/auth/webauthn/register/options", {
      method: "POST",
      headers,
    });
    const credential = await navigator.credentials.create({
      publicKey: decodeCreationOptions(options),
    });
    if (!credential) throw new Error("No passkey response was returned.");
    headers["Content-Type"] = "application/json";
    const result = await requestJSON("/auth/webauthn/register/verify", {
      method: "POST",
      headers,
      body: JSON.stringify({
        credential: credentialJSON(credential),
        nickname: String(nickname || "").trim() || undefined,
      }),
    });
    try { sessionStorage.removeItem(ENROLL_STORAGE_KEY); } catch { /* ignore */ }
    return result;
  }

  async function openEnrollment(code) {
    const normalized = normalizeEnrollmentCode(code);
    if (!normalized) throw new Error("Enter the one-time enrollment code.");
    const response = await fetch("/auth/enroll", {
      credentials: "same-origin",
      headers: { "X-Unitares-Enroll-Code": normalized },
    });
    if (!response.ok) {
      let message = "That enrollment code is invalid or expired.";
      try { message = (await response.json()).error || message; } catch { /* ignore */ }
      throw new Error(message);
    }
    const html = await response.text();
    sessionStorage.setItem(ENROLL_STORAGE_KEY, normalized);
    history.replaceState(null, "", "/auth/enroll");
    document.open();
    document.write(html);
    document.close();
  }

  function friendlyError(error) {
    if (error && error.name === "NotAllowedError") {
      return "The passkey prompt was cancelled or timed out. Try again when you’re ready.";
    }
    if (error && error.name === "InvalidStateError") {
      return "This passkey is already enrolled on this account.";
    }
    return String(error && error.message || error || "Passkey operation failed.");
  }

  window.PasskeyUI = {
    authenticate,
    enrollmentCode,
    friendlyError,
    normalizeEnrollmentCode,
    openEnrollment,
    operatorToken,
    register,
    supported: () => !!(window.PublicKeyCredential && navigator.credentials),
  };
})();
