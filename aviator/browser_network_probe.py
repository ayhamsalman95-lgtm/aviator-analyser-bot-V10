"""Browser-side Network API probe injected before page scripts.

The probe is intentionally diagnostic: it records request metadata and only
captures request-body previews when the URL/body looks fairness-related.
Events are pushed into the collector's existing browser event queue so normal
round collection remains unchanged.
"""

from __future__ import annotations

PROBE_JS = r"""
(() => {
  if (window.__aviatorNetworkApiProbeInstalled) return;
  window.__aviatorNetworkApiProbeInstalled = true;

  const KEY_RE = /fairness|roundfairness|serverseed|seedsha256|roundhash|lastround|last-round|roundinfo|round-information|rounddetails/i;
  const MAX_PREVIEW = 1200;

  const sha256 = async (value) => {
    try {
      const bytes = typeof value === "string"
        ? new TextEncoder().encode(value)
        : new Uint8Array(value);
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest)).map(x => x.toString(16).padStart(2, "0")).join("");
    } catch (_) {
      return null;
    }
  };

  const asText = (body) => {
    if (body === undefined || body === null) return null;
    if (typeof body === "string") return body;
    if (body instanceof URLSearchParams) return body.toString();
    if (body instanceof ArrayBuffer) return "[ArrayBuffer:" + body.byteLength + "]";
    if (ArrayBuffer.isView(body)) return "[" + Object.prototype.toString.call(body) + ":" + body.byteLength + "]";
    if (body instanceof Blob) return "[Blob:" + body.size + "]";
    try { return JSON.stringify(body); } catch (_) { return String(body); }
  };

  const push = (data) => {
    try {
      const item = {
        t: Date.now(),
        event_type: "browserNetworkApi",
        data
      };
      if (Array.isArray(window.__aviatorBuf)) {
        if (window.__aviatorBuf.length < 2000) window.__aviatorBuf.push(item);
      } else {
        window.__aviatorNetworkApiPending = window.__aviatorNetworkApiPending || [];
        if (window.__aviatorNetworkApiPending.length < 200) {
          window.__aviatorNetworkApiPending.push(item);
        }
      }
    } catch (_) {}
  };

  const flushPending = () => {
    try {
      if (!Array.isArray(window.__aviatorBuf) || !Array.isArray(window.__aviatorNetworkApiPending)) return;
      while (window.__aviatorNetworkApiPending.length && window.__aviatorBuf.length < 2000) {
        window.__aviatorBuf.push(window.__aviatorNetworkApiPending.shift());
      }
    } catch (_) {}
  };
  setInterval(flushPending, 250);

  const record = async (kind, method, url, body, extra = {}) => {
    try {
      const bodyText = asText(body);
      const haystack = String(url || "") + " " + String(bodyText || "");
      const relevant = KEY_RE.test(haystack);
      const bodySize = bodyText == null ? 0 : bodyText.length;
      const preview = relevant && bodyText != null ? bodyText.slice(0, MAX_PREVIEW) : null;
      const bodyHash = bodyText != null ? await sha256(bodyText) : null;
      push({
        api: kind,
        method: String(method || "GET").toUpperCase(),
        url: String(url || ""),
        relevant,
        body_size: bodySize,
        body_sha256: bodyHash,
        body_preview: preview,
        ...extra
      });
    } catch (_) {}
  };

  // fetch()
  try {
    const originalFetch = window.fetch;
    if (typeof originalFetch === "function") {
      window.fetch = function(input, init) {
        let url = "";
        let method = "GET";
        let body = null;
        try {
          if (typeof input === "string") {
            url = input;
          } else if (input && input.url) {
            url = input.url;
            method = input.method || method;
          }
          if (init) {
            method = init.method || method;
            body = init.body;
          }
        } catch (_) {}
        const started = Date.now();
        record("fetch", method, url, body, {phase: "request", started_at: started});
        return originalFetch.apply(this, arguments).then((response) => {
          push({
            api: "fetch",
            phase: "response",
            method: String(method || "GET").toUpperCase(),
            url: String(url || response.url || ""),
            status: response.status,
            ok: response.ok,
            elapsed_ms: Date.now() - started
          });
          return response;
        });
      };
    }
  } catch (_) {}

  // XMLHttpRequest
  try {
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
      try {
        this.__aviatorProbeMethod = method;
        this.__aviatorProbeUrl = url;
      } catch (_) {}
      return open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
      try {
        record("xhr", this.__aviatorProbeMethod || "GET", this.__aviatorProbeUrl || "", body, {phase: "request"});
        this.addEventListener("loadend", () => {
          try {
            push({
              api: "xhr",
              phase: "response",
              method: String(this.__aviatorProbeMethod || "GET").toUpperCase(),
              url: String(this.__aviatorProbeUrl || this.responseURL || ""),
              status: this.status
            });
          } catch (_) {}
        }, {once: true});
      } catch (_) {}
      return send.apply(this, arguments);
    };
  } catch (_) {}

  // WebSocket.send()
  try {
    const originalSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function(data) {
      try {
        const url = this.url || "";
        const bodyText = asText(data);
        const relevant = KEY_RE.test(String(url) + " " + String(bodyText || ""));
        const size = typeof data === "string"
          ? data.length
          : (data && (data.byteLength || data.size)) || 0;
        const binaryBytes = data instanceof ArrayBuffer
          ? new Uint8Array(data)
          : (ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : null);
        const binaryBase64 = binaryBytes
          ? btoa(Array.from(binaryBytes, x => String.fromCharCode(x)).join(""))
          : null;
        const binaryHash = binaryBytes ? await sha256(binaryBytes) : null;
        push({
          api: "websocket",
          phase: "send",
          method: "WS",
          url: String(url),
          relevant,
          data_type: typeof data === "string" ? "text" : Object.prototype.toString.call(data),
          body_size: size,
          body_sha256: binaryHash || (typeof data === "string" ? await sha256(data) : null),
          body_preview: relevant && bodyText != null ? bodyText.slice(0, MAX_PREVIEW) : null,
          body_base64: binaryBase64
        });
      } catch (_) {}
      return originalSend.apply(this, arguments);
    };
  } catch (_) {}
})();
"""


def install() -> None:
    """Patch Playwright BrowserContext.add_init_script to include the probe."""
    try:
        from playwright.async_api import BrowserContext
    except Exception:
        return

    if getattr(BrowserContext, "__aviator_network_probe_patched", False):
        return

    original = BrowserContext.add_init_script

    async def patched(self, script=None, path=None):
        try:
            await original(self, script=PROBE_JS)
        except TypeError:
            await original(self, PROBE_JS)
        if path is not None:
            return await original(self, path=path)
        if script is not None:
            try:
                return await original(self, script=script)
            except TypeError:
                return await original(self, script)
        return None

    BrowserContext.add_init_script = patched
    BrowserContext.__aviator_network_probe_patched = True
