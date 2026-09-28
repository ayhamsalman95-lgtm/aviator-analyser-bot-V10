(() => {
  const SOURCE = "aviator-browser-collector";

  const runtime = globalThis.chrome && globalThis.chrome.runtime;
  if (!runtime || typeof runtime.sendMessage !== "function") {
    console.warn("Aviator Browser Collector: chrome.runtime API unavailable");
    return;
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== SOURCE || data.kind !== "sfs_event") return;
    if (!data.event || data.event.event_type !== "extensionResponse") return;

    try {
      runtime.sendMessage({
        type: "sfs_event",
        event: data.event
      }).catch(() => {});
    } catch (_) {}
  });
})();
