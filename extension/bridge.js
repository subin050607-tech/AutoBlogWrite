// BlogScope(내 컴퓨터 127.0.0.1) 페이지 ↔ 확장 프로그램 연결. 같은 창에서 온 BlogScope 메시지만 받는다.
(function () {
  const announce = () => window.postMessage({ type: "BLOGSCOPE_EXT_READY", version: "0.1.0" }, location.origin);
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.origin !== location.origin) return;
    const d = ev.data || {};
    if (d.type === "BLOGSCOPE_PING") announce();
    if (d.type === "BLOGSCOPE_SEND" && d.payload) {
      chrome.runtime.sendMessage({ type: "blogscope-send", payload: d.payload }, (res) => {
        window.postMessage({ type: "BLOGSCOPE_SENT", ok: !!(res && res.ok) }, location.origin);
      });
    }
  });
  announce();
  document.addEventListener("DOMContentLoaded", announce);
})();
