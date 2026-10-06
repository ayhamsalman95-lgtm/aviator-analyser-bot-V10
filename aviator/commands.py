"""Telegram command logic, independent of the Telegram library (testable)."""
from __future__ import annotations

import json
from typing import Sequence

from .predict import probabilities


def _is_admin(cfg, chat_id: int) -> bool:
    admins = [int(x) for x in (cfg.get("admin_chat_ids") or [])]
    return not admins or chat_id in admins


def cmd_start(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    store.subscribe(chat_id)
    return ("أهلًا. تم تسجيلك لتنبيهات Aviator 52358 (قبل الجولة، النتيجة، والتحقق من العدالة).\n"
            "الأوامر: /status /last /stats /predict /round /verify <id> /add /manual /clear confirm /stop")


def cmd_stop(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    store.unsubscribe(chat_id)
    return "تم إيقاف التنبيهات. أعد التفعيل بـ /start"


def parse_values(args: Sequence[str]) -> list[float]:
    out = []
    for token in args:
        try:
            v = float(str(token).replace(",", ".").lower().rstrip("x"))
        except ValueError:
            continue
        if v >= 1.0:
            out.append(round(v, 2))
    return out


def cmd_add(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    values = parse_values(args)
    if not values:
        return "استخدم: /add 1.12 2.31 4.19"
    total = store.add_manual(chat_id, values)
    return (f"أُضيفت {len(values)} قيمة إلى جدول الإدخالات اليدوية/التجريبية (المجموع {total}).\n"
            "هذه القيم منفصلة تمامًا ولا تدخل في بيانات البحث ولا في التنبؤات.")


def cmd_manual(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    vals = store.manual_values(chat_id)
    if not vals:
        return "لا توجد إدخالات يدوية."
    tail = " | ".join(f"{v:.2f}x" for v in vals[-20:])
    return f"إدخالاتك اليدوية ({len(vals)}):\n{tail}"


def cmd_clear(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    args = [a.lower() for a in args]
    if "confirm" not in args:
        return ("/clear يحذف إدخالاتك اليدوية فقط (/add). بيانات البحث والأدلة لا تُحذف أبدًا.\n"
                "للتأكيد: /clear confirm")
    if "all" in args:
        if not (cfg.get("admin_chat_ids") and _is_admin(cfg, chat_id)):
            return "حذف إدخالات الجميع متاح فقط لمعرّفات admin_chat_ids المحددة في الإعدادات."
        n = store.clear_manual(None)
    else:
        n = store.clear_manual(chat_id)
    return f"حُذفت {n} قيمة يدوية. بيانات البحث لم تُمس."


def cmd_last(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    rows = store.conn.execute("SELECT round_id, multiplier FROM rounds ORDER BY round_id DESC LIMIT 20").fetchall()
    if not rows:
        return "لا توجد جولات محفوظة بعد."
    return "آخر 20 جولة:\n" + "\n".join(f"{r['round_id']}: {r['multiplier']:.2f}x" for r in rows)


def cmd_stats(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    from .stats import threshold_table
    rows = store.rounds_chronological()
    if not rows:
        return "لا توجد بيانات."
    vals = [r["multiplier"] for r in rows]
    lines = [f"📊 عدد الجولات الصالحة: {len(vals)}"]
    for r in threshold_table(vals, [float(t) for t in cfg["thresholds"]], float(cfg["rtp"])):
        lines.append(f"≥{r['threshold']:g}x: {100 * r['observed']:.1f}% (النظري {100 * r['theoretical']:.1f}%)")
    q = sum(store.quarantine_counts().values())
    lines.append(f"سجلات في الحجر: {q}")
    lines.append(f"التحقق من العدالة: {json.dumps(store.verification_counts(), ensure_ascii=False)}")
    return "\n".join(lines)


def cmd_predict(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    row = store.conn.execute("SELECT * FROM predictions WHERE resolved_at IS NULL "
                             "ORDER BY round_id DESC LIMIT 1").fetchone()
    if row is not None:
        from .notify import format_prediction
        return format_prediction({"round_id": row["round_id"], "output": json.loads(row["output_json"])})
    rows = store.rounds_chronological()
    if len(rows) < int(cfg["min_history"]):
        return f"البيانات غير كافية ({len(rows)}/{cfg['min_history']})."
    p = probabilities([r["cents"] for r in rows], cfg["thresholds"], float(cfg["rtp"]),
                      int(cfg["recent_window"]), float(cfg["prior_strength"]))
    lines = ["لا يوجد تقدير مُجمَّد حاليًا (يُجمَّد فقط عند بدء الرهانات).",
             "ملخص معلوماتي غير مسجل كتنبؤ:"]
    for t in cfg["thresholds"]:
        k = f"{float(t):g}"
        lines.append(f"≥{k}x: {100 * p['models']['empirical_recent'][k]:.1f}% "
                     f"(النظري {100 * p['models']['theoretical'][k]:.1f}%)")
    return "\n".join(lines)


def cmd_status(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    s = store.get_status()
    return (f"حالة الجامع: {s.get('status')}\n{s.get('detail', '')}\n"
            f"الجولات الصالحة: {store.round_count()}\n"
            f"المشتركون: {len(store.active_subscribers())}")


def cmd_verify(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    if args:
        try:
            rid = int(args[0])
        except ValueError:
            return "استخدم: /verify <round_id>"
    else:
        last = store.last_round()
        if last is None:
            return "لا توجد جولات."
        rid = int(last["round_id"])
    v = store.get_verification(rid)
    if v is None:
        return f"الجولة {rid}: لا توجد أدلة عدالة مرتبطة بها صراحةً."
    return f"الجولة {rid}: {v['status']} — {v['detail']}"


def cmd_round(store, cfg, chat_id: int, args: Sequence[str] = ()) -> str:
    last = store.last_round()
    if last is None:
        return "لا توجد جولات."
    return (f"✈️ الجولة {last['round_id']}: {last['multiplier']:.2f}x (المصدر {last['source']})\n"
            + cmd_verify(store, cfg, chat_id, [str(last["round_id"])]))


COMMANDS = {"start": cmd_start, "stop": cmd_stop, "add": cmd_add, "manual": cmd_manual,
            "clear": cmd_clear, "last": cmd_last, "stats": cmd_stats, "predict": cmd_predict,
            "status": cmd_status, "verify": cmd_verify, "round": cmd_round}
