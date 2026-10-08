from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from playwright.async_api import async_playwright

from aviator.config import load_config


FAIRNESS_RE = re.compile(r"provably\s*fair\s*settings", re.I)

EXTRACT_JS = """() => {
  const clean = (v) => String(v ?? "").replace(/\\s+/g, " ").trim();
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


async def main() -> None:
    cfg = load_config()
    profile = cfg.path("chrome_profile_dir")
    profile.mkdir(parents=True, exist_ok=True)
    out = cfg.path("logs") / "network" / "fairness_ui.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-notifications"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(cfg["site_home_url"], wait_until="domcontentloaded", timeout=60000)

            if await page.locator("input[type='password']").count():
                print("[LOGIN] Log in manually in Chrome; waiting up to 180s.", flush=True)
                deadline = time.monotonic() + float(cfg["login_wait_s"])
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    if await page.locator("input[type='password']").count() == 0:
                        break

            await page.goto(cfg["game_url"], wait_until="domcontentloaded", timeout=60000)
            host = cfg["aviator_frame_host"]

            while True:
                found = False
                for frame in list(page.frames):
                    if host not in (frame.url or ""):
                        continue
                    loc = frame.get_by_text(FAIRNESS_RE)
                    for i in range(await loc.count()):
                        item = loc.nth(i)
                        if await item.is_visible():
                            await item.click(timeout=2500)
                            found = True
                            break
                    if found:
                        break

                    icons = frame.locator("div.dropdown-toggle.button > .button-icon")
                    for i in range(await icons.count()):
                        icon = icons.nth(i)
                        if await icon.is_visible():
                            await icon.locator("..").click(timeout=2500)
                            await frame.wait_for_timeout(400)
                            loc = frame.get_by_text(FAIRNESS_RE)
                            for j in range(await loc.count()):
                                item = loc.nth(j)
                                if await item.is_visible():
                                    await item.click(timeout=2500)
                                    found = True
                                    break
                        if found:
                            break
                    if found:
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
                        return

                await asyncio.sleep(1)
        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
