from pathlib import Path

p = Path("collector_fixed_v11.py")
s = p.read_text(encoding="utf-8")

start = s.index("async def _trigger_fairness_ui(")
end = s.index("\nasync def ", start + 10)

new_func = r'''async def _trigger_fairness_ui(context, round_id: str) -> bool:
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
'''

p.write_text(s[:start] + new_func + s[end:], encoding="utf-8")

print("PATCH APPLIED OK")