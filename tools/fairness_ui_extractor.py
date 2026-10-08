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


async def capture_fairness_settings(frame, out) -> bool:
    """Open the top-right game menu, enter Provably Fair Settings, save visible evidence, close the dialog."""
    menu = frame.locator(".user-wrapper .dropdown-toggle.user").first
    try:
        if not await menu.is_visible():
            print("[FAIRNESS] Top-right menu control is not visible.", flush=True)
            return False
        await menu.click(timeout=4000)
        await frame.wait_for_timeout(500)
    except Exception as exc:
        print(f"[FAIRNESS] Could not open top-right menu: {exc}", flush=True)
        return False

    settings = frame.get_by_text(FAIRNESS_RE)
    clicked = False
    for i in range(await settings.count()):
        item = settings.nth(i)
        try:
            if await item.is_visible():
                await item.click(timeout=4000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        print("[FAIRNESS] Menu opened, but 'Provably Fair Settings' was not clickable.", flush=True)
        return False

    await frame.wait_for_timeout(700)
    snapshot = await frame.evaluate(EXTRACT_JS)
    text = snapshot.get("visible_text", "")
    candidates = snapshot.get("candidates", [])
    if not candidates and not re.search(r"server.?seed|client.?seed|sha.?256|provably|nonce|hash", text, re.I):
        print("[FAIRNESS] Settings opened but no recognizable fairness fields were visible.", flush=True)
        print("[FAIRNESS] Visible text: " + text[:3000], flush=True)
        return False

    record = {
        "kind": "fairness_ui_evidence",
        "schema_version": 2,
        "captured_at": time.time(),
        "frame_url": frame.url,
        "evidence_scope": "visible Provably Fair Settings dialog",
        "snapshot": snapshot,
    }
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("[FAIRNESS] Settings evidence captured -> " + str(out), flush=True)
    print("[FAIRNESS] Snapshot text: " + json.dumps(text[:5000], ensure_ascii=False), flush=True)

    close_selectors = [
        "[role='dialog'] button[aria-label*='close' i]",
        "[aria-modal='true'] button[aria-label*='close' i]",
        "[class*='modal' i] button[aria-label*='close' i]",
        "[class*='popup' i] button[aria-label*='close' i]",
        "[role='dialog'] button.close",
        "[aria-modal='true'] button.close",
        "[class*='modal' i] button.close",
        "[class*='popup' i] button.close",
        "[role='dialog'] [class*='close' i]",
        "[aria-modal='true'] [class*='close' i]",
        "[class*='modal' i] [class*='close' i]",
        "[class*='popup' i] [class*='close' i]",
    ]
    closed = False
    for selector in close_selectors:
        loc = frame.locator(selector)
        for i in range(await loc.count()):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    await item.click(timeout=2000)
                    await frame.wait_for_timeout(300)
                    closed = True
                    break
            except Exception:
                continue
        if closed:
            break
    if closed:
        print("[FAIRNESS] Closed Provably Fair Settings dialog.", flush=True)
    else:
        print("[FAIRNESS] Evidence saved; close button not identified safely. Please close the dialog manually.", flush=True)
    return True


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

            # Detect the first live Aviator iframe in the 1xBet tab, then
            # wait for the user to open the same game in a NEW TAB (not a new
            # browser window). Never create a second Aviator session ourselves.
            await asyncio.sleep(10)
            selector = "iframe.casino-games-stand-game-frame__iframe"
            iframe = page.locator(selector).nth(0)
            await iframe.wait_for(state="visible", timeout=30000)
            src = await iframe.get_attribute("src")
            if not src:
                raise RuntimeError("Aviator iframe has no src")
            print(f"[AVIATOR-1] First game iframe URL: {src}", flush=True)
            print("[AVIATOR] Open the Aviator game in a NEW TAB in this same Chrome window.", flush=True)
            print("[AVIATOR] Waiting up to 120 seconds for the standalone Aviator tab...", flush=True)

            deadline = time.monotonic() + 120
            game_page = None
            last_report = 0.0
            while time.monotonic() < deadline:
                for candidate in context.pages:
                    if candidate == page:
                        continue
                    url = candidate.url or ""
                    if "spribegaming.com" in url.lower() and "aviator" in url.lower():
                        game_page = candidate
                        break
                if game_page is not None:
                    break
                now = time.monotonic()
                if now - last_report >= 10:
                    print(f"[WAIT] Chrome tabs={len(context.pages)}; standalone Aviator tab not detected yet.", flush=True)
                    last_report = now
                await asyncio.sleep(1)

            if game_page is None:
                raise RuntimeError("Standalone Aviator tab was not detected within 120 seconds")

            print(f"[AVIATOR-2] Standalone Aviator TAB detected: {game_page.url}", flush=True)
            print("[FAIRNESS] Opening Provably Fair Settings once to capture the pre-round values.", flush=True)
            ok = await capture_fairness_settings(game_page.main_frame, out)
            if not ok:
                print("[FAIRNESS] Could not capture settings automatically; leaving the tab open for diagnostics.", flush=True)
                await inspect_frame(game_page.main_frame)
            print("[NEXT] The settings dialog has been handled once. Send a screenshot of the round-by-round control you want monitored before we automate repeated captures.", flush=True)
        finally:
            # The browser was attached through CDP and is owned by noVNC/Chrome.
            # Never close the existing browser session from the extractor.
            pass


if __name__ == "__main__":
    asyncio.run(main())
