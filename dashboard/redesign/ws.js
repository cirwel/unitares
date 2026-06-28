/*
 * Governance event stream — /ws/eisv.
 * --------------------------------------------------------
 * Thin WebSocket client: the server pushes {type:"eisv_update", …} on every
 * agent check-in and {type:<event>, …} for governance events (lifecycle,
 * knowledge, circuit-breaker…). The full event object is handed to the consumer
 * (app.html onWsEvent). Sections that can patch from the payload do so directly
 * (true diff-push, e.g. the EISV chart re-buckets in place); everything else
 * treats the event as a doorbell and does a debounced refresh of the active view
 * through the normal render path. Real-time (sub-second) either way.
 *
 * Reconnects with capped exponential backoff. Status is reported so the header
 * pill can show streaming vs the polling fallback.
 */
(function () {
  "use strict";

  function make(onEvent, onStatus) {
    let ws = null, retry = 0, closed = false;

    const url = () => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/eisv`;
    const status = (s) => { try { onStatus(s); } catch { /* ignore */ } };

    function schedule() {
      if (closed) return;
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, Math.min(1000 * 2 ** retry, 30000));
    }

    function connect() {
      if (closed) return;
      status("connecting");
      try { ws = new WebSocket(url()); }
      catch { schedule(); return; }

      ws.onopen = () => { retry = 0; status("open"); };
      ws.onmessage = (ev) => {
        let msg = null;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg && msg.type) { try { onEvent(msg); } catch { /* ignore */ } }
      };
      ws.onclose = () => { status("closed"); schedule(); };
      ws.onerror = () => { try { ws.close(); } catch { /* onclose handles retry */ } };
    }

    connect();
    return { close() { closed = true; try { if (ws) ws.close(); } catch { /* ignore */ } } };
  }

  window.GovSocket = { make };
})();
