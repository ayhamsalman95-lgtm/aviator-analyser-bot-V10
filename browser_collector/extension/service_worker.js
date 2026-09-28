const DEFAULTS = {
  endpoint: "",
  token: ""
};

async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return {
    endpoint: String(stored.endpoint || "").trim().replace(/\/$/, ""),
    token: String(stored.token || "").trim()
  };
}

async function upload(event) {
  const settings = await getSettings();
  if (!settings.endpoint || !settings.token) {
    return { ok: false, skipped: true };
  }

  const response = await fetch(settings.endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + settings.token
    },
    body: JSON.stringify({ events: [event] })
  });

  if (!response.ok) {
    throw new Error("upload failed: " + response.status);
  }

  return await response.json();
}

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(DEFAULTS);
  await chrome.storage.local.set(existing);
});

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "sfs_event") return;
  upload(message.event).catch((error) => {
    console.warn("Aviator Browser Collector:", error);
  });
});
