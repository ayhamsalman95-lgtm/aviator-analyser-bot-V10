"""Chrome collector V10 for real Aviator rounds and Provably-Fair evidence.

V8 uses Aviator roundChartInfo.maxMultiplier + roundId as the authoritative completed-round result. Cashout/bet payloads are never treated as round results.\n\nThe collector does two separate jobs:
1) read the real round/multiplier stream from Chrome;
2) capture and associate seed/hash evidence without fabricating values.

Important: a server-seed reveal can arrive after OnCrash, so seed enrichment is
applied back to the already-saved round instead of being discarded.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Response
from playwright.async_api import async_playwright

try:
    from sfs2x import decode_s2c_packet, parse_s2c_command
    SFS_DECODER_AVAILABLE = True
except Exception:
    decode_s2c_packet = None
    parse_s2c_command = None
    SFS_DECODER_AVAILABLE = False

from fairness import verify_round
from seeds import (
    extract_hex_from_text,
    extract_seed_fields,
    merge_seed_state,
    noisy_url,
    parse_html_attributes,
    parse_labeled_text,
)
from store import (
    BASE,
    CONFIG,
    append_diagnostic,
    append_seed_log,
    last_round,
    load_rounds,
    save_live,
    set_status,
    upsert_round,
)

# The project is an Aviator collector.  The previous fallback pointed to a
# generic Crash page, which could make the collector monitor the wrong game.
DEFAULT_AVIATOR_URL = "https://1xlite-130003.top/ar/casino-search?game=52358"
SITE_HOME_URL = "https://1xlite-130003.top/ar/"
LOGIN_WAIT_SECONDS = 180


def normalize_game_url(raw_url: Any) -> str:
    """Return a usable Aviator URL and prevent the old Crash fallback."""
    value = str(raw_url or "").strip()
    if not value:
        return DEFAULT_AVIATOR_URL

    try:
        p = urlsplit(value)
        path = (p.path or "").lower().rstrip("/")
        # Explicitly replace the old generic Crash fallback.
        if path.endswith("/games/crash"):
            return DEFAULT_AVIATOR_URL
    except Exception:
        return DEFAULT_AVIATOR_URL

    return value


GAME_URL = normalize_game_url(CONFIG.get("game_url"))
PROFILE = BASE / "chrome_profile"
NETWORK_LOG = BASE / "game_network.jsonl"
DIAGNOSTICS_LOG = BASE / "diagnostics.jsonl"
MAX_LOG_PAYLOAD = 400_000
MAX_HTML = 120_000
NUM_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*x?\s*$", re.I)

GAME_HOST_HINTS = {
    "1xlite-130003.top",
    "1xbet.com",
}

SIGNALR_TARGETS = {
    "OnCrash", "OnStart", "OnStage", "OnBetting", "OnRegistration",
    "OnCashouts", "OnBets", "OnProfits", "OnUpdate", "OnTops",
}

SECRET_QUERY_KEYS = re.compile(
    r"(?i)^(token|access_token|refresh_token|authorization|auth|password|pass|secret|signature|sig|cookie|session|sid)$"
)
SECRET_JSON_KEYS = re.compile(
    r"(?i)(password|passwd|token|access_token|refresh_token|authorization|cookie|set-cookie|secret)"
)

INJECT_JS = r"""
(() => {
  if (window.__aviatorHooked) return;
  window.__aviatorHooked = true;
  window.__aviatorBuf = [];
  const push = (item) => {
    try {
      window.__aviatorBuf.push(item);
      if (window.__aviatorBuf.length > 1200) window.__aviatorBuf.splice(0, 600);
    } catch (e) {}
  };

  const wrap = (Orig) => {
    if (!Orig) return Orig;
    const WS = function(url, protocols) {
      const ws = protocols !== undefined ? new Orig(url, protocols) : new Orig(url);
      ws.addEventListener("message", (ev) => {
        let data;
        try {
          data = typeof ev.data === "string" ? ev.data
            : (ev.data && ev.data.byteLength ? `[binary:${ev.data.byteLength}]` : String(ev.data));
        } catch (e) { data = String(ev.data); }
        push({ t: Date.now(), dir: "in", url: String(url), data: data.slice(0, 500000) });
      });
      const origSend = ws.send.bind(ws);
      ws.send = function(data) {
        let text;
        try {
          text = typeof data === "string" ? data
            : (data && data.byteLength ? `[binary:${data.byteLength}]` : String(data));
        } catch (e) { text = String(data); }
        push({ t: Date.now(), dir: "out", url: String(url), data: text.slice(0, 300000) });
        return origSend(data);
      };
      return ws;
    };
    try { WS.prototype = Orig.prototype; } catch (e) {}
    try {
      WS.CONNECTING = Orig.CONNECTING; WS.OPEN = Orig.OPEN;
      WS.CLOSING = Orig.CLOSING; WS.CLOSED = Orig.CLOSED;
    } catch (e) {}
    return WS;
  };
  try { window.WebSocket = wrap(window.WebSocket); } catch (e) {}

  // Capture fetch/XHR text that Playwright may not classify as a normal response
  // body (for example a blob/json endpoint used only inside an iframe).
  try {
    const origFetch = window.fetch;
    window.fetch = async function(...args) {
      const response = await origFetch.apply(this, args);
      try {
        const clone = response.clone();
        const text = await clone.text();
        push({ t: Date.now(), dir: "fetch-response", url: String(response.url || (args[0] && args[0].url) || args[0] || ""), status: response.status, data: text.slice(0, 400000) });
      } catch (e) {}
      return response;
    };
  } catch (e) {}

  try {
    const XHR = XMLHttpRequest.prototype;
    const open = XHR.open;
    const send = XHR.send;
    XHR.open = function(method, url, ...rest) {
      this.__aviatorMethod = method;
      this.__aviatorUrl = String(url);
      return open.call(this, method, url, ...rest);
    };
    XHR.send = function(body) {
      try {
        this.addEventListener("load", () => {
          let text = "";
          try { text = String(this.responseText || ""); } catch (e) {}
          push({ t: Date.now(), dir: "xhr-response", method: this.__aviatorMethod, url: this.__aviatorUrl, status: this.status, data: text.slice(0, 400000) });
        });
      } catch (e) {}
      return send.call(this, body);
    };
  } catch (e) {}

  // SmartFoxServer HTML5 clients decode the wire protocol inside the page
  // and then dispatch high-level events. Hook dispatchEvent as well, so the
  // collector can observe those already-decoded events instead of depending
  // only on raw websocket JSON.
  try {
    const seen = new WeakSet();
    const snapshot = (value, depth = 0) => {
      if (depth > 5) return "<depth>";
      if (value === null || value === undefined) return value;
      const typ = typeof value;
      if (typ === "string" || typ === "number" || typ === "boolean") return value;
      if (typ === "bigint") return String(value);
      if (typ === "function") return "<function>";
      if (typ !== "object") return String(value);
      if (value instanceof Uint8Array) {
        return { _binary: true, length: value.byteLength, hex: Array.from(value.slice(0, 256)).map(x => x.toString(16).padStart(2, "0")).join("") };
      }
      if (seen.has(value)) return "<cycle>";
      seen.add(value);
      if (Array.isArray(value)) return value.slice(0, 120).map(v => snapshot(v, depth + 1));

      // SFSObject/SFSArray expose key accessors in the JavaScript API.
      try {
        if (typeof value.getKeysArray === "function") {
          const out = {};
          const keys = value.getKeysArray() || [];
          for (const k of keys.slice(0, 120)) {
            try { out[String(k)] = snapshot(value.get(k), depth + 1); } catch (e) {}
          }
          return out;
        }
      } catch (e) {}

      const out = {};
      let count = 0;
      for (const k of Object.keys(value)) {
        if (count++ >= 120) break;
        if (/^(password|passwd|token|access_token|refresh_token|authorization|cookie|secret|signature|sig)$/i.test(k)) {
          out[k] = "<redacted>";
          continue;
        }
        try { out[k] = snapshot(value[k], depth + 1); } catch (e) {}
      }
      return out;
    };

    let lastState = false;
    const tryHookSFS = () => {
      try {
        const ns = window.SFS2X;
        const C = ns && ns.SmartFox;
        if (!C || !C.prototype || typeof C.prototype.dispatchEvent !== "function") return false;
        if (C.prototype.__aviatorDispatchHooked) return true;
        const origDispatch = C.prototype.dispatchEvent;
        C.prototype.dispatchEvent = function(evt) {
          try {
            push({
              t: Date.now(),
              dir: "sfs-event",
              event_type: String(evt && evt.type || ""),
              data: snapshot(evt),
            });
          } catch (e) {}
          return origDispatch.apply(this, arguments);
        };
        C.prototype.__aviatorDispatchHooked = true;
        if (!lastState) {
          push({ t: Date.now(), dir: "sfs-hook", status: "hooked" });
          lastState = true;
        }
        return true;
      } catch (e) {
        return false;
      }
    };

    tryHookSFS();
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (tryHookSFS() || tries > 240) clearInterval(timer);
    }, 500);
  } catch (e) {}
})();
"""

SCRAPE_JS = r"""
() => {
  const text = (document.body && document.body.innerText) ? document.body.innerText.slice(0, 30000) : "";
  const html = (document.documentElement && document.documentElement.innerHTML) ? document.documentElement.innerHTML.slice(0, 120000) : "";
  const labeled = [];
  const attrs = [];
  const meta = [];
  const scripts = [];
  const frames = [];

  const nodes = document.querySelectorAll("[class*='seed' i], [class*='hash' i], [class*='fair' i], [class*='nonce' i], [class*='provably' i], [id*='seed' i], [id*='hash' i], [id*='fair' i], [id*='nonce' i], [data-seed], [data-seed-hash], [data-server-seed], [data-client-seed]");
  nodes.forEach((el) => {
    const t = (el.innerText || el.textContent || "").trim();
    if (t && t.length < 1000) labeled.push({ cls: String(el.className || ""), id: String(el.id || ""), text: t });
    try {
      for (const a of Array.from(el.attributes || [])) {
        const name = String(a.name || "");
        const val = String(a.value || "");
        if (/seed|hash|nonce|fair|provably/i.test(name)) attrs.push({ name, value: val.slice(0, 1000) });
      }
    } catch (e) {}
  });

  document.querySelectorAll("meta[name], meta[property], input[name], input[id], [data-seed], [data-seed-hash], [data-server-seed], [data-client-seed], [data-nonce]").forEach((el) => {
    try {
      const name = el.getAttribute("name") || el.getAttribute("property") || el.getAttribute("id") || el.getAttribute("data-seed") || "";
      const value = el.getAttribute("content") || el.getAttribute("value") || el.getAttribute("data-value") || el.getAttribute("data-seed") || el.textContent || "";
      if (/seed|hash|nonce|fair|provably/i.test(String(name))) meta.push({ name: String(name), value: String(value).slice(0, 1000) });
    } catch (e) {}
  });

  document.querySelectorAll("script").forEach((el) => {
    try {
      const t = String(el.textContent || "");
      if (/seed|serverseed|clientseed|provably|fair|nonce/i.test(t)) scripts.push(t.slice(0, 20000));
    } catch (e) {}
  });

  document.querySelectorAll("iframe").forEach((el) => {
    try { frames.push(String(el.src || "")); } catch (e) {}
  });

  const storage = {};
  try {
    for (const storeName of ["localStorage", "sessionStorage"]) {
      const store = window[storeName];
      if (!store) continue;
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        if (k && /seed|hash|nonce|fair|crash|aviator|provably/i.test(k)) {
          storage[storeName + ":" + k] = String(store.getItem(k) || "").slice(0, 1000);
        }
      }
    }
  } catch (e) {}

  const buf = window.__aviatorBuf ? window.__aviatorBuf.splice(0, window.__aviatorBuf.length) : [];
  return { href: location.href, title: document.title, text, html, labeled, attrs, meta, scripts, frames, storage, buf };
}
"""


def as_float(v: Any):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
        return x if x >= 1.0 else None
    if isinstance(v, str):
        m = NUM_RE.match(v.replace(",", "."))
        if m:
            x = float(m.group(1))
            return x if x >= 1.0 else None
    return None


def safe_url(raw_url: str) -> str:
    """Remove credentials and sensitive query values while retaining endpoint shape."""
    try:
        p = urlsplit(str(raw_url))
        clean_q = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            clean_q.append((k, "<redacted>" if SECRET_QUERY_KEYS.match(k) else v))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(clean_q), ""))
    except Exception:
        return str(raw_url).split("#", 1)[0]


def payload_bytes(payload: Any) -> bytes | None:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, memoryview):
        return payload.tobytes()
    return None


def payload_text(payload: Any) -> str:
    b = payload_bytes(payload)
    if b is not None:
        return b.decode("utf-8", errors="replace")
    return str(payload)


def binary_preview(payload: Any, limit: int = 96) -> str:
    b = payload_bytes(payload)
    if b is None:
        return ""
    return b[:limit].hex()


def redact_obj(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET_JSON_KEYS.search(str(k)):
                out[k] = "<redacted>"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, list):
        return [redact_obj(x) for x in value]
    return value


def safe_request_body(request) -> Any:
    try:
        body = request.post_data
    except Exception:
        body = None
    if not body:
        return None
    body = str(body)
    if len(body) > 10000:
        return {
            "truncated": True,
            "length": len(body),
            "sha1": hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest(),
        }
    try:
        return redact_obj(json.loads(body))
    except Exception:
        return body[:10000] if not SECRET_JSON_KEYS.search(body) else "<redacted-text>"


def log_network(record: dict) -> None:
    try:
        with NETWORK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redact_obj(record), ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def log_diag(record: dict) -> None:
    try:
        append_diagnostic(redact_obj(record))
    except Exception:
        pass


def json_candidates(raw: str) -> list[Any]:
    text = (raw or "").strip()
    if not text or text.startswith("[binary:"):
        return []
    chunks = [x.strip() for x in re.split(r"[\x1e\u001e\n\r]+", text) if x.strip()] or [text]
    out: list[Any] = []
    seen = set()
    for chunk in chunks:
        if chunk in seen:
            continue
        seen.add(chunk)
        try:
            out.append(json.loads(chunk))
            continue
        except Exception:
            pass
        for pos in [m.start() for m in re.finditer(r"[\{\[]", chunk)][:20]:
            try:
                out.append(json.loads(chunk[pos:]))
            except Exception:
                continue
    return out


def event_id_for(obj: dict, multiplier: float | None, raw: str) -> str:
    for key in ("l", "id", "roundId", "round_id", "gameId", "ts"):
        if obj.get(key) not in (None, ""):
            return str(obj.get(key))
    basis = raw[:800] if raw else json.dumps(obj, ensure_ascii=False)[:800]
    return hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()[:16]


def verification_record(record: dict) -> dict:
    verify = verify_round(
        record.get("multiplier"),
        server_seed=record.get("server_seed") or "",
        client_seeds=record.get("client_seeds") or [],
        seed_hash=record.get("seed_hash") or "",
        nonce=record.get("nonce"),
    )
    return {
        "verified": verify.ok,
        "verify_scheme": verify.scheme,
        "verify_detail": verify.detail,
        "verified_multiplier": verify.expected_multiplier,
        "hash_match": verify.hash_match,
        "verify_tried": verify.tried,
        "verification_updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _walk_dicts(value: Any, max_nodes: int = 500):
    stack = [value]
    seen = set()
    count = 0
    while stack and count < max_nodes:
        cur = stack.pop()
        count += 1
        if isinstance(cur, dict):
            ident = id(cur)
            if ident in seen:
                continue
            seen.add(ident)
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur[:120])


STRONG_MULTIPLIER_KEYS = {
    "crashPoint", "crash_point", "finalMultiplier", "final_multiplier",
    "crashMultiplier", "crash_multiplier", "crashedAt", "crashed_at",
    "gameMultiplier", "game_multiplier", "endMultiplier", "end_multiplier",
}
CRASH_COMMAND_HINTS = {
    "crash", "roundend", "round_end", "roundresult", "round_result",
    "gameresult", "game_result", "gamecrash", "game_crash",
    "roundcrashed", "round_crashed",
}
NON_CRASH_COMMANDS = {
    "updatecurrentcashouts", "currentbetsinfo", "updatecurrentbets",
    "onlineplayers", "x", "init", "ping_response", "pingresponse",
    "updatecurrentcashout",
}

ROUND_RESULT_COMMANDS = {
    "roundchartinfo",
}



def _event_multiplier_and_id(value: Any, *, allow_generic: bool = False):
    """Extract only strong, crash-specific multiplier fields.

    Generic fields such as result/factor/coef are intentionally excluded because
    Aviator player-cashout/bet payloads can contain those values and they are not
    the round crash point.
    """
    id_keys = ("roundId", "round_id", "gameId", "game_id", "id", "l")
    best_value = None
    best_id = None
    for obj in _walk_dicts(value):
        for key in id_keys:
            if obj.get(key) not in (None, ""):
                best_id = str(obj.get(key))
                break
        for key in STRONG_MULTIPLIER_KEYS:
            if key in obj:
                v = as_float(obj.get(key))
                if v is not None:
                    best_value = v
                    break
        if best_value is None and allow_generic:
            for key in ("multiplier", "coefficient", "multiplierValue"):
                if key in obj:
                    v = as_float(obj.get(key))
                    if v is not None:
                        best_value = v
                        break
        if best_value is not None:
            break
    return best_value, best_id


class RoundTracker:
    def __init__(self):
        previous = last_round() or {}
        self.current: dict = {
            "id": None,
            "phase": "waiting",
            "seed_hash": None,
            "server_seed": None,
            "client_seeds": [],
            "combined_hash": None,
            "nonce": None,
            "multiplier": None,
        }
        self.pending_commitment: dict = {}
        self.last_round_id = str(previous.get("id")) if previous.get("id") not in (None, "") else None
        self.seen_crashes: set[tuple] = set()
        self.seed_hits = 0
        self.last_seed_fingerprint = None

    def _field_count(self, state: dict) -> int:
        return sum(1 for k in ("seed_hash", "server_seed", "combined_hash", "nonce") if state.get(k)) + len(state.get("client_seeds") or [])

    def _merge_sources(self, state: dict, extracted: dict, source: str) -> dict:
        sources = dict(state.get("seed_sources") or {})
        for key in ("seed_hash", "server_seed", "combined_hash", "nonce"):
            vals = extracted.get(key) or []
            if vals:
                sources[key] = {"source": source, "value": vals[0]}
        clients = extracted.get("client_seeds") or []
        if clients:
            sources["client_seeds"] = {"source": source, "count": len(clients)}
        state["seed_sources"] = sources
        return state

    def _apply_to_saved_round(self, round_id: str, extracted: dict, source: str, url: str) -> bool:
        rid = str(round_id)
        rounds = load_rounds()
        existing = next((dict(r) for r in rounds if str(r.get("id")) == rid), None)
        if existing is None:
            return False
        before = json.dumps(existing, sort_keys=True, default=str)
        merged = merge_seed_state(existing, extracted)
        merged = self._merge_sources(merged, extracted, source)
        if merged.get("id") is None:
            merged["id"] = rid
        merged.update(verification_record(merged))
        after = json.dumps(merged, sort_keys=True, default=str)
        if before == after:
            return False
        upsert_round(merged)
        append_seed_log({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "url": safe_url(url),
            "round_id": rid,
            "mode": "late_enrichment",
            "extracted": extracted,
            "state": {
                "seed_hash": merged.get("seed_hash"),
                "server_seed": merged.get("server_seed"),
                "client_seeds": merged.get("client_seeds"),
                "nonce": merged.get("nonce"),
                "combined_hash": merged.get("combined_hash"),
            },
        })
        return True

    def apply_seeds(self, extracted: dict, source: str, url: str = "", target_round_id: str | None = None) -> None:
        if not extracted:
            return
        meaningful = any(extracted.get(k) for k in ("seed_hash", "server_seed", "client_seeds", "combined_hash", "nonce"))
        if not meaningful:
            return

        rid = str(target_round_id) if target_round_id not in (None, "") else None
        has_server = bool(extracted.get("server_seed"))

        # Explicit round id is authoritative. Never attach evidence for one
        # round to a different active round.
        if rid is not None:
            if self.current.get("id") == rid:
                target_state = self.current
                before = json.dumps(target_state, sort_keys=True, default=str)
                self.current = merge_seed_state(target_state, extracted)
                self.current = self._merge_sources(self.current, extracted, source)
                after = json.dumps(self.current, sort_keys=True, default=str)
                if before != after:
                    self.seed_hits += 1
                    append_seed_log({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": source,
                        "url": safe_url(url),
                        "round_id": rid,
                        "mode": "active-explicit",
                        "extracted": extracted,
                    })
                    self.flush_live()
                return

            if self._apply_to_saved_round(rid, extracted, source, url):
                self.seed_hits += 1
                return

            # The round has an explicit id but is not saved yet. Keep it
            # isolated until OnBetting/OnStart creates the matching state.
            pending = merge_seed_state({}, extracted)
            pending = self._merge_sources(pending, extracted, source)
            pending["round_id"] = rid
            pending["explicit_round_id"] = True
            self.pending_commitment = pending
            self.seed_hits += 1
            return

        # Server-seed reveals without an explicit round id are normally for the
        # round that just crashed. Bind them to the last completed round.
        if rid is None and self.last_round_id:
            if self._apply_to_saved_round(self.last_round_id, extracted, source, url):
                self.seed_hits += 1
                return

        if self.current.get("id") is None:
            pending = merge_seed_state(self.pending_commitment, extracted)
            pending = self._merge_sources(pending, extracted, source)
            self.pending_commitment = pending
            self.seed_hits += 1
            return

        before = json.dumps(self.current, sort_keys=True, default=str)
        self.current = merge_seed_state(self.current, extracted)
        self.current = self._merge_sources(self.current, extracted, source)
        after = json.dumps(self.current, sort_keys=True, default=str)
        if before != after:
            self.seed_hits += 1
            append_seed_log({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "url": safe_url(url),
                "round_id": self.current.get("id"),
                "mode": "active",
                "extracted": extracted,
            })
            self.flush_live()

    def on_signalr(self, obj: dict, raw: str, url: str) -> None:
        target = obj.get("target")
        args = obj.get("arguments") or []
        payload = args[0] if args and isinstance(args[0], dict) else {}
        if not isinstance(payload, dict):
            payload = {}

        rid = None
        for key in ("l", "id", "roundId", "round_id", "gameId"):
            if payload.get(key) not in (None, ""):
                rid = str(payload.get(key))
                break

        # Establish the round state before attaching seed evidence to explicit
        # OnBetting/OnStage/OnStart events. This prevents a new round's
        # commitment from being attached to the previous round.
        if target in ("OnStage", "OnBetting") and rid:
            self._begin_round(rid, target)
        elif target == "OnStart" and rid:
            if self.current.get("id") != rid:
                self._begin_round(rid, "waiting")
            else:
                self.current["phase"] = "flying"
                self.flush_live()

        if target != "OnCrash":
            extracted = extract_seed_fields(obj, short_ok=target in SIGNALR_TARGETS)
            self.apply_seeds(extracted, f"signalr:{target}", url, target_round_id=rid)

        if target == "OnCrash":
            value = None
            for key in ("f", "multiplier", "coefficient", "crashPoint", "crash"):
                value = as_float(payload.get(key))
                if value is not None:
                    break
            if value is None:
                return
            eid = rid or event_id_for(payload, value, raw)
            key = (eid, round(value, 6))
            if key in self.seen_crashes:
                return
            self.seen_crashes.add(key)
            if len(self.seen_crashes) > 8000:
                self.seen_crashes.clear()
            self.finish_round(eid, value, payload, url)

        if target == "OnCrash":
            log_network({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "signalr_oncrash_keys",
                "url": safe_url(url),
                "keys": list(payload.keys()),
                "payload": payload,
            })

    def _begin_round(self, rid: str, target: str) -> None:
        old = self.current.get("id")
        if old != rid:
            self.current = {
                "id": rid,
                "phase": "betting" if target == "OnBetting" else "waiting",
                "seed_hash": None,
                "server_seed": None,
                "client_seeds": [],
                "combined_hash": None,
                "nonce": None,
                "multiplier": None,
            }

            # Apply only pending evidence explicitly belonging to this round.
            pending_id = self.pending_commitment.get("round_id")
            if self.pending_commitment and (pending_id in (None, rid)):
                pending = dict(self.pending_commitment)
                pending.pop("round_id", None)
                pending.pop("explicit_round_id", None)
                self.current = merge_seed_state(self.current, pending)
                self.current["seed_sources"] = dict(pending.get("seed_sources") or {})
                self.pending_commitment = {}
        else:
            self.current["phase"] = "betting" if target == "OnBetting" else self.current.get("phase") or "waiting"

        self.flush_live(force_predict=target == "OnBetting")

    def finish_round(self, round_id: str, multiplier: float, payload: dict, url: str, source: str = "websocket") -> None:
        rid = str(round_id)

        # OnCrash belongs to this exact round. If the active state belongs to a
        # different round, start a clean state for the crash rather than mixing
        # seed data across rounds.
        if self.current.get("id") != rid:
            self.current = {
                "id": rid,
                "phase": "flying",
                "seed_hash": None,
                "server_seed": None,
                "client_seeds": [],
                "combined_hash": None,
                "nonce": None,
                "multiplier": None,
            }

        extracted = extract_seed_fields(payload, short_ok=True)
        self.current["id"] = rid
        # Explicitly bind any seed material in the OnCrash payload to this round.
        self.current = merge_seed_state(self.current, extracted)
        self.current = self._merge_sources(self.current, extracted, "oncrash-payload")
        self.current["multiplier"] = multiplier
        self.current["phase"] = "crashed"

        record = {
            "id": rid,
            "multiplier": multiplier,
            "ts": payload.get("ts"),
            "phase": "crashed",
            "seed_hash": self.current.get("seed_hash"),
            "server_seed": self.current.get("server_seed"),
            "client_seeds": list(self.current.get("client_seeds") or []),
            "combined_hash": self.current.get("combined_hash"),
            "nonce": self.current.get("nonce"),
            "seed_sources": self.current.get("seed_sources") or {},
            "source": source,
            "raw_keys": sorted(str(k) for k in payload.keys()),
        }
        record.update(verification_record(record))
        saved = upsert_round(record)
        self.last_round_id = rid
        print(
            f"[COLLECTOR] ROUND {rid} -> {multiplier:.2f}x source={source} seeds={_seed_summary(saved)}",
            flush=True,
        )

        self.current = {
            "id": None,
            "phase": "waiting",
            "seed_hash": None,
            "server_seed": None,
            "client_seeds": [],
            "combined_hash": None,
            "nonce": None,
            "multiplier": None,
        }
        self.flush_live()

    def ingest_generic(self, source: str, url: str, payload: Any, process_hex: bool = True) -> None:
        raw = payload_text(payload)
        if not raw:
            return
        for obj in json_candidates(raw):
            if isinstance(obj, dict) and obj.get("target") in SIGNALR_TARGETS:
                self.on_signalr(obj, raw, url)
            else:
                extracted = extract_seed_fields(obj, short_ok=False)
                self.apply_seeds(extracted, source, url)
                self._maybe_generic_crash(obj, raw, url)

        # Only treat raw hex as evidence when seed/fair/nonce context is present.
        if process_hex and re.search(r"(?i)seed|provably|fair|nonce|server.?seed|client.?seed", raw):
            context_window = raw.lower()
            hexes = extract_hex_from_text(context_window)
            labeled = {
                "seed_hash": [],
                "server_seed": [],
                "client_seeds": hexes.get("hex32") or [],
                "combined_hash": hexes.get("hex128") or [],
                "nonce": [],
            }
            for m in re.finditer(r"(?i)(seed.?hash|server.?seed.?hash|hash)", raw):
                nearby = raw[m.start():m.start() + 250]
                labeled["seed_hash"].extend(extract_hex_from_text(nearby).get("hex64") or [])
            if labeled["seed_hash"] or labeled["combined_hash"] or labeled["client_seeds"]:
                self.apply_seeds(labeled, source + ":hex", url)

    @staticmethod
    def _recursive_value(data: Any, keys: set[str], max_nodes: int = 1000):
        """Find the first non-empty value whose normalized key matches one of keys."""
        wanted = {re.sub(r"[^a-z0-9]", "", k.lower()) for k in keys}
        stack = [data]
        seen = set()
        nodes = 0
        while stack and nodes < max_nodes:
            cur = stack.pop()
            nodes += 1
            if isinstance(cur, dict):
                ident = id(cur)
                if ident in seen:
                    continue
                seen.add(ident)
                for k, v in cur.items():
                    nk = re.sub(r"[^a-z0-9]", "", str(k).lower())
                    if nk in wanted and v not in (None, "", [], {}):
                        return v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(reversed(cur[:200]))
        return None

    def ingest_sfs_event(self, event: dict, url: str) -> None:
        if not isinstance(event, dict):
            return

        event_type = str(event.get("event_type") or "")
        data = event.get("data") or {}
        et = event_type.lower().replace("-", "_")
        normalized_et = et.replace(" ", "_")

        # V9: Spribe exposes a dedicated fairness response. In the observed
        # stream it contains serverSeedSHA256 (the commitment/hash). Bind it
        # to the explicit round when available, otherwise to the most recently
        # completed round. Never infer a server seed from a multiplier.
        if normalized_et == "serverseedresponse":
            # In the observed Aviator stream, serverSeedSHA256 may be nested
            # inside the decoded SFS payload. Search recursively instead of
            # assuming it exists at the top level.
            rid_value = self._recursive_value(
                data, {"roundId", "round_id", "gameId", "game_id", "id"}
            )
            rid = str(rid_value) if rid_value not in (None, "") else None

            raw_hash = self._recursive_value(
                data,
                {
                    "serverSeedSHA256", "serverSeedSha256",
                    "server_seed_sha256", "seedHash", "seed_hash"
                },
            )
            seed_hash = None
            if isinstance(raw_hash, str) and re.fullmatch(r"[0-9a-fA-F]{64}", raw_hash.strip()):
                seed_hash = raw_hash.strip().lower()

            extracted = {
                "seed_hash": [seed_hash] if seed_hash else [],
                "server_seed": [],
                "client_seeds": [],
                "combined_hash": [],
                "nonce": [],
            }

            raw_server = self._recursive_value(data, {"serverSeed", "server_seed"})
            if isinstance(raw_server, str) and raw_server.strip():
                extracted["server_seed"] = [raw_server.strip()]

            raw_client = self._recursive_value(data, {"clientSeed", "client_seed"})
            if isinstance(raw_client, str) and raw_client.strip():
                extracted["client_seeds"] = [raw_client.strip()]

            raw_nonce = self._recursive_value(
                data, {"nonce", "serverSeedNonce", "server_seed_nonce"}
            )
            if raw_nonce not in (None, ""):
                extracted["nonce"] = [raw_nonce]

            meaningful = any(extracted.values())
            if meaningful:
                target_rid = rid or self.last_round_id
                print(
                    f"[SFS] FAIRNESS serverSeedResponse hash={'yes' if seed_hash else 'no'} "
                    f"server_seed={'yes' if extracted['server_seed'] else 'no'} round_id={target_rid or '-'}",
                    flush=True,
                )
                self.apply_seeds(
                    extracted,
                    "sfs:serverSeedResponse",
                    url,
                    target_round_id=target_rid,
                )
            else:
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "sfs_fairness_response_empty",
                    "url": safe_url(url),
                    "command": event_type,
                    "keys": sorted(str(k) for k in d.keys()),
                })
            return

        # V8: roundChartInfo is the authoritative completed-round message for
        # the Spribe Aviator stream observed here: it carries maxMultiplier
        # together with roundId. Do NOT use cashout/bet multipliers.
        if normalized_et in ROUND_RESULT_COMMANDS:
            chart = data if isinstance(data, dict) else {}
            rid = None
            for key in ("roundId", "round_id"):
                if chart.get(key) not in (None, ""):
                    rid = str(chart.get(key))
                    break
            value = None
            for key in ("maxMultiplier", "max_multiplier"):
                if key in chart:
                    value = as_float(chart.get(key))
                    if value is not None:
                        break

            if rid is None or value is None:
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "sfs_round_result_invalid",
                    "url": safe_url(url),
                    "command": event_type,
                    "round_id": rid,
                    "max_multiplier": chart.get("maxMultiplier") if isinstance(chart, dict) else None,
                    "keys": sorted(str(k) for k in chart.keys()) if isinstance(chart, dict) else [],
                })
                return

            # One completed record per round. A repeated SmartFox delivery of
            # the same roundChartInfo must not create duplicate Telegram alerts.
            if rid == self.last_round_id or (rid, round(value, 6)) in self.seen_crashes:
                return
            self.seen_crashes.add((rid, round(value, 6)))

            print(f"[SFS] ROUND_RESULT round_id={rid} maxMultiplier={value:.2f}x", flush=True)
            self.finish_round(rid, value, chart, url, source="sfs:roundChartInfo")
            return

        # Never interpret player cashout/bet/online-player messages as a round
        # result. Those messages legitimately contain monetary/cashout
        # multipliers and were the source of the repeated false OnCrash values.
        if normalized_et in NON_CRASH_COMMANDS:
            return

        # Preserve seed/fairness evidence from decoded SFS data.
        try:
            extracted = extract_seed_fields(data, short_ok=True)
            self.apply_seeds(extracted, f"sfs-event:{event_type}", url)
        except Exception:
            pass

        # changeState announces the next round id before the chart result for
        # the previous round arrives. Keep the id for live diagnostics, but do
        # not save a result from state changes themselves.
        if normalized_et == "changestate" and isinstance(data, dict):
            rid = None
            for key in ("roundId", "round_id"):
                if data.get(key) not in (None, ""):
                    rid = str(data.get(key))
                    break
            if rid:
                self.current["id"] = rid
                state_id = data.get("newStateId")
                self.current["phase"] = {1: "betting", 2: "flying", 3: "crashed"}.get(state_id, "waiting")
                self.flush_live(force_predict=state_id == 1)
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "sfs_change_state",
                    "url": safe_url(url),
                    "round_id": rid,
                    "new_state_id": state_id,
                })

        raw_lower = json.dumps(data, ensure_ascii=False, default=str).lower()

        # Only accept an SFS packet as a crash/result event when:
        #   1) its command clearly indicates round end/crash/result, OR
        #   2) it contains a strong crash-specific field.
        command_is_crash = any(h in normalized_et for h in CRASH_COMMAND_HINTS)
        has_strong_key = any(k.lower() in raw_lower for k in STRONG_MULTIPLIER_KEYS)

        if not command_is_crash and not has_strong_key:
            # Keep diagnostics for likely state/history packets, but never save
            # them as a round result.
            if normalized_et in {"changestate", "roundchartinfo", "onstage", "onstart"}:
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "sfs_candidate_no_crash",
                    "url": safe_url(url),
                    "command": event_type,
                    "data_keys": (
                        sorted(str(k) for k in data.keys())
                        if isinstance(data, dict) else []
                    ),
                })
            return

        value, rid = _event_multiplier_and_id(
            data,
            allow_generic=command_is_crash,
        )
        if value is None:
            log_network({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "sfs_candidate_no_multiplier",
                "url": safe_url(url),
                "command": event_type,
                "data_keys": (
                    sorted(str(k) for k in data.keys())
                    if isinstance(data, dict) else []
                ),
            })
            return

        rid = rid or event_id_for(
            data if isinstance(data, dict) else {},
            value,
            json.dumps(data, ensure_ascii=False, default=str),
        )
        key = (rid, round(value, 6))
        if key in self.seen_crashes:
            return

        # Additional guard: state/history packets containing a generic
        # "multiplier" are not enough by themselves.
        if not command_is_crash and not any(
            isinstance(obj, dict) and any(k in obj for k in STRONG_MULTIPLIER_KEYS)
            for obj in _walk_dicts(data)
        ):
            return

        self.seen_crashes.add(key)
        self.finish_round(rid, value, data if isinstance(data, dict) else {}, url)

    def ingest_dom(self, scrape: dict) -> None:
        url = scrape.get("href") or ""
        html = scrape.get("html") or ""
        self.apply_seeds(parse_labeled_text(scrape.get("text") or ""), "dom-text", url)
        self.apply_seeds(parse_labeled_text(html), "dom-html", url)
        self.apply_seeds(parse_html_attributes(html), "dom-html-attributes", url)
        for item in scrape.get("labeled") or []:
            self.apply_seeds(parse_labeled_text(item.get("text") or ""), "dom-node", url)
        for item in scrape.get("attrs") or []:
            self.apply_seeds(parse_labeled_text(f"{item.get('name')}: {item.get('value')}"), "dom-attribute", url)
        for item in scrape.get("meta") or []:
            self.apply_seeds(parse_labeled_text(f"{item.get('name')}: {item.get('value')}"), "dom-meta", url)
        for script in scrape.get("scripts") or []:
            self.apply_seeds(parse_labeled_text(script), "dom-script", url)
            self.ingest_generic("dom-script", url, script, process_hex=True)
        storage = scrape.get("storage") or {}
        if storage:
            self.apply_seeds(extract_seed_fields(storage, short_ok=True), "dom-storage", url)
            self.ingest_generic("dom-storage", url, json.dumps(storage, ensure_ascii=False))
        for frame in scrape.get("buf") or []:
            if not isinstance(frame, dict):
                continue
            direction = str(frame.get("dir") or "")
            if direction == "sfs-event":
                self.ingest_sfs_event(frame, frame.get("url") or url)
                continue
            self.ingest_generic(
                "injected-ws-" + direction,
                frame.get("url") or url,
                frame.get("data"),
            )

    def _maybe_generic_crash(self, obj: Any, raw: str, url: str) -> None:
        if not isinstance(obj, dict):
            return
        kind = str(obj.get("type") or obj.get("event") or obj.get("code") or "").lower()
        if not any(word in kind for word in ("crash", "flew", "round_end", "roundend", "game_crash")):
            return
        value = None
        for key in ("f", "multiplier", "coefficient", "crashPoint", "crash", "result"):
            value = as_float(obj.get(key))
            if value is not None:
                break
        inner = obj.get("crash") or obj.get("data") or obj.get("payload")
        if value is None and isinstance(inner, dict):
            for key in ("f", "multiplier", "coefficient", "crashPoint"):
                value = as_float(inner.get(key))
                if value is not None:
                    obj = inner
                    break
        if value is None:
            return
        rid = event_id_for(obj, value, raw)
        key = (rid, round(value, 6))
        if key in self.seen_crashes:
            return
        self.seen_crashes.add(key)
        self.finish_round(rid, value, obj, url)

    def flush_live(self, force_predict: bool = False) -> None:
        state = {
            "phase": self.current.get("phase") or "waiting",
            "round_id": self.current.get("id"),
            "seed_hash": self.current.get("seed_hash"),
            "server_seed": self.current.get("server_seed"),
            "client_seeds": list(self.current.get("client_seeds") or []),
            "combined_hash": self.current.get("combined_hash"),
            "nonce": self.current.get("nonce"),
            "multiplier": self.current.get("multiplier"),
            "seed_hits": self.seed_hits,
            "seed_sources": self.current.get("seed_sources") or {},
        }
        if force_predict:
            state["predict_token"] = f"{state.get('round_id')}-{int(time.time())}"
        save_live(state)


def _short(value: Any, n: int = 10) -> str:
    if not value:
        return "-"
    s = str(value)
    return s if len(s) <= n else s[:n] + "…"


def _seed_summary(record: dict) -> str:
    return f"hash={_short(record.get('seed_hash'))} server={_short(record.get('server_seed'))} c={len(record.get('client_seeds') or [])}"


def _is_game_host(host: str) -> bool:
    h = (host or "").lower().split(":", 1)[0]
    for hint in GAME_HOST_HINTS:
        if h == hint or h.endswith("." + hint):
            return True
    return False


def _candidate_response(response: Response) -> bool:
    try:
        u = urlsplit(response.url)
        host = u.netloc.lower()
        path = u.path.lower()
    except Exception:
        return False

    if _is_game_host(host):
        return True

    # Support common game iframe / websocket helper endpoints without opening
    # every unrelated response on the page.
    return any(marker in path for marker in (
        "/games-frame/",
        "/game-crash",
        "/game/",
        "/signalr",
        "/hub",
    ))


# AVIATOR_V11_AUTO_FAIRNESS
FAIRNESS_UI_RE = re.compile(
    r"(?i)(provably\s*fair|fairness|game\s*fairness|العدل|نزاهة|التحقق)"
)


async def _trigger_fairness_ui(context, round_id: str) -> bool:
    """Open Aviator Provably Fair Settings only."""

    settings_re = re.compile(
        r"(?i)provably\s*fair\s*(settings|configuration)"
    )

    seed_re = re.compile(
        r"(?i)(server\s*seed|client\s*seed|seed\s*hash|nonce|"
        r"بذرة\s*الخادم|بذرة\s*العميل)"
    )

    # Never click the general "Provably Fair Game" link.
    for pge in list(context.pages):
        for frame in list(pge.frames):

            # Only inspect the Aviator game frame.
            try:
                url = frame.url or ""
            except Exception:
                continue

            if "aviator-next.spribegaming.com" not in url:
                continue

            print(
                f"[FAIRNESS] inspecting Aviator controls round={round_id}",
                flush=True
            )

            try:
                loc = frame.locator(
                    "button, a, [role='button'], "
                    "[aria-label], [title]"
                )
                count = min(await loc.count(), 200)
            except Exception:
                continue

            # Print useful controls so we can identify the real Aviator menu.
            for i in range(count):
                try:
                    el = loc.nth(i)

                    if not await el.is_visible():
                        continue

                    aria = str(
                        await el.get_attribute("aria-label") or ""
                    )
                    title = str(
                        await el.get_attribute("title") or ""
                    )
                    text_value = str(
                        await el.inner_text(timeout=300) or ""
                    )

                    label = " ".join(
                        x for x in [aria, title, text_value] if x
                    ).strip()

                    if label:
                        print(
                            f"[FAIRNESS] AVIATOR CONTROL: "
                            f"{label[:250]}",
                            flush=True
                        )

                except Exception:
                    continue

            # Search only for actual Settings.
            try:
                loc = frame.locator(
                    "button, a, [role='button'], "
                    "[aria-label], [title]"
                )
                count = min(await loc.count(), 200)
            except Exception:
                continue

            for i in range(count):
                try:
                    el = loc.nth(i)

                    if not await el.is_visible():
                        continue

                    label = " ".join([
                        str(await el.get_attribute("aria-label") or ""),
                        str(await el.get_attribute("title") or ""),
                        str(await el.inner_text(timeout=300) or "")
                    ]).strip()

                    # IMPORTANT:
                    # "Provably Fair Game" is deliberately rejected.
                    if not settings_re.search(label):
                        continue

                    print(
                        f"[FAIRNESS] SETTINGS candidate round={round_id}: "
                        f"{label[:200]}",
                        flush=True
                    )

                    await el.click(timeout=2500, force=True)
                    await pge.wait_for_timeout(1200)

                    for cp in list(context.pages):
                        for cf in list(cp.frames):
                            try:
                                body = await cf.locator(
                                    "body"
                                ).inner_text(timeout=1500)
                            except Exception:
                                continue

                            if seed_re.search(body):
                                print(
                                    f"[FAIRNESS] SETTINGS VERIFIED "
                                    f"round={round_id}",
                                    flush=True
                                )

                                print(
                                    f"[FAIRNESS] SETTINGS TEXT "
                                    f"round={round_id}: "
                                    f"{body[:4000].replace(chr(10), ' | ')}",
                                    flush=True
                                )

                                return True

                except Exception as e:
                    print(
                        f"[FAIRNESS] settings error round={round_id}: "
                        f"{type(e).__name__}: {e}",
                        flush=True
                    )

    print(
        f"[FAIRNESS] Provably Fair Settings not found "
        f"round={round_id}",
        flush=True
    )

    return False

async def _login_needed(page) -> bool:
    """Best-effort detection of a login page/modal without assuming site selectors."""
    try:
        current = (page.url or "").lower()
        if current.startswith("chrome-error://"):
            return False

        # A password field is a strong signal that the page is asking for login.
        if await page.locator("input[type='password']").count() > 0:
            return True

        # Also catch common login/auth URL paths.
        if any(marker in current for marker in ("/login", "/signin", "/sign-in", "/auth")):
            return True
    except Exception:
        pass
    return False


async def _wait_for_manual_login(page) -> None:
    """Pause for a manual login in the persistent Chrome profile when required."""
    if not await _login_needed(page):
        return

    print("[LOGIN] تسجيل الدخول مطلوب. سجّل الدخول يدويًا داخل نافذة Chrome المفتوحة.", flush=True)
    print(f"[LOGIN] ستتم المتابعة تلقائيًا بعد نجاح الدخول (مهلة {LOGIN_WAIT_SECONDS} ثانية).", flush=True)

    deadline = time.monotonic() + LOGIN_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        if not await _login_needed(page):
            print("[LOGIN] تم تجاوز شاشة تسجيل الدخول.", flush=True)
            return

    raise RuntimeError("انتهت مهلة تسجيل الدخول. سجّل الدخول في Chrome ثم شغّل bot.py مرة أخرى.")


async def run() -> None:
    print(f"[GAME] target={GAME_URL}", flush=True)
    set_status("starting", "فتح صفحة Aviator المحددة ومراقبة WebSocket وHTTP والإطارات وبيانات العدل...")
    tracker = RoundTracker()

    async with async_playwright() as p:
        PROFILE.mkdir(exist_ok=True)
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE),
                channel="chrome",
                headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--disable-notifications"],
            )
        except Exception as exc:
            set_status("error", f"تعذر تشغيل Chrome: {exc}")
            raise

        await context.add_init_script(INJECT_JS)
        attached_pages: set[int] = set()

        async def on_request(request):
            try:
                url = request.url
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "http_request",
                    "url": safe_url(url),
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "post_data": safe_request_body(request),
                })
            except Exception as exc:
                log_diag({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "http_request_error",
                    "error": str(exc),
                })

        async def on_response(response: Response):
            try:
                url = response.url
                ctype = (response.headers.get("content-type") or "").lower()
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "http_response",
                    "url": safe_url(url),
                    "status": response.status,
                    "content_type": ctype,
                    "resource_type": response.request.resource_type,
                })

                if not _candidate_response(response):
                    return
                if not any(x in ctype for x in ("json", "text/", "javascript", "xml", "html")):
                    return
                try:
                    body = await response.text()
                except Exception:
                    return

                truncated = len(body) > MAX_LOG_PAYLOAD
                body_for_log = body[:MAX_LOG_PAYLOAD]
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "http_response_body",
                    "url": safe_url(url),
                    "status": response.status,
                    "content_type": ctype,
                    "payload_truncated": truncated,
                    "payload": body_for_log,
                })

                tracker.ingest_generic("http_response", url, body_for_log, process_hex=True)
                if re.search(r"(?i)seed|provably|fair|nonce", body):
                    log_diag({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "http_response_candidate",
                        "url": safe_url(url),
                        "content_type": ctype,
                        "length": len(body),
                        "matched_terms": sorted(set(re.findall(r"(?i)seed|provably|fair|nonce", body))),
                    })
            except Exception as exc:
                print(f"[COLLECTOR] response error: {exc}", flush=True)

        def on_ws(ws):
            ws_url = ws.url
            print(f"[WS] OPEN {safe_url(ws_url)}", flush=True)
            log_network({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "websocket_open",
                "url": safe_url(ws_url),
            })

            def on_received(payload):
                b = payload_bytes(payload)
                if b is not None:
                    log_network({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": "websocket_received_binary",
                        "url": safe_url(ws_url),
                        "length": len(b),
                        "preview_hex": binary_preview(b),
                    })
                    print(f"[WS] RX BINARY {len(b)} bytes preview={binary_preview(b, 24)}", flush=True)
                    if SFS_DECODER_AVAILABLE:
                        try:
                            obj, consumed = decode_s2c_packet(b)
                            cmd, params = parse_s2c_command(obj)
                            print(f"[SFS] RX cmd={cmd!r} consumed={consumed}", flush=True)
                            log_network({
                                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "kind": "sfs_decoded",
                                "url": safe_url(ws_url),
                                "command": cmd,
                                "consumed": consumed,
                                "object": obj,
                                "params": params,
                                "object_keys": (
                                    sorted(str(k) for k in obj.keys())
                                    if isinstance(obj, dict) else []
                                ),
                                "param_keys": (
                                    sorted(str(k) for k in params.keys())
                                    if isinstance(params, dict) else []
                                ),
                            })
                            tracker.ingest_sfs_event({
                                "event_type": str(cmd or "sfs_packet"),
                                "data": params if params is not None else obj,
                                "object": obj,
                            }, ws_url)
                            return
                        except Exception as exc:
                            log_diag({
                                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "source": "sfs_decode_error",
                                "url": safe_url(ws_url),
                                "length": len(b),
                                "error": str(exc),
                            })
                    return

                raw = payload_text(payload)
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "websocket_received",
                    "url": safe_url(ws_url),
                    "payload_truncated": len(raw) > MAX_LOG_PAYLOAD,
                    "payload": raw[:MAX_LOG_PAYLOAD],
                })
                tracker.ingest_generic("websocket_received", ws_url, raw, process_hex=True)

            def on_sent(payload):
                b = payload_bytes(payload)
                if b is not None:
                    log_network({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": "websocket_sent_binary",
                        "url": safe_url(ws_url),
                        "length": len(b),
                        "preview_hex": binary_preview(b),
                    })
                    print(f"[WS] TX BINARY {len(b)} bytes preview={binary_preview(b, 24)}", flush=True)
                    return
                raw = payload_text(payload)
                log_network({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "websocket_sent",
                    "url": safe_url(ws_url),
                    "payload_truncated": len(raw) > MAX_LOG_PAYLOAD,
                    "payload": raw[:MAX_LOG_PAYLOAD],
                })
                tracker.ingest_generic("websocket_sent", ws_url, raw, process_hex=True)

            ws.on("framereceived", on_received)
            ws.on("framesent", on_sent)
            ws.on("socketerror", lambda error: print(f"[WS] ERROR {safe_url(ws_url)}: {error}", flush=True))
            ws.on("close", lambda _=None: print(f"[WS] CLOSE {safe_url(ws_url)}", flush=True))

        def attach_page(page):
            pid = id(page)
            if pid in attached_pages:
                return
            attached_pages.add(pid)
            page.on("websocket", on_ws)
            page.on("request", lambda request: asyncio.create_task(on_request(request)))
            page.on("response", lambda response: asyncio.create_task(on_response(response)))
            page.on("close", lambda _: print("[COLLECTOR] PAGE CLOSED", flush=True))

        for existing in context.pages:
            attach_page(existing)
        context.on("page", attach_page)

        page = context.pages[0] if context.pages else await context.new_page()
        attach_page(page)

        try:
            # Open the site first so the user can establish the authenticated
            # session in the persistent Chrome profile.
            set_status("opening", "فتح الموقع والتحقق من جلسة تسجيل الدخول...")
            await page.goto(SITE_HOME_URL, wait_until="domcontentloaded", timeout=60000)
            print(f"[SITE] home={safe_url(page.url)}", flush=True)
            if page.url.startswith("chrome-error://"):
                raise RuntimeError("Chrome لم يستطع الوصول إلى الموقع. تحقق من الاتصال أو VPN.")

            await _wait_for_manual_login(page)

            set_status("opening_game", "فتح لعبة Aviator بعد تجهيز جلسة الدخول...")
            await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
            print(f"[GAME] loaded={safe_url(page.url)}", flush=True)
            if page.url.startswith("chrome-error://"):
                raise RuntimeError("Chrome لم يستطع الوصول إلى صفحة Aviator. تحقق من الاتصال أو VPN.")
        except Exception as exc:
            set_status("page_error", f"تعذر فتح الصفحة: {exc}")
            try:
                await context.close()
            except Exception:
                pass
            raise

        if SFS_DECODER_AVAILABLE:
            print("[SFS] Python binary decoder: enabled", flush=True)
        else:
            print("[SFS] Python binary decoder: unavailable; Chrome-side SmartFox event hook remains enabled", flush=True)
        set_status("waiting", "اترك صفحة Aviator مفتوحة. يجمع المضاعفات + بيانات العدل من HTTP/WebSocket/DOM/SmartFox events.")
        tracker.flush_live()

        probe_next = 0.0
        fairness_probe_round = None
        fairness_probe_next = 0.0
        try:
            while True:
                if page.is_closed():
                    raise RuntimeError("نافذة اللعبة أُغلقت.")

                now = time.monotonic()
                for pge in list(context.pages):
                    try:
                        frames = list(pge.frames)
                    except Exception:
                        continue

                    for frame in frames:
                        try:
                            scrape = await frame.evaluate(SCRAPE_JS)
                        except Exception:
                            continue
                        if isinstance(scrape, dict):
                            if now >= probe_next:
                                print(
                                    f"[PAGE] title={str(scrape.get('title') or '')[:120]!r} "
                                    f"frame={safe_url(scrape.get('href') or '')} "
                                    f"buf={len(scrape.get('buf') or [])}",
                                    flush=True,
                                )
                            tracker.ingest_dom(scrape)
                            combined = " ".join([
                                str(scrape.get("href") or ""),
                                str(scrape.get("text") or ""),
                                str(scrape.get("html") or ""),
                            ])
                            if re.search(r"(?i)seed|provably|fair|nonce", combined):
                                log_diag({
                                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "source": "dom_candidate",
                                    "url": safe_url(scrape.get("href") or ""),
                                    "title": scrape.get("title"),
                                    "frames": scrape.get("frames") or [],
                                    "labeled_count": len(scrape.get("labeled") or []),
                                    "attributes_count": len(scrape.get("attrs") or []),
                                    "meta_count": len(scrape.get("meta") or []),
                                    "script_count": len(scrape.get("scripts") or []),
                                })

                # Automatically open Fairness after each completed round.
                # This causes the game client to emit the same fairness request
                # that normally occurs when the user opens the Fairness panel.
                latest_round = tracker.last_round_id
                if latest_round and latest_round != fairness_probe_round and now >= fairness_probe_next:
                    clicked = await _trigger_fairness_ui(context, latest_round)
                    fairness_probe_next = now + 2.0
                    if clicked:
                        fairness_probe_round = latest_round

                if now >= probe_next:
                    probe_next = now + 5.0
                await asyncio.sleep(1.0)
        finally:
            try:
                await context.close()
            except Exception as exc:
                print(f"[COLLECTOR] context close warning: {exc}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        set_status("stopped", "تم إيقاف الجامع.")
    except Exception as exc:
        set_status("error", str(exc))
        raise
