"""Run the Aviator collector against an already-open Chrome via CDP.

Usage:
    AVIATOR_CDP_URL=http://127.0.0.1:9222 python -m tools.cdp_collector

The browser stays responsible for login/session state. This script only consumes
WebSocket frames and client-visible browser events from the selected Aviator page.
"""
from __future__ import annotations

import asyncio
import os
import time

from playwright.async_api import async_playwright

from aviator.collector import Collector
from aviator.sfs_codec import dependency_report, unwrap_browser_event


def pick_page(context):
    pages = [p for p in context.pages if "game=52358" in (p.url or "")]
    if not pages:
        raise RuntimeError("No Aviator game=52358 page found in the connected Chrome")
    return pages[0]


async def main() -> None:
    cdp_url = os.getenv("AVIATOR_CDP_URL", "http://127.0.0.1:9222")
    col = Collector()
    rep = dependency_report()
    print(f"[CDP] connecting to {cdp_url}; sfs2x available={rep['available']}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        page = pick_page(context)

        def on_ws(ws):
            url = ws.url
            print(f"[CDP] WS {url}", flush=True)
            col.netlog.write({"kind": "ws_open", "url": url})

            def received(payload):
                try:
                    if isinstance(payload, (bytes, bytearray, memoryview)):
                        col.on_binary_frame(bytes(payload), url)
                    else:
                        col.on_text_frame(str(payload), url)
                except Exception as exc:
                    col.netlog.write({
                        "kind": "frame_handler_error",
                        "error": f"{type(exc).__name__}: {exc}",
                    })

            ws.on("framereceived", received)
            ws.on("close", lambda *_: col.netlog.write({"kind": "ws_close", "url": url}))

        def on_response(resp):
            try:
                col.netlog.write({
                    "kind": "http_response",
                    "url": resp.url,
                    "status": resp.status,
                })
            except Exception:
                pass

        page.on("websocket", on_ws)
        page.on("response", on_response)

        print(f"[CDP] PAGE {page.url}", flush=True)
        # Reload after listeners are attached so the existing game WebSocket is recreated
        # under our listener. The logged-in Chrome profile/session remains in control.
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        col.tracker.last_event_at = time.time()
        print("[CDP] collecting...", flush=True)

        while True:
            if page.is_closed():
                raise RuntimeError("Aviator page closed")

            for pg in list(context.pages):
                if "game=52358" not in (pg.url or ""):
                    continue
                for frame in list(pg.frames):
                    try:
                        events = await frame.evaluate(
                            "() => (window.__aviatorBuf ? "
                            "window.__aviatorBuf.splice(0, window.__aviatorBuf.length) : [])"
                        )
                    except Exception:
                        continue
                    col.on_browser_events(events)

            col.store.set_status(
                "collecting",
                "يجمع النتائج عبر Chrome CDP",
                **col.tracker.snapshot(),
            )
            if time.time() - col.tracker.last_event_at > 180:
                raise RuntimeError("No decoded SmartFox command for 180s")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
