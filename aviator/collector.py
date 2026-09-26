"""Chrome/Playwright collector for Spribe Aviator (game 52358).

* Supervisor loop with exponential backoff: any session failure (page closed,
  navigation error, login timeout, stale stream) reconnects instead of exiting.
* Watchdog: no SmartFox command for `watchdog_no_event_s` -> reconnect.
* Every packet of every binary WebSocket frame is decoded with sfs2x-py.
* The Chrome-side SmartFox dispatchEvent hook is a second, independent source;
  both feed the same RoundTracker and duplicates collapse on round id.
* Telegram is NOT used here (outbox only).
* Automatic fairness clicking is OFF by default (fairness_autoclick=false).
"""
from __future__ import annotations

import asyncio
import re
import time

from .config import load_config
from .db import Store
from .netlog import RotatingJsonlLog, safe_url
from .sfs_codec import SfsDecoder, binary_summary, dependency_report, unwrap_browser_event
from .tracker import RoundTracker

INJECT_JS = r"""
(() => {
  if (window.__aviatorHooked) return;
  window.__aviatorHooked = true;
  window.__aviatorBuf = [];
  const push = (item) => {
    try {
      window.__aviatorBuf.push(item);
      if (window.__aviatorBuf.length > 2000) window.__aviatorBuf.splice(0, 1000);
    } catch (e) {}
  };
  const seen = new WeakSet();
  const snap = (v, d = 0) => {
    if (d > 6) return null;
    if (v === null || v === undefined) return v;
    const t = typeof v;
    if (t === "string" || t === "number" || t === "boolean") return v;
    if (t === "bigint") return String(v);
    if (t !== "object") return null;
    if (seen.has(v)) return null;
    seen.add(v);
    try {
      if (typeof v.getKeysArray === "function") {
        const o = {};
        for (const k of (v.getKeysArray() || []).slice(0, 200)) { try { o[String(k)] = snap(v.get(k), d + 1); } catch (e) {} }
        return o;
      }
      if (typeof v.size === "function" && typeof v.get === "function") {
        const a = [];
        for (let i = 0; i < Math.min(v.size(), 500); i++) { try { a.push(snap(v.get(i), d + 1)); } catch (e) {} }
        return a;
      }
    } catch (e) {}
    if (Array.isArray(v)) return v.slice(0, 500).map(x => snap(x, d + 1));
    const o = {};
    for (const k of Object.keys(v).slice(0, 200)) {
      if (/^(password|passwd|token|authorization|cookie|secret|session)$/i.test(k)) continue;
      try { o[k] = snap(v[k], d + 1); } catch (e) {}
    }
    return o;
  };
  const hook = () => {
    try {
      const C = window.SFS2X && window.SFS2X.SmartFox;
      if (!C || !C.prototype || typeof C.prototype.dispatchEvent !== "function") return false;
      if (C.prototype.__aviatorHooked) return true;
      const orig = C.prototype.dispatchEvent;
      C.prototype.dispatchEvent = function(evt) {
        try {
          const type = String(evt && evt.type || "");
          if (type === "extensionResponse") push({ t: Date.now(), event_type: type, data: snap(evt) });
        } catch (e) {}
        return orig.apply(this, arguments);
      };
      C.prototype.__aviatorHooked = true;
      return true;
    } catch (e) { return false; }
  };
  hook();
  let tries = 0;
  const timer = setInterval(() => { tries += 1; if (hook() || tries > 240) clearInterval(timer); }, 500);
})();
"""

DRAIN_JS = "() => (window.__aviatorBuf ? window.__aviatorBuf.splice(0, window.__aviatorBuf.length) : [])"


class SessionStale(RuntimeError):
    pass


