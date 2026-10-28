// Connects the LiveView socket. The two libraries load first, from
// /assets/vendor (served from the pinned dependencies, see endpoint.ex).
(function () {
  "use strict";
  var meta = document.querySelector("meta[name='csrf-token']");
  if (!meta || !window.Phoenix || !window.LiveView) return;

  var liveSocket = new window.LiveView.LiveSocket("/live", window.Phoenix.Socket, {
    params: { _csrf_token: meta.getAttribute("content") },
  });
  liveSocket.connect();
  window.liveSocket = liveSocket;
})();
