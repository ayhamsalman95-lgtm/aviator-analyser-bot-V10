import asyncio
import json
import subprocess
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import BadRequest, Forbidden

from fairness import verify_round
from predictor import combined_predict
from store import (
    BASE,
    HISTORY,
    load_history,
    load_live,
    load_rounds,
    last_round,
    save_history,
)

CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SUBSCRIBERS = BASE / "subscribers.json"
STATUS = BASE / "collector_status.json"
MIN_HISTORY = int(CONFIG.get("min_history", 30))
WINDOW = int(CONFIG.get("window", 10))
MAX_HISTORY = int(CONFIG.get("max_history", 10000))
COLLECTOR_PROC = None
MONITOR_TASK = None
TELEGRAM_ERROR_LOG = BASE / "telegram_errors.log"


def load_subscribers():
    try:
        raw = json.loads(SUBSCRIBERS.read_text(encoding="utf-8"))
        return {int(x) for x in raw}
    except Exception:
        return set()


def save_subscribers(values):
    SUBSCRIBERS.write_text(json.dumps(sorted(values)), encoding="utf-8")


def read_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "not_started", "detail": "جامع النتائج لم يبدأ بعد."}


def _short(value, n=14):
    if not value:
        return "—"
    s = str(value)
    return s if len(s) <= n + 1 else s[:n] + "…"


def seeds_block(record: dict | None, live: dict | None = None) -> str:
    src = record or {}
    live = live or {}
    seed_hash = src.get("seed_hash") or live.get("seed_hash")
    server = src.get("server_seed") or live.get("server_seed")
    clients = src.get("client_seeds") or live.get("client_seeds") or []
    nonce = src.get("nonce") or live.get("nonce")
    combined = src.get("combined_hash") or live.get("combined_hash")
    sources = src.get("seed_sources") or live.get("seed_sources") or {}
    lines = ["🔐 بيانات العدل (Provably Fair)", ""]
    lines.append(f"Seed Hash: `{_short(seed_hash, 18)}`" if seed_hash else "Seed Hash: لم تُلتقط")
    if clients:
        for i, c in enumerate(clients[:3], 1):
            lines.append(f"Client Seed {i}: `{_short(c, 18)}`")
    else:
        lines.append("Client Seeds: لم تُلتقط")
    if server:
        lines.append(f"Server Seed: `{_short(server, 18)}`")
    else:
        lines.append("Server Seed: لم تُلتقط/لم تُكشف في المصدر المراقب")
    if nonce:
        lines.append(f"Nonce: `{nonce}`")
    if combined:
        lines.append(f"Combined Hash: `{_short(combined, 18)}`")
    if sources:
        src_names = []
        for key, info in sources.items():
            if isinstance(info, dict) and info.get("source"):
                src_names.append(f"{key}←{info['source']}")
        if src_names:
            lines.append("المصدر: " + ", ".join(src_names[:5]))
    return "\n".join(lines)


def verify_block(record: dict | None) -> str:
    if not record:
        return "التحقق: لا توجد جولة مكتملة."
    result = verify_round(
        record.get("multiplier"),
        server_seed=record.get("server_seed") or "",
        client_seeds=record.get("client_seeds") or [],
        seed_hash=record.get("seed_hash") or "",
        nonce=record.get("nonce"),
    )
    mark = "✅ مطابق" if result.ok else "⏳ غير مكتمل" if not record.get("server_seed") else "❌ غير مطابق"
    extra = f"\nالمخطط: {result.scheme}" if result.scheme and result.scheme != "none" else ""
    expected = (
        f"\nالمضاعف المحسوب: {result.expected_multiplier:.2f}x"
        if result.expected_multiplier is not None
        else ""
    )
    return f"التحقق: {mark}\n{result.detail}{extra}{expected}"


def prediction_text(history):
    if len(history) < MIN_HISTORY:
        return (
            "✈️ AVIATOR ANALYZER\n\n"
            f"البيانات غير كافية لإصدار تقدير.\n"
            f"المتوفر: {len(history)} / {MIN_HISTORY}\n\n"
            "عند تشغيل الجامع تُضاف الجولات الجديدة من صفحة اللعبة."
        )
    p = combined_predict(history, WINDOW)
    return (
        "✈️ AVIATOR ANALYZER\n\n"
        f"التقدير الإحصائي للجولة التالية: ≈ {p.combined:.2f}x\n"
        f"Heuristic: {p.heuristic:.2f}x\n"
        f"Sequence/LSTM: {p.sequence:.2f}x\n"
        f"اتفاق النموذجين: {p.agreement:.0f}%\n"
        f"آخر نتيجة مسجلة: {history[-1]:.2f}x\n\n"
        "⚠️ تقدير إحصائي فقط. Seed Hash لا يُفك قبل كشف بذرة الخادم، ولا توجد نتيجة مؤكدة للجولة التالية."
    )


