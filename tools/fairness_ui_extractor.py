from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

from playwright.async_api import async_playwright

from aviator.config import load_config


FAIRNESS_RE = re.compile(r"provably\s*fair\s*settings", re.I)

EXTRACT_JS = r"""() => {
  const clean = (v) => String(v ?? "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 &&
      cs.display !== "none" && cs.visibility !== "hidden" &&
      parseFloat(cs.opacity || "1") > 0.05;
  };
  const roots = Array.from(document.querySelectorAll(
    "[role='dialog'],[aria-modal='true'],[class*='modal' i]," +
    "[class*='popup' i],[class*='dialog' i],[class*='fair' i]"
  )).filter(visible);
  const source = roots.length ? roots : [document.body];
  return {
    url: location.href,
    title: clean(document.title),
    captured_at_ms: Date.now(),
    candidates: source.map(el => ({
      tag: el.tagName.toLowerCase(),
      id: clean(el.id),
      cls: clean(el.className),
      text: clean(el.innerText || el.textContent || "").slice(0, 16000),
      fields: Array.from(el.querySelectorAll(
        "input,textarea,[contenteditable='true'],[data-testid],[aria-label],[title]"
      )).filter(visible).slice(0, 200).map(x => ({
        tag: x.tagName.toLowerCase(),
        text: clean(x.innerText || x.textContent || ""),
        value: clean(x.value),
        aria: clean(x.getAttribute("aria-label")),
        title: clean(x.getAttribute("title")),
        name: clean(x.getAttribute("name")),
        testid: clean(x.getAttribute("data-testid")),
        type: clean(x.getAttribute("type"))
      }))
    })).filter(x => /provably|fair|server.?seed|player.?seed|client.?seed|sha.?256|sha.?512|round.?hash/i.test(
      x.text + " " + x.cls + " " + x.id
    )),
    visible_text: clean(document.body.innerText).slice(0, 20000)
  };
}"""


async def capture_fairness(frame, out) -> bool:
    loc = frame.get_by_text(FAIRNESS_RE)
    for i in range(await loc.count()):
        item = loc.nth(i)
        if await item.is_visible():
            print("[FAIRNESS] Found visible settings text; clicking.", flush=True)
            await item.click(timeout=2500)
            snapshot = await frame.evaluate(EXTRACT_JS)
            record = {
                "kind": "fairness_ui_evidence",
                "schema_version": 1,
                "captured_at": time.time(),
                "frame_url": frame.url,
                "snapshot": snapshot,
            }
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print("[FAIRNESS] UI evidence captured -> " + str(out), flush=True)
            return True
    return False