class Collector:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.store = Store.from_config(self.cfg)
        logs = self.cfg.logs_dir
        self.netlog = RotatingJsonlLog(logs / "network" / "game_network.jsonl",
                                       int(self.cfg["network_log_max_bytes"]), int(self.cfg["network_log_backups"]))
        self.tracker = RoundTracker(self.store, self.cfg, netlog=self.netlog)
        self.decoder = SfsDecoder()
        self._last_fairness_click = 0.0

    # --------------------------------------------------------------- frames
    def on_binary_frame(self, data: bytes, ws_url: str) -> None:
        res = self.decoder.decode_frame(data)
        if not res.decoder_available:
            self.netlog.write({"kind": "ws_binary_undecoded", "url": safe_url(ws_url), **binary_summary(data)})
            return
        for cmd, params in res.commands:
            self.netlog.write({"kind": "sfs_decoded", "url": safe_url(ws_url), "command": cmd, "params": params})
            try:
                self.tracker.handle(cmd, params, origin="py-sfs")
            except Exception as exc:  # one bad packet never kills the session
                self.netlog.write({"kind": "tracker_error", "command": cmd, "error": f"{type(exc).__name__}: {exc}"})
        if res.error or res.leftover:
            self.netlog.write({"kind": "sfs_decode_error", "url": safe_url(ws_url), "error": res.error,
                               "packets": res.packets, "leftover": res.leftover, **binary_summary(data)})

    def on_text_frame(self, text: str, ws_url: str) -> None:
        if self.cfg["log_text_frames"]:
            self.netlog.write({"kind": "ws_text", "url": safe_url(ws_url), "length": len(text),
                               "payload": text[:20000]})

    def on_browser_events(self, events: list) -> None:
        for ev in events or []:
            unwrapped = unwrap_browser_event(ev)
            if unwrapped is None:
                continue
            cmd, params = unwrapped
            try:
                self.tracker.handle(cmd, params, origin="js-sfs")
            except Exception as exc:
                self.netlog.write({"kind": "tracker_error", "command": cmd, "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------- fairness
    async def maybe_open_fairness(self, context) -> None:
        """Disabled by default. When enabled: rate limited, event-driven, no control dumps."""
        if not self.cfg["fairness_autoclick"] or not self.tracker.completed_queue:
            return
        self.tracker.completed_queue.clear()
        now = time.monotonic()
        if now - self._last_fairness_click < float(self.cfg["fairness_autoclick_min_interval_s"]):
            return
        self._last_fairness_click = now
        settings_re = re.compile(r"provably\s*fair\s*settings", re.I)
        host = self.cfg["aviator_frame_host"]
        for page in list(context.pages):
            for frame in list(page.frames):
                if host not in (frame.url or ""):
                    continue
                try:
                    loc = frame.get_by_text(settings_re)
                    if await loc.count() > 0:
                        visible = []
                        for i in range(await loc.count()):
                            candidate = loc.nth(i)
                            try:
                                if await candidate.is_visible():
                                    visible.append(candidate)
                            except Exception:
                                pass
                        if visible:
                            await visible[0].click(timeout=2500)
                            print("[FAIRNESS] opened Provably Fair Settings (rate limited)", flush=True)
                            return

                    # The settings item is normally inside the game's "..." menu.
                    # Open a visible menu/options button first, then retry the item.
                    menu_re = re.compile(r"(more|menu|options|additional)", re.I)
                    for selector in ("button", "[role='button']"):
                        buttons = frame.locator(selector)
                        for i in range(await buttons.count()):
                            button = buttons.nth(i)
                            try:
                                if not await button.is_visible():
                                    continue
                                label = " ".join(filter(None, [
                                    await button.get_attribute("aria-label"),
                                    await button.get_attribute("title"),
                                    await button.inner_text(),
                                ]))
                                if not menu_re.search(label or ""):
                                    continue
                                await button.click(timeout=2500)
                                await frame.wait_for_timeout(300)
                                loc = frame.get_by_text(settings_re)
                                for j in range(await loc.count()):
                                    candidate = loc.nth(j)
                                    try:
                                        if await candidate.is_visible():
                                            await candidate.click(timeout=2500)
                                            print("[FAIRNESS] opened Provably Fair Settings via menu (rate limited)", flush=True)
                                            return
                                    except Exception:
                                        continue
                            except Exception:
                                continue

                    self.netlog.write({"kind": "fairness_menu_not_found"})
                except Exception as exc:
                    self.netlog.write({"kind": "fairness_click_error", "error": str(exc)})
                    return

    # -------------------------------------------------------------- session
    async def _wait_login(self, page) -> None:
        async def needed() -> bool:
            try:
                url = (page.url or "").lower()
                if await page.locator("input[type='password']").count() > 0:
                    return True
                return any(m in url for m in ("/login", "/signin", "/sign-in", "/auth"))
            except Exception:
                return False
        if not await needed():
            return
        self.store.set_status("login_required", "سجّل الدخول يدويًا في نافذة Chrome")
        print("[LOGIN] سجّل الدخول يدويًا في نافذة Chrome المفتوحة.", flush=True)
        deadline = time.monotonic() + float(self.cfg["login_wait_s"])
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if not await needed():
                return
        raise RuntimeError("login timeout")

    async def run_session(self) -> None:
        from playwright.async_api import async_playwright  # imported lazily (optional in tests)
        profile = self.cfg.path("chrome_profile_dir")
        profile.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile), channel="chrome", headless=False,
                viewport={"width": 1440, "height": 900}, args=["--disable-notifications"])
            try:
                await context.add_init_script(INJECT_JS)

                def on_ws(ws):
                    url = ws.url
                    self.netlog.write({"kind": "ws_open", "url": safe_url(url)})

                    def received(payload):
                        try:
                            if isinstance(payload, (bytes, bytearray, memoryview)):
                                self.on_binary_frame(bytes(payload), url)
                            else:
                                self.on_text_frame(str(payload), url)
                        except Exception as exc:
                            self.netlog.write({"kind": "frame_handler_error", "error": str(exc)})
                    ws.on("framereceived", received)
                    ws.on("close", lambda *_: self.netlog.write({"kind": "ws_close", "url": safe_url(url)}))

                def on_response(resp):
                    try:
                        self.netlog.write({"kind": "http_response", "url": safe_url(resp.url),
                                           "status": resp.status})
                    except Exception:
                        pass

                def attach(page):
                    page.on("websocket", on_ws)
                    page.on("response", on_response)

                for pg in context.pages:
                    attach(pg)
                context.on("page", attach)
                page = context.pages[0] if context.pages else await context.new_page()

                self.store.set_status("opening", "فتح الموقع")
                await page.goto(self.cfg["site_home_url"], wait_until="domcontentloaded", timeout=60000)
                await self._wait_login(page)
                self.store.set_status("opening_game", "فتح Aviator 52358")
                await page.goto(self.cfg["game_url"], wait_until="domcontentloaded", timeout=60000)
                if page.url.startswith("chrome-error://"):
                    raise RuntimeError("Chrome could not reach the game page")
                self.store.set_status("collecting", "يجمع النتائج من SmartFox",
                                      decoder=dependency_report())
                self.tracker.last_event_at = time.time()
                watchdog = float(self.cfg["watchdog_no_event_s"])
                while True:
                    if page.is_closed():
                        raise RuntimeError("game page closed")
                    for pg in list(context.pages):
                        for frame in list(pg.frames):
                            try:
                                events = await frame.evaluate(DRAIN_JS)
                            except Exception:
                                continue
                            self.on_browser_events(events)
                    await self.maybe_open_fairness(context)
                    if time.time() - self.tracker.last_event_at > watchdog:
                        raise SessionStale(f"no SmartFox command for {watchdog:.0f}s")
                    self.store.set_status("collecting", "يجمع النتائج", **self.tracker.snapshot())
                    await asyncio.sleep(0.5)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    async def supervise(self) -> None:
        backoff = [float(x) for x in self.cfg["reconnect_backoff_s"]] or [10.0]
        failures = 0
        rep = dependency_report()
        print(f"[SFS] sfs2x-py available={rep['available']} {rep.get('error') or ''}", flush=True)
        while True:
            started = time.monotonic()
            try:
                await self.run_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if time.monotonic() - started > 300:
                    failures = 0  # the session was healthy for a while
                delay = backoff[min(failures, len(backoff) - 1)]
                failures += 1
                self.store.set_status("reconnecting", f"{type(exc).__name__}: {exc}", retry_in_s=delay,
                                      failures=failures)
                print(f"[COLLECTOR] session ended: {type(exc).__name__}: {exc}; reconnect in {delay:.0f}s",
                      flush=True)
                await asyncio.sleep(delay)


def main() -> None:
    col = Collector()
    try:
        asyncio.run(col.supervise())
    except KeyboardInterrupt:
        col.store.set_status("stopped", "stopped by user")
        print("[COLLECTOR] stopped", flush=True)