def log_telegram_error(chat_id, exc, context="send_message"):
    try:
        with TELEGRAM_ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                "chat_id": chat_id,
                "context": context,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def broadcast(app: Application, text: str):
    subscribers = load_subscribers()
    if not subscribers:
        print("[TELEGRAM] No subscribers. Open the bot in Telegram and send /start once.", flush=True)
        return

    delivered = 0
    for chat_id in sorted(subscribers):
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            delivered += 1
            print(f"[TELEGRAM] sent -> {chat_id}", flush=True)
            continue
        except BadRequest as exc:
            # Markdown formatting can fail; retry as plain text before giving up.
            log_telegram_error(chat_id, exc, "markdown")
            try:
                await app.bot.send_message(chat_id=chat_id, text=text)
                delivered += 1
                print(f"[TELEGRAM] sent plain -> {chat_id}", flush=True)
                continue
            except Exception as exc2:
                log_telegram_error(chat_id, exc2, "plain-after-markdown")
                print(f"[TELEGRAM] send failed -> {chat_id}: {exc2}", flush=True)
        except Forbidden as exc:
            # The user blocked the bot; remove only this definite dead subscriber.
            log_telegram_error(chat_id, exc, "forbidden")
            print(f"[TELEGRAM] blocked/dead subscriber -> {chat_id}", flush=True)
            subs = load_subscribers()
            subs.discard(chat_id)
            save_subscribers(subs)
        except Exception as exc:
            # Network/API errors must NOT silently delete a valid subscriber.
            log_telegram_error(chat_id, exc, "send")
            print(f"[TELEGRAM] send error -> {chat_id}: {exc}", flush=True)

    if delivered == 0 and subscribers:
        print("[TELEGRAM] No message was delivered. Check telegram_errors.log.", flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        subs = load_subscribers()
        subs.add(update.effective_chat.id)
        save_subscribers(subs)
    live = load_live()
    rec = last_round()
    await update.message.reply_text(
        "أهلًا. تم تسجيلك لتنبيهات ما قبل الجولة وبعدها.\n\n"
        + prediction_text(load_history())
        + "\n\n"
        + seeds_block(rec, live)
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم:\n/add 1.12 2.31 1.04 4.19")
        return
    values = []
    for token in context.args:
        try:
            value = float(token.replace(",", ".").lower().replace("x", ""))
            if value >= 1.0:
                values.append(value)
        except ValueError:
            continue
    if not values:
        await update.message.reply_text("لم أجد أرقامًا صالحة.")
        return
    history = load_history()
    history.extend(values)
    save_history(history[-MAX_HISTORY:])
    await update.message.reply_text(
        f"أضفت {len(values)} نتيجة. الإجمالي: {len(history)}\n\n" + prediction_text(history)
    )


async def telegram_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        subs = load_subscribers()
        subs.add(chat_id)
        save_subscribers(subs)
    await update.message.reply_text(
        "✅ اتصال Telegram يعمل.\n"
        "تم تسجيل هذه المحادثة للتنبيهات التلقائية.\n\n"
        "سيظهر في نافذة البوت أيضًا: [TELEGRAM] sent -> <chat_id> عند إرسال أول تنبيه.\n"
        "استخدم /status للتأكد من حالة الجامع."
    )


async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(prediction_text(load_history()))


async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = load_history()
    if not history:
        await update.message.reply_text("لا توجد نتائج محفوظة.")
        return
    tail = " | ".join(f"{v:.2f}x" for v in history[-20:])
    rec = last_round()
    extra = ""
    if rec:
        extra = f"\n\nآخر جولة كاملة: {rec.get('id')} → {rec.get('multiplier')}x\n" + seeds_block(rec)
    await update.message.reply_text("آخر 20 نتيجة:\n" + tail + extra)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = load_history()
    if not history:
        await update.message.reply_text("لا توجد بيانات.")
        return
    values = sorted(history)
    med = values[len(values) // 2]
    below2 = 100.0 * sum(v < 2.0 for v in history) / len(history)
    below3 = 100.0 * sum(v < 3.0 for v in history) / len(history)
    rounds = load_rounds()
    with_hash = sum(1 for r in rounds if r.get("seed_hash"))
    with_server = sum(1 for r in rounds if r.get("server_seed"))
    with_clients = sum(1 for r in rounds if r.get("client_seeds"))
    await update.message.reply_text(
        "📊 الإحصائيات\n\n"
        f"عدد النتائج: {len(history)}\n"
        f"الوسيط التقريبي: {med:.2f}x\n"
        f"أقل من 2x: {below2:.1f}%\n"
        f"أقل من 3x: {below3:.1f}%\n\n"
        f"جولات فيها Seed Hash: {with_hash}\n"
        f"جولات فيها Server Seed: {with_server}\n"
        f"جولات فيها Client Seeds: {with_clients}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = read_status()
    alive = COLLECTOR_PROC is not None and COLLECTOR_PROC.poll() is None
    live = load_live()
    await update.message.reply_text(
        ("🟢 الجامع يعمل" if alive else "🟠 الجامع متوقف")
        + "\n\n"
        + f"الحالة: {s.get('status', 'unknown')}\n"
        + f"التفاصيل: {s.get('detail', '')}\n"
        + f"مرحلة الجولة: {live.get('phase', 'waiting')}\n"
        + f"البيانات: {len(load_history())}\n"
        + f"التقاط بذور: {live.get('seed_hits', 0)}\n"
        + f"مشتركون Telegram: {len(load_subscribers())}"
    )


async def seeds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = last_round()
    live = load_live()
    await update.message.reply_text(seeds_block(rec, live) + "\n\n" + verify_block(rec))


async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = last_round()
    if not rec:
        await update.message.reply_text("لا توجد جولة للتحقق.")
        return
    await update.message.reply_text(
        f"الجولة {rec.get('id')} — {rec.get('multiplier')}x\n\n"
        + seeds_block(rec)
        + "\n\n"
        + verify_block(rec)
    )


async def round_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = last_round()
    if not rec:
        await update.message.reply_text("لا توجد جولة محفوظة.")
        return
    await update.message.reply_text(
        f"✈️ الجولة {rec.get('id')}\n"
        f"النتيجة: {rec.get('multiplier')}x\n"
        f"المصدر: {rec.get('source')}\n\n"
        + seeds_block(rec)
        + "\n\n"
        + verify_block(rec)
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        subs = load_subscribers()
        subs.discard(update.effective_chat.id)
        save_subscribers(subs)
    await update.message.reply_text("تم إيقاف التنبيهات التلقائية لهذا الحساب. يمكنك إعادة تفعيلها بـ /start")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    HISTORY.write_text("[]", encoding="utf-8")
    (BASE / "rounds.json").write_text("[]", encoding="utf-8")
    await update.message.reply_text("تم حذف سجل النتائج المحلي. سيُعاد جمع الجولات الجديدة تلقائيًا.")


def seed_fingerprint(rec: dict | None) -> str:
    if not rec:
        return ""
    payload = {
        "id": rec.get("id"),
        "seed_hash": rec.get("seed_hash"),
        "server_seed": rec.get("server_seed"),
        "client_seeds": rec.get("client_seeds") or [],
        "combined_hash": rec.get("combined_hash"),
        "nonce": rec.get("nonce"),
        "verified": rec.get("verified"),
        "verify_scheme": rec.get("verify_scheme"),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


async def monitor_history(app: Application):
    last_len = len(load_rounds())
    last_predict = None
    sent_seed_fingerprints = {}
    while True:
        await asyncio.sleep(1.2)
        live = load_live()
        phase = live.get("phase")
        token = live.get("round_id") or live.get("predict_token")
        if phase == "betting" and token and token != last_predict:
            last_predict = token
            history = load_history()
            seed_hash = live.get("seed_hash")
            text = (
                "🟡 جولة جديدة — ما قبل الإقلاع\n\n"
                f"رقم الجولة: {live.get('round_id') or '—'}\n"
            )
            if seed_hash:
                text += f"Seed Hash: `{_short(seed_hash, 20)}`\n"
                text += "Server Seed ما زالت غير مكشوفة في هذه اللحظة.\n\n"
            else:
                text += "Seed Hash: لم تُلتقط حتى الآن.\n"
                text += "الجامع مستمر في مراقبة HTTP/WebSocket/DOM.\n\n"
            clients = live.get("client_seeds") or []
            if clients:
                text += "Client Seeds: " + ", ".join(f"`{_short(c, 10)}`" for c in clients[:3]) + "\n\n"
            text += "🔮 تقدير الجولة التالية:\n\n" + prediction_text(history)
            await broadcast(app, text)

        rounds = load_rounds()
        if len(rounds) > last_len:
            new = rounds[last_len:]
            last_len = len(rounds)
            rec = new[-1]
            history = load_history()
            if len(history) >= MIN_HISTORY:
                text = (
                    "🆕 جولة انتهت\n\n"
                    f"النتيجة: {rec.get('multiplier')}x\n"
                    f"رقم الجولة: {rec.get('id')}\n\n"
                    + seeds_block(rec, live)
                    + "\n\n"
                    + verify_block(rec)
                    + "\n\n🔮 تحليل الجولة التالية:\n\n"
                    + prediction_text(history)
                )
                await broadcast(app, text)
                sent_seed_fingerprints[str(rec.get("id"))] = seed_fingerprint(rec)
            continue

        # A server seed may arrive seconds after OnCrash. The collector now
        # enriches the same saved round; notify Telegram once when that happens.
        if rounds:
            rec = rounds[-1]
            rid = str(rec.get("id") or "")
            fp = seed_fingerprint(rec)
            previous = sent_seed_fingerprints.get(rid)
            if rid and fp and previous and fp != previous and any([
                rec.get("seed_hash"), rec.get("server_seed"), rec.get("client_seeds"),
                rec.get("nonce"), rec.get("combined_hash"),
            ]):
                await broadcast(
                    app,
                    "🔐 تحديث بيانات العدل للجولة " + rid + "\n\n"
                    + seeds_block(rec, live)
                    + "\n\n"
                    + verify_block(rec)
                )
                sent_seed_fingerprints[rid] = fp


async def post_init(app: Application):
    global MONITOR_TASK
    MONITOR_TASK = asyncio.create_task(monitor_history(app), name="history-monitor")
    print("[TELEGRAM] monitor task started", flush=True)


async def post_shutdown(app: Application):
    global MONITOR_TASK
    if MONITOR_TASK is not None and not MONITOR_TASK.done():
        MONITOR_TASK.cancel()
        try:
            await MONITOR_TASK
        except asyncio.CancelledError:
            pass
    MONITOR_TASK = None


def start_collector():
    global COLLECTOR_PROC
    if not CONFIG.get("auto_collect", True):
        return
    collector = BASE / "collector_fixed_v11.py"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0
    COLLECTOR_PROC = subprocess.Popen(
        [sys.executable, str(collector)],
        cwd=str(BASE),
        creationflags=flags,
    )


def main():
    global COLLECTOR_PROC
    token = CONFIG.get("bot_token", "").strip()
    if not token or token == "PUT_NEW_BOT_TOKEN_HERE":
        raise SystemExit("ضع Bot Token الجديد في config.json ثم شغّل bot.py مرة أخرى.")

    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("predict", predict))
    app.add_handler(CommandHandler("test", telegram_test))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("seeds", seeds_cmd))
    app.add_handler(CommandHandler("verify", verify_cmd))
    app.add_handler(CommandHandler("round", round_cmd))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("clear", clear))

    print("Aviator Telegram Analyzer is running...", flush=True)
    print(f"[TELEGRAM] subscribers={sorted(load_subscribers())}", flush=True)
    start_collector()
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        if COLLECTOR_PROC is not None and COLLECTOR_PROC.poll() is None:
            COLLECTOR_PROC.terminate()




# AVIATOR_V10_TELEGRAM_DEDUPE
# Prevent duplicate Telegram delivery while allowing different messages
# (e.g. pre-round notice vs completed-round result) for the same round.
def _install_telegram_dedupe():
    import hashlib as _hashlib
    import re as _re
    from telegram.ext import ExtBot as _ExtBot
    from store import telegram_alert_already_sent, mark_telegram_alert_sent

    if getattr(_ExtBot.send_message, "__aviator_v10_wrapped__", False):
        return

    _orig_send_message = _ExtBot.send_message
    _round_re = _re.compile(r"(?:رقم\s*الجولة|round\s*(?:id|number)?)\s*[:：=]?\s*(\d+)", _re.I)

    async def _deduped_send_message(self, chat_id, text, *args, **kwargs):
        body = str(text or "")
        match = _round_re.search(body)
        key = None
        if match:
            round_id = match.group(1)
            if "جولة انتهت" in body or "round ended" in body.lower():
                kind = "result"
            elif "ما قبل الإقلاع" in body or "جولة جديدة" in body or "before takeoff" in body.lower():
                kind = "preflight"
            elif "بيانات العدل" in body or "provably fair" in body.lower():
                kind = "fairness"
            else:
                kind = "round"
            digest = _hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest()[:20]
            key = f"{chat_id}|{kind}|{round_id}|{digest}"
            if telegram_alert_already_sent(key):
                print(f"[TELEGRAM] duplicate suppressed round={round_id} kind={kind}", flush=True)
                return None

        result = await _orig_send_message(self, chat_id, text, *args, **kwargs)
        if key:
            mark_telegram_alert_sent(key)
        return result

    _deduped_send_message.__aviator_v10_wrapped__ = True
    _ExtBot.send_message = _deduped_send_message


if __name__ == "__main__":
    _install_telegram_dedupe()
    main()
