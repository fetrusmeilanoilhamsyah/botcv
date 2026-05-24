"""
main.py - Entry point DiBot CV FEE
"""
import logging
import os
import time
import threading
from asyncio import Semaphore
from collections import defaultdict
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    ERROR_ALERT_COOLDOWN,
    JOB_CLEANUP_SESSION_INTERVAL,
    JOB_EXPIRE_VIP_INTERVAL,
    JOB_NOTIFY_EXPIRY_INTERVAL,
    JOB_CLEANUP_DISK_INTERVAL,
    SESSION_INACTIVE_TIMEOUT,
    SESSION_STUCK_TIMEOUT,
    HEALTH_PORT,       # FIX: import dari config agar konsisten
    WEBHOOK_PORT,      # FIX: import dari config agar konsisten (default 8081 ≠ 8080)
)
from database import db
from database.db_async import adb

# ── Handlers ──────────────────────────────────────────────────────────────────
from handlers.start import cmd_start, handle_back_to_start
from handlers.reset import cmd_reset, handle_reset_callback
from handlers.referral import cmd_referral
from handlers.daftar import cmd_daftar
from handlers.stat import cmd_stat
from handlers.akun import cmd_akun
from handlers.addvip import cmd_addvip, cmd_delvip
from handlers.broadcast import cmd_broadcast, handle_broadcast_msg, STATE as BROADCAST_STATE
from handlers.media_broadcast import cmd_media_broadcast, handle_broadcast_media, STATE as MEDIA_BROADCAST_STATE
from handlers.new_member import cmd_newmember, handle_newmember_id, STATE as NEWMEMBER_STATE
from handlers.del_member import cmd_delmember, handle_delmember_id, STATE as DELMEMBER_STATE
from handlers.admin_navy import cmd_admin, handle_admin_navy, STATES as AN_STATES
from handlers.merge import (
    cmd_merge, handle_merge_file, handle_merge_done, handle_merge_naming,
    STATE as MERGE_STATE, STATE_NAMING as MERGE_NAMING,
)
from handlers.vcftotxt import (
    cmd_vcftotxt, handle_vcftotxt_file, handle_vcftotxt_done, handle_vcftotxt_naming,
    STATE as VCF2TXT_STATE, STATE_NAMING as VCF2TXT_NAMING,
)
from handlers.count import (
    cmd_count, handle_count_file, handle_count_done, handle_show_count_help_callback, STATE as COUNT_STATE,
)
from handlers.xlsxtotxt import (
    cmd_xlsxtotxt, handle_xlsxtotxt_file, handle_xlsxtotxt_done, STATE as XLSX2TXT_STATE,
)
from handlers.pecahvcf import (
    cmd_pecahvcf, handle_pecah_per_file, handle_pecah_vcf_file,
    STATE_PER_FILE as PECAH_S1, STATE_WAIT_VCF as PECAH_S2,
)
from handlers.rename import (
    cmd_rename, handle_rename_name, handle_rename_file,
    STATE_NAME as RENAME_S1, STATE_FILE as RENAME_S2,
)
from handlers.duplikat import (
    cmd_duplikat, handle_duplikat_file, handle_show_duplikat_help_callback,
    STATE as DUPLICAT_STATE,
)
from handlers.txttovcf import (
    cmd_txttovcf,
    handle_ttv_contact_name, handle_ttv_per_file, handle_ttv_file_name,
    handle_ttv_awalan, handle_ttv_file, handle_ttv_done,
    S0, S1, S2, S3, S4, S5,
)

# ── VIP handler (Pakasir auto-fallback) ───────────────────────────────────────
VIP_PAKASIR_MODE = False
try:
    from handlers.vip_pakasir import (
        cmd_vip,
        handle_buy_vip,
        handle_check_payment,
        handle_cancel_payment,
        handle_vip_history,
    )
    VIP_PAKASIR_MODE = True