async def inspect_frame(frame) -> None:
    try:
        info = await frame.evaluate(r"""() => {
          const clean = v => String(v ?? "").replace(/\s+/g, " ").trim();
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 &&
              s.display !== "none" && s.visibility !== "hidden" &&
              parseFloat(s.opacity || "1") > 0.05;
          };
          const all = Array.from(document.querySelectorAll("*"));
          const visibleElements = all.filter(visible);
          const nodes = Array.from(document.querySelectorAll(
            "button,a,[role='button'],input,select,textarea"
          )).filter(visible).slice(0, 150).map(el => ({
            tag: el.tagName.toLowerCase(),
            text: clean(el.innerText || el.textContent || ""),
            aria: clean(el.getAttribute("aria-label")),
            title: clean(el.getAttribute("title")),
            cls: clean(el.className),
            type: clean(el.getAttribute("type"))
          }));
          const canvases = Array.from(document.querySelectorAll("canvas")).map(c => ({
            width: c.width,
            height: c.height,
            rect: (() => { const r=c.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })(),
            cls: clean(c.className),
            id: clean(c.id)
          }));
          const shadows = [];
          for (const el of all) {
            if (el.shadowRoot) {
              shadows.push({
                tag: el.tagName.toLowerCase(),
                id: clean(el.id),
                cls: clean(el.className),
                shadow_html: clean(el.shadowRoot.innerHTML).slice(0, 4000)
              });
            }
          }
          const scripts = Array.from(document.scripts).map(s => ({
            src: clean(s.src),
            type: clean(s.type),
            len: (s.textContent || "").length
          }));
          return {
            title: clean(document.title),
            ready_state: document.readyState,
            html_len: document.documentElement?.outerHTML?.length || 0,
            body_html: clean(document.body?.innerHTML || "").slice(0, 12000),
            text: clean(document.body?.innerText || "").slice(0, 12000),
            visible_element_count: visibleElements.length,
            nodes,
            canvases,
            shadow_count: shadows.length,
            shadows,
            scripts
          };
        }""")
        print("[FRAME-DIAG] title=" + info["title"], flush=True)
        print("[FRAME-DIAG] ready_state=" + info["ready_state"], flush=True)
        print(f"[FRAME-DIAG] html_len={info['html_len']} visible_elements={info['visible_element_count']}", flush=True)
        print("[FRAME-DIAG] body_html=" + info["body_html"], flush=True)
        print("[FRAME-DIAG] text=" + info["text"], flush=True)
        print("[FRAME-DIAG] canvases=" + json.dumps(info["canvases"], ensure_ascii=False), flush=True)
        print(f"[FRAME-DIAG] shadow_count={info['shadow_count']}", flush=True)
        for s in info["shadows"]:
            print("[FRAME-SHADOW] " + json.dumps(s, ensure_ascii=False, separators=(",", ":")), flush=True)
        for n in info["nodes"]:
            print("[FRAME-ELEMENT] " + json.dumps(n, ensure_ascii=False, separators=(",", ":")), flush=True)
        for s in info["scripts"]:
            if s["src"]:
                print("[FRAME-SCRIPT] " + json.dumps(s, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print(f"[FRAME-DIAG] inspect failed: {exc}", flush=True)


async def goto_tolerant(page, url: str, label: str):
    print(f"[NAV] Opening {label}...", flush=True)
    try:
        response = await page.goto(url, wait_until="commit", timeout=30000)
        print(
            f"[NAV] {label} navigation committed status={response.status if response else 'none'} "
            f"url={page.url}",
            flush=True,
        )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=90000)
        except Exception as exc:
            print(f"[NAV] {label} DOM load still pending; continuing: {exc}", flush=True)
        return response
    except Exception as exc:
        print(f"[NAV] {label} navigation warning; continuing: {exc}", flush=True)
        return None


async def main() -> None:
    cfg = load_config()
    out = cfg.path("logs_dir") / "network" / "fairness_ui.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        cdp_url = os.environ.get("AVIATOR_CDP_URL", "http://127.0.0.1:9222")
        print(f"[START] Connecting to existing Chrome via CDP {cdp_url}...", flush=True)
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("Connected to Chrome, but no browser context is available.")
        context = contexts[0]
        try:
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            print(f"[CDP] Attached pages={len(pages)} current_url={page.url}", flush=True)

            if not page.url or page.url == "about:blank":
                await goto_tolerant(page, cfg["site_home_url"], "site home")

            if await page.locator("input[type='password']").count():
                print("[LOGIN] Login page detected. Waiting for existing authenticated session.", flush=True)
                deadline = time.monotonic() + float(cfg["login_wait_s"])
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    if await page.locator("input[type='password']").count() == 0:
                        print("[LOGIN] Password form disappeared.", flush=True)
                        break
                else:
                    print("[LOGIN] No authenticated session detected.", flush=True)
                    return

            if "casino-search?game=52358" not in page.url:
                await goto_tolerant(page, cfg["game_url"], "game")
            print(f"[NAV] Game current title={await page.title()} url={page.url}", flush=True)

            # 1xBet currently embeds Aviator at launch.spribegaming.com.
            # Do not rely on a stale hard-coded frame host; identify the live
            # visible game iframe and ignore unrelated/stale frames.
            selector = "iframe.casino-games-stand-game-frame__iframe"

            async def find_live_game_frames():
                locator = page.locator(selector)
                candidates = []
                for i in range(await locator.count()):
                    try:
                        iframe = locator.nth(i)
                        if not await iframe.is_visible():
                            continue
                        frame = await iframe.content_frame()
                        if frame is None:
                            continue
                        text = ""
                        try:
                            text = (await frame.locator("body").inner_text(timeout=1500))[:4000]
                        except Exception:
                            pass
                        low = text.lower()
                        if "session ended" in low and "opened in another browser window" in low:
                            continue
                        score = 0
                        if "spribegaming.com" in (frame.url or "").lower():
                            score += 3
                        for needle in ("provably fair", "all bets", "previous", "top", "bets", "total win", "powered by"):
                            if needle in low:
                                score += 1
                        candidates.append((score, frame))
                    except Exception:
                        continue
                candidates.sort(key=lambda item: item[0], reverse=True)
                return [frame for _, frame in candidates]

            deadline = time.monotonic() + 120
            last_report = 0.0
            inspected = set()

            while time.monotonic() < deadline:
                frames = list(page.frames)
                host_frames = await find_live_game_frames()
                now = time.monotonic()
                if now - last_report >= 10:
                    print(f"[SCAN] frames={len(frames)} live_game_frames={len(host_frames)} page_url={page.url}", flush=True)
                    for f in frames:
                        print(f"[FRAME] {f.url}", flush=True)
                    last_report = now

                for frame in host_frames:
                    if await capture_fairness(frame, out):
                        return
                    frame_key = frame.url
                    if frame_key not in inspected:
                        inspected.add(frame_key)
                        print("[FRAME-DIAG] Inspecting target iframe once.", flush=True)
                        await inspect_frame(frame)

                    menu_candidates = [
                        "div.dropdown-toggle.button",
                        "[class*='dropdown-toggle']",
                        "[aria-label*='menu' i]",
                        "[title*='menu' i]",
                        "button"
                    ]
                    clicked = False
                    for selector in menu_candidates:
                        loc = frame.locator(selector)
                        count = await loc.count()
                        for i in range(min(count, 20)):
                            item = loc.nth(i)
                            try:
                                if await item.is_visible():
                                    txt = (await item.inner_text()).strip()
                                    aria = await item.get_attribute("aria-label")
                                    title = await item.get_attribute("title")
                                    cls = await item.get_attribute("class") or ""
                                    if selector == "button" and not (
                                        "menu" in f"{txt} {aria} {title} {cls}".lower()
                                        or "dropdown" in cls.lower()
                                    ):
                                        continue
                                    print(f"[MENU] Clicking candidate selector={selector} text={txt!r} aria={aria!r}", flush=True)
                                    await item.click(timeout=2500)
                                    await frame.wait_for_timeout(1000)
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if clicked:
                            break

                    if await capture_fairness(frame, out):
                        return

                await asyncio.sleep(1)

            print("[TIMEOUT] No Fairness UI found within 120 seconds.", flush=True)
            print(f"[DIAG] page title={await page.title()} url={page.url}", flush=True)
            for frame in await find_live_game_frames():
                print("[TIMEOUT] Final live-game-frame diagnostic:", flush=True)
                await inspect_frame(frame)
        finally:
            # The browser was attached through CDP and is owned by noVNC/Chrome.
            # Never close the existing browser session from the extractor.
            pass


if __name__ == "__main__":
    asyncio.run(main())
