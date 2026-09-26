"""Telegram delivery from the persistent outbox (library independent).

The collector only writes outbox rows. The bot reads them, so a slow/failed
Telegram API can never block or crash data collection, and no event is missed
because of a polling window. Deduplication is structural: one delivery row per
(outbox event, chat). Event keys are built from round ids, not message text.
"""
from __future__ import annotations

import json
import re
import time
from typing import Awaitable, Callable, Optional

_MDV2 = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md_v2(text: str) -> str:
    return _MDV2.sub(r"\\\1", str(text))


def _pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{100 * p:.1f}%"


def format_prediction(payload: dict) -> str:
    out = payload.get("output") or {}
    models = out.get("models") or {}
    rec = models.get("empirical_recent", {})
    theo = models.get("theoretical", {})
    lines = [f"🟡 الجولة {payload['round_id']} — ما قبل الإقلاع",
             "تم تجميد التقدير عند بدء الرهانات (newStateId=1) قبل معرفة النتيجة.", "",
             "احتمال أن تصل الجولة إلى العتبة أو تتجاوزها:"]
    for t in out.get("thresholds", []):
        k = f"{float(t):g}"
        lines.append(f"≥{k}x: {_pct(rec.get(k))} (النظري {_pct(theo.get(k))})")
    lines += ["", f"البيانات المستخدمة: {out.get('n_all', 0)} جولة سابقة فقط.",
              "⚠️ احتمالات إحصائية فقط، وليست تنبؤًا مؤكدًا. الجولات مصممة لتكون مستقلة."]
    return "\n".join(lines)


def format_result(payload: dict, store=None) -> str:
    lines = [f"🆕 انتهت الجولة {payload['round_id']}: {float(payload['multiplier']):.2f}x"]
    if store is not None:
        pred = store.get_prediction(int(payload["round_id"]))
        if pred is not None:
            lines.append("كان لهذه الجولة تقدير مُجمَّد مسبقًا؛ النتيجة أُرفقت به بشكل منفصل.")
    return "\n".join(lines)


def format_fairness(payload: dict) -> str:
    names = {"verified": "✅ تم التحقق (أُعيد إنتاج النتيجة)", "mismatch": "❌ غير مطابق",
             "incomplete": "⏳ بيانات غير مكتملة", "conflict": "⚠️ بيانات متعارضة"}
    return (f"🔐 عدالة الجولة {payload['round_id']}: {names.get(payload['status'], payload['status'])}\n"
            f"{payload.get('detail') or ''}")


def format_event(kind: str, payload: dict, store=None) -> Optional[str]:
    if kind == "prediction_frozen":
        return format_prediction(payload)
    if kind == "round_completed":
        return format_result(payload, store)
    if kind == "fairness_update":
        if payload.get("status") == "incomplete":
            return None  # don't spam partial evidence
        return format_fairness(payload)
    return None


def is_forbidden(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return name == "forbidden" or "bot was blocked" in str(exc).lower() or "chat not found" in str(exc).lower()


class Notifier:
    def __init__(self, store, send: Callable[[int, str], Awaitable[None]], cfg=None, clock=time.time):
        self.store = store
        self.send = send
        self.clock = clock
        self.max_attempts = int(cfg["telegram_max_attempts"]) if cfg else 5
        self.max_age = float(cfg["telegram_event_max_age_s"]) if cfg else 900.0

    def _stale(self, ev) -> bool:
        if ev["kind"] == "prediction_frozen" and ev["round_id"] is not None:
            return self.store.get_round(int(ev["round_id"])) is not None
        return False

    async def deliver_pending(self) -> dict:
        counts = {"sent": 0, "failed": 0, "skipped": 0}
        now = self.clock()
        subs = self.store.active_subscribers()
        if not subs:
            return counts
        for ev in self.store.pending_outbox(now - self.max_age):
            payload = json.loads(ev["payload_json"])
            stale = self._stale(ev)
            text = None if stale else format_event(ev["kind"], payload, self.store)
            for sub in subs:
                chat_id = int(sub["chat_id"])
                if sub["created_at"] > ev["created_at"]:
                    continue  # no backlog for new subscribers
                d = self.store.delivery(ev["id"], chat_id)
                if d is not None and d["status"] in ("sent", "skipped"):
                    continue
                if d is not None and d["status"] == "failed":
                    if d["attempts"] >= self.max_attempts:
                        continue
                    if now - d["updated_at"] < min(60.0, 2 ** d["attempts"]):
                        continue  # exponential backoff per chat
                if text is None:
                    self.store.record_delivery(ev["id"], chat_id, "skipped", "stale_or_silent")
                    counts["skipped"] += 1
                    continue
                try:
                    await self.send(chat_id, text)
                except Exception as exc:  # failure isolated to this chat/event
                    if is_forbidden(exc):
                        self.store.unsubscribe(chat_id)
                        self.store.record_delivery(ev["id"], chat_id, "skipped", f"forbidden: {exc}")
                        counts["skipped"] += 1
                    else:
                        self.store.record_delivery(ev["id"], chat_id, "failed", f"{type(exc).__name__}: {exc}")
                        counts["failed"] += 1
                    continue
                self.store.record_delivery(ev["id"], chat_id, "sent")
                counts["sent"] += 1
        return counts