except ImportError as e:
    from handlers.vip import cmd_vip  # noqa: F811
    print(f"VIP Pakasir tidak tersedia, fallback ke handler manual: {e}")

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        RotatingFileHandler("logs/bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Rate limiting ─────────────────────────────────────────────────────────────
MAX_CONCURRENT_PER_USER  = 2
MAX_CONCURRENT_FILE      = 16
user_semaphores      = defaultdict(lambda: Semaphore(MAX_CONCURRENT_PER_USER))
user_file_semaphores = defaultdict(lambda: Semaphore(MAX_CONCURRENT_FILE))

_error_last_sent: dict = {}
_job_running = {"expire": False, "cleanup": False, "notify": False}
_start_time = time.time()


def rate_limiter(func):
    async def wrapper(update: Update, context):
        async with user_semaphores[update.effective_user.id]:
            return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def file_rate_limiter(func):
    async def wrapper(update: Update, context):
        async with user_file_semaphores[update.effective_user.id]:
            return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Error handler ─────────────────────────────────────────────────────────────
async def error_handler(update: object, context):
    from telegram.error import NetworkError, TimedOut, Conflict
    if isinstance(context.error, (NetworkError, TimedOut, Conflict)):
        logger.warning("Network error (auto-retry): %s", context.error)
        return

    logger.error("Exception:", exc_info=context.error)

    now = time.time()
    err_type = type(context.error).__name__
    if now - _error_last_sent.get(err_type, 0) < ERROR_ALERT_COOLDOWN:
        return
    _error_last_sent[err_type] = now

    import traceback
    from config import ADMIN_IDS
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    short_tb = tb[-1500:] if len(tb) > 1500 else tb
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"<b>ERROR BOT</b>\n<code>{err_type}</code>\n<pre>{short_tb}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    if isinstance(update, Update) and update.message:
        await update.message.reply_text("Terjadi kesalahan. Ketik /reset untuk mereset sesi.")


# ── Text & file routers ───────────────────────────────────────────────────────
async def text_router(update: Update, context):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess:
        return

    state = sess.get("state")
    text = (update.message.text or "").strip().lower()

    # "selesai" / "done" sebagai teks biasa (bukan /done command)
    if text in ("selesai", "done") and state not in (BROADCAST_STATE, MEDIA_BROADCAST_STATE):
        await done_router(update, context)
        return

    route = {
        **{v: handle_admin_navy for v in AN_STATES.values()},
        MERGE_NAMING:     handle_merge_naming,
        VCF2TXT_NAMING:   handle_vcftotxt_naming,
        PECAH_S1:         handle_pecah_per_file,
        RENAME_S1:        handle_rename_name,
        S1:               handle_ttv_contact_name,
        S2:               handle_ttv_per_file,
        S3:               handle_ttv_file_name,
        S4:               handle_ttv_awalan,
        BROADCAST_STATE:  handle_broadcast_msg,
        NEWMEMBER_STATE:  handle_newmember_id,
        DELMEMBER_STATE:  handle_delmember_id,
        MEDIA_BROADCAST_STATE: handle_broadcast_media,
    }
    handler = route.get(state)
    if handler:
        await handler(update, context)


async def file_router(update: Update, context):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess:
        return

    state = sess.get("state")
    is_doc = bool(update.message.document)

    doc_states = {
        MERGE_STATE:    (handle_merge_file,       "file VCF atau TXT berupa DOKUMEN"),
        VCF2TXT_STATE:  (handle_vcftotxt_file,    "file VCF berupa DOKUMEN"),
        PECAH_S2:       (handle_pecah_vcf_file,   "file VCF berupa DOKUMEN"),
        RENAME_S2:      (handle_rename_file,      "file VCF berupa DOKUMEN"),
        S0:             (handle_ttv_file,         "file TXT berupa DOKUMEN"),
        S5:             (handle_ttv_file,         "file TXT berupa DOKUMEN"),
        COUNT_STATE:    (handle_count_file,       "file VCF berupa DOKUMEN"),
        XLSX2TXT_STATE: (handle_xlsxtotxt_file,   "file XLSX/CSV berupa DOKUMEN"),
        DUPLICAT_STATE: (handle_duplikat_file,    "file VCF atau TXT berupa DOKUMEN"),
    }

    if state in doc_states:
        handler, hint = doc_states[state]
        if not is_doc:
            await update.message.reply_text(f"Kirim {hint}.")
            return
        await handler(update, context)
    elif state == MEDIA_BROADCAST_STATE:
        await handle_broadcast_media(update, context)


async def done_router(update: Update, context):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or not sess.get("state"):
        await update.message.reply_text("Tidak ada proses aktif.")
        return

    state = sess.get("state")
    route = {
        MERGE_STATE:    handle_merge_done,
        VCF2TXT_STATE:  handle_vcftotxt_done,
        S0:             handle_ttv_done,
        S5:             handle_ttv_done,
        COUNT_STATE:    handle_count_done,
        XLSX2TXT_STATE: handle_xlsxtotxt_done,
    }
    handler = route.get(state)
    if handler:
        await handler(update, context)
    else:
        await update.message.reply_text("Tidak ada proses aktif yang bisa diselesaikan.")


# ── show_vip_menu callback ─────────────────────────────────────────────────────
async def cb_show_vip_menu(update: Update, context):
    """
    Callback dari tombol 'Lihat Paket VIP' di require_member.
    Jawab query lalu kirim pesan VIP langsung — tidak pakai fake Update object.
    """
    query = update.callback_query
    await query.answer()

    # Hapus pesan peringatan VIP/redirect agar layar tidak menumpuk
    try:
        await query.message.delete()
    except Exception:
        pass

    user    = query.from_user
    chat_id = query.message.chat_id

    # Bangun konten VIP menu secara langsung (tanpa import cmd_vip logic ganda)
    from handlers.vip_pakasir import cmd_vip as _cmd_vip, QRIS_ENABLED, PAKET, _fmt_price, PAKASIR_SANDBOX
    from database.db_async import adb as _adb
    from datetime import datetime

    status_line = ""
    if await _adb.is_member(user.id):
        expired_at = await _adb.get_vip_expiry(user.id)
        if expired_at:
            exp  = datetime.fromisoformat(expired_at)
            sisa = (exp - datetime.now()).days
            status_line = (
                f"Status VIP    : Aktif\n"
                f"Berakhir      : {exp.strftime('%d/%m/%Y')} ({sisa} hari lagi)\n\n"
            )
        else:
            status_line = "Status        : Member Permanen\n\n"

    paket_lines = "PAKET VIP\n" + ("-" * 28) + "\n"
    for p in PAKET:
        paket_lines += f"  {p['label']:<12}  {_fmt_price(p['price'])}\n"

    if QRIS_ENABLED:
        mode = "SANDBOX" if PAKASIR_SANDBOX else "QRIS Otomatis"
        info = f"\nPembayaran: {mode}\nPilih paket, bayar QRIS, VIP aktif otomatis."
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        rows = []
        for p in PAKET:
            rows.append([
                InlineKeyboardButton(
                    f"{p['label'].upper()} — {_fmt_price(p['price'])}",
                    callback_data=f"buy_vip_{p['days']}",
                    style="primary"
                )
            ])
        rows.append([InlineKeyboardButton("RIWAYAT PEMBAYARAN", callback_data="vip_history", style="success")])
    else:
        from config import ADMIN_CONTACT as _AC
        info = f"\nPembayaran: Manual\nHubungi {_AC} untuk aktivasi."
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        rows = [[InlineKeyboardButton(
            "HUBUNGI ADMIN",
            url=f"https://t.me/{_AC.lstrip('@')}"
        )]]

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{status_line}{paket_lines}{info}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def _job_expire_vip(context):
    if _job_running["expire"]:
        return
    _job_running["expire"] = True
    try:
        count = await adb.expire_vip_members()
        if count:
            logger.info("[JOB] %d VIP expired", count)
    finally:
        _job_running["expire"] = False


async def _job_cleanup_sessions(context):
    if _job_running["cleanup"]:
        return
    _job_running["cleanup"] = True
    try:
        from middleware.session import clear_user_dir
        tmp_base = os.path.join("tmp", "sessions")
        if not os.path.exists(tmp_base):
            return

        now = time.time()
        cleaned = 0
        for uid_str in os.listdir(tmp_base):
            if not uid_str.isdigit():
                continue
            path = os.path.join(tmp_base, uid_str)
            try:
                if os.path.isdir(path) and (now - os.path.getmtime(path)) > SESSION_STUCK_TIMEOUT:
                    sess = db.get_session(int(uid_str))
                    if not sess or not sess.get("state"):
                        clear_user_dir(int(uid_str))
                        cleaned += 1
            except Exception:
                pass
        if cleaned:
            logger.info("[JOB] Cleaned %d stuck session dirs", cleaned)

        # Cleanup semaphores untuk user lama tidak aktif
        inactive = []
        for uid in list(user_semaphores.keys()):
            user = db.get_user(uid)
            if user:
                try:
                    from datetime import datetime
                    last = datetime.fromisoformat(dict(user)["last_active"])
                    if (datetime.now() - last).total_seconds() > SESSION_INACTIVE_TIMEOUT:
                        inactive.append(uid)
                except Exception:
                    pass
        for uid in inactive:
            user_semaphores.pop(uid, None)
            user_file_semaphores.pop(uid, None)
        if inactive:
            logger.info("[JOB] Cleaned %d inactive semaphores", len(inactive))

        cleaned_cache = await adb.cleanup_stale_sessions()
        if cleaned_cache:
            logger.info("[JOB] Cleaned %d stale session cache", cleaned_cache)
    finally:
        _job_running["cleanup"] = False


async def _job_notify_expiry(context):
    if _job_running["notify"]:
        return
    _job_running["notify"] = True
    try:
        for u in await adb.get_users_for_expiry_notif():
            uid = u["id"]
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text="Masa aktif VIP kamu tinggal 24 jam lagi. Perpanjang agar fitur tetap aktif.",
                )
                await adb.mark_expiry_notified(uid)
            except Exception:
                pass
    finally:
        _job_running["notify"] = False


