(() => {
  const SOURCE = "aviator-browser-collector";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== SOURCE || data.kind !== "sfs_event") return;
    if (!data.event || data.event.event_type !== "extensionResponse") return;

    chrome.runtime.sendMessage({
      type: "sfs_event",
      event: data.event
    }).catch(() => {});
  });
})();
