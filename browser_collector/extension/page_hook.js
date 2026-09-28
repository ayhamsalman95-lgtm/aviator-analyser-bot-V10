(() => {
  if (window.__aviatorBrowserCollectorHooked) return;
  window.__aviatorBrowserCollectorHooked = true;

  const seen = new WeakSet();

  const snap = (v, depth = 0) => {
    if (depth > 6) return null;
    if (v === null || v === undefined) return v;

    const type = typeof v;
    if (type === "string" || type === "number" || type === "boolean") return v;
    if (type === "bigint") return String(v);
    if (type !== "object") return null;
    if (seen.has(v)) return null;
    seen.add(v);

    try {
      if (typeof v.getKeysArray === "function") {
        const out = {};
        for (const key of (v.getKeysArray() || []).slice(0, 200)) {
          const name = String(key);
          if (/^(password|passwd|token|authorization|cookie|secret|session)$/i.test(name)) continue;
          try { out[name] = snap(v.get(name), depth + 1); } catch (_) {}
        }
        return out;
      }

      if (typeof v.size === "function" && typeof v.get === "function") {
        const out = [];
        for (let i = 0; i < Math.min(v.size(), 500); i++) {
          try { out.push(snap(v.get(i), depth + 1)); } catch (_) {}
        }
        return out;
      }
    } catch (_) {}

    if (Array.isArray(v)) return v.slice(0, 500).map(x => snap(x, depth + 1));

    const out = {};
    for (const key of Object.keys(v).slice(0, 200)) {
      if (/^(password|passwd|token|authorization|cookie|secret|session)$/i.test(key)) continue;
      try { out[key] = snap(v[key], depth + 1); } catch (_) {}
    }
    return out;
  };

  const emit = (data) => {
    try {
      window.postMessage({
        source: "aviator-browser-collector",
        kind: "sfs_event",
        event: {
          t: Date.now(),
          event_type: "extensionResponse",
          data
        }
      }, location.origin);
    } catch (_) {}
  };

  const hook = () => {
    try {
      const SmartFox = window.SFS2X && window.SFS2X.SmartFox;
      if (!SmartFox || !SmartFox.prototype ||
          typeof SmartFox.prototype.dispatchEvent !== "function") {
        return false;
      }

      if (SmartFox.prototype.__aviatorBrowserCollectorHooked) return true;

      const original = SmartFox.prototype.dispatchEvent;
      SmartFox.prototype.dispatchEvent = function(evt) {
        try {
          if (String(evt && evt.type || "") === "extensionResponse") {
            emit(snap(evt));
          }
        } catch (_) {}

        return original.apply(this, arguments);
      };

      SmartFox.prototype.__aviatorBrowserCollectorHooked = true;
      return true;
    } catch (_) {
      return false;
    }
  };

  hook();
  let tries = 0;
  const timer = setInterval(() => {
    tries += 1;
    if (hook() || tries > 240) clearInterval(timer);
  }, 500);
})();