async def job_expire_pending_payments(context):
    """Expire QRIS payment yang sudah > 5 menit, hapus pesan QR, dan beritahu user."""
    try:
        expired_payments = await adb.get_and_expire_old_pending_payments(minutes=5)
        
        if expired_payments:
            logger.info("[Job] expire_pending_payments: memproses %d transaksi kedaluwarsa", len(expired_payments))
            for p in expired_payments:
                # 1. Hapus pesan QR di Telegram
                qr_chat_id = p.get("qr_chat_id")
                qr_message_id = p.get("qr_message_id")
                if qr_chat_id and qr_message_id:
                    try:
                        await context.bot.delete_message(chat_id=qr_chat_id, message_id=qr_message_id)
                        logger.info("[Job] Berhasil menghapus pesan QR untuk order %s", p["order_id"])
                    except Exception as e:
                        logger.debug("[Job] Gagal menghapus pesan QR untuk order %s: %s", p["order_id"], e)
                
                # 2. Kirim notifikasi kedaluwarsa ke user
                try:
                    text_msg = (
                        f"⏰ <b>Pembayaran Kedaluwarsa!</b>\n\n"
                        f"Batas waktu pembayaran QRIS selama 5 menit telah habis.\n"
                        f"Order ID: <code>{p['order_id']}</code>\n\n"
                        f"Jika Anda masih ingin membeli VIP, silakan gunakan perintah /vip kembali."
                    )
                    await context.bot.send_message(
                        chat_id=p["user_id"],
                        text=text_msg,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.debug("[Job] Gagal mengirim notifikasi kedaluwarsa ke user %s: %s", p["user_id"], e)
    except Exception as exc:
        logger.error("[Job] expire_pending_payments error: %s", exc)


async def job_cleanup_disk(context):
    """Hapus session files user yang sudah tidak aktif > 24 jam."""
    try:
        from middleware.session import cleanup_old_sessions
        count = cleanup_old_sessions(max_age_hours=24)
        if count > 0:
            logger.info("[Job] cleanup_disk: removed %d stale session dirs", count)
    except Exception as exc:
        logger.error("[Job] cleanup_disk error: %s", exc)


# ── Health check ──────────────────────────────────────────────────────────────
def _run_health_server():
    from aiohttp import web

    async def health(request):
        try:
            from database import db as _db
            stats = _db.get_db_stats()
            return web.json_response({
                "status": "ok",
                "uptime": int(time.time() - _start_time),
                **stats,
            })
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=500)

    app = web.Application()
    app.router.add_get("/health", health)
    # FIX: gunakan HEALTH_PORT dari config (bukan os.getenv ulang)
    web.run_app(app, host="0.0.0.0", port=HEALTH_PORT, print=None, handle_signals=False)


