/*
 * Adjudication section — the daily ground-truth ritual.
 *
 * Surfaces a small unadjudicated slice of the Sentinel backlog with one-click
 * operator verdicts (confirm / false-positive / dismiss-with-reason). Each
 * verdict posts /v1/sentinel/adjudicate → an external_signal outcome_event
 * attributed to Sentinel's UUID — the label feed for the EISV §6.3
 * residual-vs-Φ falsifier.
 *
 * Deliberately small queue: outcomes join to the last prior state snapshot,
 * so a batch sweep collapses into ONE statistical cluster. A few verdicts a
 * day on separate days is worth more than fifty in one sitting — the progress
 * strip tracks exactly that (independent days, bad-label days vs target).
 *
 * Writes require the operator credential (X-Unitares-Operator); the data
 * layer picks it up from ?operator_token= (persisted + scrubbed) or
 * localStorage. Without it the section still renders read-only with a hint.
 */
(function () {
  "use strict";

  const mountSel = "#adj-mount";
  let mounted = false;

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  function age(ts) {
    const d = new Date(ts);
    if (isNaN(d)) return "";
    const h = Math.max(0, (Date.now() - d.getTime()) / 36e5);
    if (h < 1) return Math.round(h * 60) + "m ago";
    if (h < 48) return Math.round(h) + "h ago";
    return Math.round(h / 24) + "d ago";
  }

  function progressHtml(p) {
    if (!p) return "";
    const badDays = p.bad_days ?? 0, target = p.bad_days_target ?? 3;
    const onTarget = badDays >= target;
    return `
      <div class="adj-progress">
        <span class="stat"><b>${p.outcomes ?? 0}</b> labels</span>
        <span class="stat"><b>${p.bad ?? 0}</b> bad</span>
        <span class="stat"><b>${p.days ?? 0}</b> independent days</span>
        <span class="stat ${onTarget ? "ok" : ""}"><b>${badDays}/${target}</b> bad-label days</span>
        <span class="hint">independent days = statistical power; verdicts on separate days beat batches</span>
      </div>`;
  }

  function cardHtml(f, reasons) {
    const opts = (reasons || [])
      .filter((r) => r !== "fp")
      .map((r) => `<option value="${esc(r)}">${esc(r.replace(/_/g, " "))}</option>`)
      .join("");
    return `
      <div class="adj-card" data-fp="${esc(f.fingerprint)}">
        <div class="adj-head">
          <span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span>
          <span class="type">${esc(f.finding_type || "")}</span>
          <span class="when">${esc(age(f.timestamp))}</span>
        </div>
        <div class="adj-msg">${esc(f.message || "(no message)")}</div>
        <div class="adj-actions">
          <button class="btn confirm" data-act="confirm">✓ Confirm — Sentinel was right</button>
          <button class="btn fp" data-act="fp">✗ False positive</button>
          <span class="dismiss-group">
            <select class="reason">${opts}</select>
            <button class="btn dismiss" data-act="dismiss">Dismiss</button>
          </span>
        </div>
        <div class="adj-note" hidden></div>
      </div>`;
  }

  function shell() {
    return `
      <div class="adj-wrap">
        <div class="adj-top">
          <h2>Adjudication <span class="src-badge" id="adj-src"></span></h2>
          <span id="adj-pending" class="pending"></span>
        </div>
        <p class="adj-blurb">Your verdict on each finding becomes an external-truth
        label attributed to Sentinel — the ground truth the EISV falsifier needs.
        A few per day, on separate days, is the ideal cadence.</p>
        <div id="adj-progress-slot"></div>
        <div id="adj-token-hint" class="adj-token-hint" hidden>
          Operator token missing — open the dashboard once with
          <code>?operator_token=…</code> to enable verdicts (it persists locally).
        </div>
        <div id="adj-queue"></div>
      </div>`;
  }

  async function verdict(card, status, reason) {
    const fp = card.getAttribute("data-fp");
    const note = card.querySelector(".adj-note");
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const r = await window.DATA.adjudicate(fp, status, reason);
      note.hidden = false;
      note.textContent = status === "confirmed"
        ? "Recorded: confirmed (good label)."
        : reason === "fp"
          ? "Recorded: false positive (bad label)."
          : `Recorded: dismissed (${reason}) — valid finding, not actioned.`;
      card.classList.add("done");
      if (r && r.progress) {
        const slot = document.querySelector("#adj-progress-slot");
        if (slot) slot.innerHTML = progressHtml(r.progress);
      }
      setTimeout(() => { card.remove(); refreshPendingCount(-1); }, 900);
    } catch (e) {
      note.hidden = false;
      const msg = String(e && e.message || e);
      if (msg.includes("403")) {
        note.textContent = "Operator credential required — see the token hint above.";
        const hint = document.querySelector("#adj-token-hint");
        if (hint) hint.hidden = false;
      } else if (msg.includes("409")) {
        note.textContent = "Already adjudicated elsewhere — removing.";
        setTimeout(() => card.remove(), 900);
      } else {
        note.textContent = "Failed: " + msg;
      }
      card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  }

  function refreshPendingCount(delta) {
    const el = document.querySelector("#adj-pending");
    if (!el) return;
    const m = /(\d+)/.exec(el.textContent || "");
    if (m) {
      const n = Math.max(0, parseInt(m[1], 10) + delta);
      el.textContent = n + " pending";
    }
  }

  async function load() {
    const mount = document.querySelector(mountSel);
    if (!mount) return;
    if (!mounted) { mount.innerHTML = shell(); mounted = true; }

    const { source, data } = await window.DATA.adjudicationQueue();
    const badge = document.querySelector("#adj-src");
    if (badge) { badge.textContent = source; badge.className = "src-badge " + source; }

    const slot = document.querySelector("#adj-progress-slot");
    if (slot) slot.innerHTML = progressHtml(data.progress);
    const pending = document.querySelector("#adj-pending");
    if (pending) pending.textContent = (data.pending_total ?? 0) + " pending";
    const hint = document.querySelector("#adj-token-hint");
    if (hint) hint.hidden = !!window.DATA.operatorToken();

    const queueEl = document.querySelector("#adj-queue");
    if (!queueEl) return;
    const items = data.queue || [];
    if (!items.length) {
      queueEl.innerHTML = `<div class="adj-empty">Queue clear — nothing awaiting a verdict.
        New Sentinel findings land here as they fire. Come back tomorrow.</div>`;
      return;
    }
    queueEl.innerHTML = items.map((f) => cardHtml(f, data.dismiss_reasons)).join("");
    queueEl.querySelectorAll(".adj-card").forEach((card) => {
      card.querySelector('[data-act="confirm"]').addEventListener("click", () => verdict(card, "confirmed", null));
      card.querySelector('[data-act="fp"]').addEventListener("click", () => verdict(card, "dismissed", "fp"));
      card.querySelector('[data-act="dismiss"]').addEventListener("click", () => {
        const sel = card.querySelector("select.reason");
        verdict(card, "dismissed", sel ? sel.value : "unclear");
      });
    });
  }

  window.Adjudication = { load };
})();
