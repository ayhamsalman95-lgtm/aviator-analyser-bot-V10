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
                            self.tracker.completed_queue.clear()
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
                                            self.tracker.completed_queue.clear()
                                            return
                                    except Exception:
                                        continue
                            except Exception:
                                continue

                    # Fallback for Aviator's icon-only hamburger menu (three horizontal bars).
                    # Restrict the search to visible icon buttons in the upper-right area of the game frame.
                    try:
                        viewport = await frame.evaluate("""() => ({
                            w: window.innerWidth || document.documentElement.clientWidth || 0,
                            h: window.innerHeight || document.documentElement.clientHeight || 0
                        })""")
                        candidates = frame.locator("button:has(svg), [role='button']:has(svg)")
                        scored = []
                        for i in range(await candidates.count()):
                            candidate = candidates.nth(i)
                            try:
                                if not await candidate.is_visible():
                                    continue
                                box = await candidate.bounding_box()
                                if not box:
                                    continue
                                if box["x"] < viewport["w"] * 0.72 or box["y"] > viewport["h"] * 0.32:
                                    continue
                                info = await candidate.evaluate("""el => {
                                    const svg = el.querySelector("svg");
                                    if (!svg) return null;
                                    const lineEls = Array.from(svg.querySelectorAll("line,rect,path,polyline"));
                                    const horizontal = lineEls.filter(x => {
                                        const r = x.getBoundingClientRect();
                                        return r.width > r.height * 2.5;
                                    }).length;
                                    const html = (svg.outerHTML || "").slice(0, 1200);
                                    return {horizontal, html};
                                }""")
                                if not info:
                                    continue
                                # Prefer an icon containing several horizontal strokes,
                                # then prefer the candidate furthest to the right.
                                score = (
                                    min(int(info.get("horizontal") or 0), 3) * 100000
                                    + int(box["x"]) * 10
                                    - int(box["y"])
                                )
                                scored.append((score, candidate, info, box))
                            except Exception:
                                continue
                        scored.sort(key=lambda item: item[0], reverse=True)
                        for _, candidate, info, _ in scored:
                            if int(info.get("horizontal") or 0) < 2:
                                continue
                            await candidate.click(timeout=2500)
                            await frame.wait_for_timeout(300)
                            loc = frame.get_by_text(settings_re)
                            for j in range(await loc.count()):
                                menu_item = loc.nth(j)
                                try:
                                    if await menu_item.is_visible():
                                        await menu_item.click(timeout=2500)
                                        print("[FAIRNESS] opened Provably Fair Settings via hamburger menu (rate limited)", flush=True)
                                        self.tracker.completed_queue.clear()
                                        return
                                except Exception:
                                    continue
                            # A click occurred but did not expose the settings item.
                            # Do not keep clicking other controls in the same pass.
                            break
                    except Exception as hamburger_exc:
                        self.netlog.write({
                            "kind": "fairness_hamburger_error",
                            "error": str(hamburger_exc),
                        })

                    # Diagnostic probe: the game menu button can be an icon-only control
                    # with no aria-label/title/text. Record only safe UI metadata, never cookies,
                    # tokens, page source, or arbitrary DOM values.
                    try:
                        probe = await frame.evaluate("""() => {
                            const visible = (el) => {
                                const r = el.getBoundingClientRect();
                                const cs = getComputedStyle(el);
                                return !!(r.width && r.height && cs.visibility !== "hidden" &&
                                           cs.display !== "none" && parseFloat(cs.opacity || "1") > 0.05);
                            };
                            const clean = (v) => String(v || "").replace(/\\s+/g, " ").trim().slice(0, 140);
                            const els = Array.from(document.querySelectorAll(
                                "button,[role='button'],[aria-label],a,[data-testid]"
                            )).filter(visible).map((el) => {
                                const r = el.getBoundingClientRect();
                                return {
                                    tag: el.tagName.toLowerCase(),
                                    text: clean(el.innerText),
                                    aria: clean(el.getAttribute("aria-label")),
                                    title: clean(el.getAttribute("title")),
                                    role: clean(el.getAttribute("role")),
                                    testid: clean(el.getAttribute("data-testid")),
                                    id: clean(el.id),
                                    cls: clean(el.className),
                                    x: Math.round(r.x), y: Math.round(r.y),
                                    w: Math.round(r.width), h: Math.round(r.height)
                                };
                            });
                            return els
                                .sort((a,b) => (b.x + b.w) - (a.x + a.w) || a.y - b.y)
                                .slice(0, 40);
                        }()""")
                        self.netlog.write({
                            "kind": "fairness_dom_probe",
                            "frame_url": safe_url(frame.url),
                            "candidates": probe,
                        })
                    except Exception as probe_exc:
                        self.netlog.write({
                            "kind": "fairness_dom_probe_error",
                            "frame_url": safe_url(frame.url),
                            "error": str(probe_exc),
                        })
                    self.netlog.write({"kind": "fairness_menu_not_found"})
                except Exception as exc:
                    self.netlog.write({"kind": "fairness_click_error", "error": str(exc)})
                    return