# ── main ────────────────────────────────────────────────────────────────────────
def main():
    # Health check server (background)
    threading.Thread(target=_run_health_server, daemon=True, name="health-server").start()
    # FIX: gunakan HEALTH_PORT dari config (konsisten)
    logger.info("Health check server started on port %s", HEALTH_PORT)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(32)
        .connection_pool_size(100)
        .pool_timeout(30)
        .read_timeout(30)
        .write_timeout(120)
        .connect_timeout(30)
        .build()
    )

    # ── Command handlers ──
    app.add_handler(CommandHandler("start",                              rate_limiter(cmd_start)))
    app.add_handler(CommandHandler(["reset", "resetdatabase"],          rate_limiter(cmd_reset)))
    app.add_handler(CommandHandler(["admin", "Admin"],                  rate_limiter(cmd_admin)))
    app.add_handler(CommandHandler("txttovcf",                          rate_limiter(cmd_txttovcf)))
    app.add_handler(CommandHandler("xlsxtotxt",                         rate_limiter(cmd_xlsxtotxt)))
    app.add_handler(CommandHandler("merge",                             rate_limiter(cmd_merge)))
    app.add_handler(CommandHandler("vcftotxt",                          rate_limiter(cmd_vcftotxt)))
    app.add_handler(CommandHandler("pecahvcf",                          rate_limiter(cmd_pecahvcf)))
    app.add_handler(CommandHandler("rename",                            rate_limiter(cmd_rename)))
    app.add_handler(CommandHandler("count",                             rate_limiter(cmd_count)))
    app.add_handler(CommandHandler("duplikat",                          rate_limiter(cmd_duplikat)))
    app.add_handler(CommandHandler(["broadcast", "brodcast", "Brodcast"], rate_limiter(cmd_broadcast)))
    app.add_handler(CommandHandler("mediabroadcast",                    rate_limiter(cmd_media_broadcast)))
    app.add_handler(CommandHandler("newmember",                         rate_limiter(cmd_newmember)))
    app.add_handler(CommandHandler(["delmember", "copotmember"],        rate_limiter(cmd_delmember)))
    app.add_handler(CommandHandler(["referal", "referral"],             rate_limiter(cmd_referral)))
    app.add_handler(CommandHandler("daftar",                            rate_limiter(cmd_daftar)))
    app.add_handler(CommandHandler("vip",                               rate_limiter(cmd_vip)))
    app.add_handler(CommandHandler("addvip",                            rate_limiter(cmd_addvip)))
    app.add_handler(CommandHandler("delvip",                            rate_limiter(cmd_delvip)))
    app.add_handler(CommandHandler("stat",                              rate_limiter(cmd_stat)))
    app.add_handler(CommandHandler("akun",                             rate_limiter(cmd_akun)))
    app.add_handler(CommandHandler("done",                              rate_limiter(done_router)))

    # ── Callback handlers ──
    app.add_handler(CallbackQueryHandler(cb_show_vip_menu,       pattern="^show_vip_menu$"))
    app.add_handler(CallbackQueryHandler(handle_back_to_start,   pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(handle_show_duplikat_help_callback, pattern="^show_duplikat_help$"))
    app.add_handler(CallbackQueryHandler(handle_show_count_help_callback, pattern="^show_count_help$"))
    app.add_handler(CallbackQueryHandler(handle_reset_callback,  pattern="^admin_db_reset"))

    if VIP_PAKASIR_MODE:
        app.add_handler(CallbackQueryHandler(handle_buy_vip,         pattern="^buy_vip_"))
        app.add_handler(CallbackQueryHandler(handle_check_payment,   pattern="^check_payment_"))
        app.add_handler(CallbackQueryHandler(handle_cancel_payment,  pattern="^cancel_payment_"))
        app.add_handler(CallbackQueryHandler(handle_vip_history,     pattern="^vip_history$"))

    # ── Message handlers ──
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.ANIMATION,
        file_rate_limiter(file_router),
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rate_limiter(text_router)))

    # ── Error handler ──
    app.add_error_handler(error_handler)

    # ── Startup: expire VIP ──
    from database import db as _db
    expired = _db.expire_vip_members()
    if expired:
        logger.info("Startup: %d VIP expired", expired)

    # ── Pakasir webhook server ──
    if os.getenv("PAKASIR_ENABLED", "false").lower() == "true" and VIP_PAKASIR_MODE:
        # Validasi: pastikan WEBHOOK_PORT != HEALTH_PORT agar tidak bentrok
        if WEBHOOK_PORT == HEALTH_PORT:
            logger.error(
                "WEBHOOK_PORT (%s) == HEALTH_PORT (%s)! Ganti salah satunya di .env. "
                "Webhook server TIDAK dijalankan untuk mencegah crash.",
                WEBHOOK_PORT, HEALTH_PORT
            )
        else:
            try:
                from webhook_pakasir import start_webhook_server_thread
                # FIX: gunakan WEBHOOK_PORT dari config (bukan os.getenv ulang dengan default berbeda)
                start_webhook_server_thread(WEBHOOK_PORT, app.bot)
                logger.info("Pakasir webhook server started on port %s", WEBHOOK_PORT)
            except Exception as e:
                logger.error("Gagal start webhook server: %s", e)

    # ── Scheduled jobs ──
    app.job_queue.run_repeating(_job_expire_vip,               interval=JOB_EXPIRE_VIP_INTERVAL,       first=60)
    app.job_queue.run_repeating(_job_cleanup_sessions,         interval=JOB_CLEANUP_SESSION_INTERVAL,  first=120)
    app.job_queue.run_repeating(_job_notify_expiry,            interval=JOB_NOTIFY_EXPIRY_INTERVAL,    first=300)
    app.job_queue.run_repeating(job_expire_pending_payments,   interval=60,                            first=60)
    app.job_queue.run_repeating(job_cleanup_disk,              interval=JOB_CLEANUP_DISK_INTERVAL,     first=120)

    logger.info("Bot berjalan...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()