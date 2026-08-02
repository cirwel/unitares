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
    let ws = null, retry = 0, closed = false, generation = 0;

    // A session cookie is implicit in the first handshake and keeps bearer
    // material out of URLs. Browsers hide failed-handshake status codes, so an
    // early close/error retries once with the legacy query token when one is
    // available. Every later reconnect starts cookie-first again.
    const url = (withToken) => {
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const t = withToken && window.DATA && window.DATA.apiToken && window.DATA.apiToken();
      return `${scheme}//${location.host}/ws/eisv${t ? "?token=" + encodeURIComponent(t) : ""}`;
    };
    const status = (s) => { try { onStatus(s); } catch { /* ignore */ } };

    function schedule() {
      if (closed) return;
      retry = Math.min(retry + 1, 6);
      setTimeout(() => connect(false), Math.min(1000 * 2 ** retry, 30000));
    }

    function connect(withToken) {
      if (closed) return;
      const attempt = ++generation;
      const startedAt = Date.now();
      let opened = false, ended = false;
      status("connecting");
      try { ws = new WebSocket(url(withToken)); }
      catch {
        if (!withToken && window.DATA && window.DATA.apiToken && window.DATA.apiToken()) {
          connect(true);
        } else {
          schedule();
        }
        return;
      }
      const socket = ws;

      function fallbackOrSchedule() {
        if (closed || ended || attempt !== generation) return;
        ended = true;
        const early = !opened || Date.now() - startedAt < 2500;
        const t = window.DATA && window.DATA.apiToken && window.DATA.apiToken();
        if (early && !withToken && t) {
          connect(true);
          try { socket.close(); } catch { /* old callbacks are generation-gated */ }
          return;
        }
        status("closed");
        schedule();
      }

      socket.onopen = () => {
        if (attempt !== generation) return;
        opened = true;
        retry = 0;
        status("open");
      };
      socket.onmessage = (ev) => {
        if (attempt !== generation) return;
        let msg = null;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg && msg.type) { try { onEvent(msg); } catch { /* ignore */ } }
      };
      socket.onclose = fallbackOrSchedule;
      socket.onerror = fallbackOrSchedule;
    }

    connect(false);
    return {
      close() {
        closed = true;
        generation += 1;
        try { if (ws) ws.close(); } catch { /* ignore */ }
      },
    };
  }

  window.GovSocket = { make };
})();
