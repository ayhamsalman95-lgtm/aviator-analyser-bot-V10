from __future__ import annotations

import asyncio
import json
import re
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
          return {
            title: clean(document.title),
            text: clean(document.body?.innerText || "").slice(0, 12000),
            nodes
          };
        }""")
        print("[FRAME-DIAG] title=" + info["title"], flush=True)
        print("[FRAME-DIAG] text=" + info["text"], flush=True)
        for n in info["nodes"]:
            print(
                "[FRAME-ELEMENT] "
                + json.dumps(n, ensure_ascii=False, separators=(",", ":")),
                flush=True,
            )
    except Exception as exc:
        print(f"[FRAME-DIAG] inspect failed: {exc}", flush=True)


async def main() -> None:
    cfg = load_config()
    profile = cfg.path("chrome_profile_dir")
    profile.mkdir(parents=True, exist_ok=True)
    out = cfg.path("logs_dir") / "network" / "fairness_ui.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        print("[START] Launching headless Chrome...", flush=True)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-notifications"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await goto_tolerant(page, cfg["site_home_url"], "site home")
            print(
                f"[NAV] Home current title={await page.title()} url={page.url}",
                flush=True,
            )

            if await page.locator("input[type='password']").count():
                print(
                    "[LOGIN] Login page detected. Headless mode cannot accept manual input; "
                    "waiting for an existing authenticated session.",
                    flush=True,
                )
                deadline = time.monotonic() + float(cfg["login_wait_s"])
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    if await page.locator("input[type='password']").count() == 0:
                        print("[LOGIN] Password form disappeared.", flush=True)
                        break
                else:
                    print("[LOGIN] No authenticated session detected.", flush=True)
                    return

            await goto_tolerant(page, cfg["game_url"], "game")
            print(
                f"[NAV] Game current title={await page.title()} url={page.url}",
                flush=True,
            )

            host = cfg["aviator_frame_host"]
            deadline = time.monotonic() + 90
            last_report = 0.0
            inspected = set()

            while time.monotonic() < deadline:
                frames = list(page.frames)
                host_frames = [f for f in frames if host in (f.url or "")]
                now = time.monotonic()
                if now - last_report >= 10:
                    print(
                        f"[SCAN] frames={len(frames)} target_frames={len(host_frames)} "
                        f"page_url={page.url}",
                        flush=True,
                    )
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
                                    print(
                                        f"[MENU] Clicking candidate selector={selector} "
                                        f"text={txt!r} aria={aria!r}",
                                        flush=True,
                                    )
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

            print("[TIMEOUT] No Fairness UI found within 90 seconds.", flush=True)
            print(f"[DIAG] page title={await page.title()} url={page.url}", flush=True)
            for frame in list(page.frames):
                if host in (frame.url or ""):
                    print("[TIMEOUT] Final target-frame diagnostic:", flush=True)
                    await inspect_frame(frame)
        finally:
            await context.close()


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


if __name__ == "__main__":
    asyncio.run(main())
