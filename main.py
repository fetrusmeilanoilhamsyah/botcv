"""
main.py - Entry point DiBot CV FEE
"""
import logging
import asyncio
import os
import time
import threading
from asyncio import Semaphore
from collections import defaultdict
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    Defaults,
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
    LOCAL_BOT_API_PORT,
)
from database import db
from database.db_async import adb

# ── Handlers ──────────────────────────────────────────────────────────────────
from handlers.start import cmd_start, handle_back_to_start
from handlers.reset import cmd_reset, handle_reset_callback
from handlers.referral import cmd_referral, handle_redeem_points
from handlers.daftar import cmd_daftar
from handlers.stat import cmd_stat
from handlers.akun import cmd_akun
from handlers.addvip import cmd_addvip, cmd_delvip
from handlers.broadcast import cmd_broadcast, handle_broadcast_msg, cmd_stop_broadcast, STATE as BROADCAST_STATE
from handlers.media_broadcast import cmd_media_broadcast, handle_broadcast_media, STATE as MEDIA_BROADCAST_STATE
from handlers.new_member import cmd_newmember, handle_newmember_id, STATE as NEWMEMBER_STATE
from handlers.del_member import cmd_delmember, handle_delmember_id, STATE as DELMEMBER_STATE
from handlers.admin_navy import cmd_admin, handle_admin_navy, handle_show_admin_help_callback, STATES as AN_STATES
from handlers.merge import (
    cmd_merge, handle_merge_file, handle_merge_done, handle_merge_naming,
    handle_show_merge_help_callback,
    STATE as MERGE_STATE, STATE_NAMING as MERGE_NAMING,
)
from handlers.vcftotxt import (
    cmd_vcftotxt, handle_vcftotxt_file, handle_vcftotxt_done, handle_vcftotxt_naming,
    handle_show_vcftotxt_help_callback,
    STATE as VCF2TXT_STATE, STATE_NAMING as VCF2TXT_NAMING,
)
from handlers.count import (
    cmd_count, handle_count_file, handle_count_done, handle_show_count_help_callback, STATE as COUNT_STATE,
)
from handlers.xlsxtotxt import (
    cmd_xlsxtotxt, handle_xlsxtotxt_file, handle_xlsxtotxt_done,
    handle_show_xlsxtotxt_help_callback,
    STATE as XLSX2TXT_STATE,
)
from handlers.pecahvcf import (
    cmd_pecahvcf, handle_pecah_per_file, handle_pecah_vcf_file,
    handle_show_pecahvcf_help_callback,
    STATE_PER_FILE as PECAH_S1, STATE_WAIT_VCF as PECAH_S2,
)
from handlers.pecahtxt import (
    cmd_pecahtxt, handle_pecahtxt_per_file, handle_pecahtxt_file,
    handle_pecahtxt_done, handle_show_pecahtxt_help_callback,
    STATE_PER_FILE as PECAHTXT_S1, STATE_COLLECTING as PECAHTXT_S2,
)
from handlers.rename import (
    cmd_rename, handle_rename_name, handle_rename_file,
    handle_show_rename_help_callback,
    STATE_NAME as RENAME_S1, STATE_FILE as RENAME_S2,
)
from handlers.duplikat import (
    cmd_duplikat, handle_duplikat_file, handle_show_duplikat_help_callback,
    STATE as DUPLICAT_STATE,
)
from handlers.walink import (
    cmd_walink, handle_walink_file, handle_show_walink_help_callback,
    STATE as WALINK_STATE,
)
from handlers.walinkweb import (
    cmd_walinkweb, handle_walinkweb_file, handle_walinkweb_msg,
    handle_show_walinkweb_help_callback,
    S0 as WALINKWEB_S0, S1 as WALINKWEB_S1,
)
from handlers.cleanup import (
    cmd_cleanup, handle_cleanup_file, handle_show_cleanup_help_callback,
    STATE as CLEANUP_STATE,
)
from handlers.txttovcf import (
    cmd_txttovcf,
    handle_ttv_contact_name, handle_ttv_per_file, handle_ttv_file_name,
    handle_ttv_awalan, handle_ttv_file, handle_ttv_done,
    handle_show_txttovcf_help_callback, handle_ttv_style_callback,
    S0, S1, S2, S3, S4, S5,
)
from handlers.xlsxtovcf import (
    cmd_xlsxtovcf,
    handle_xtv_contact_name, handle_xtv_per_file, handle_xtv_file_name,
    handle_xtv_awalan, handle_xtv_file, handle_xtv_done,
    handle_show_xlsxtovcf_help_callback, handle_xtv_style_callback,
    S0 as XTV_S0, S1 as XTV_S1, S2 as XTV_S2, S3 as XTV_S3, S4 as XTV_S4, S5 as XTV_S5,
)
from handlers.backup import cmd_backup
from handlers.manual import (
    cmd_manual,
    handle_manual_text,
    handle_manual_format_callback,
    handle_manual_contact_name,
    handle_manual_file_name,
    handle_show_manual_help_callback,
    S_WAIT_TEXT,
    S_WAIT_FORMAT,
    S_WAIT_CONTACTNAME,
    S_WAIT_FILENAME,
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
from config import (
    GLOBAL_MAX_CONCURRENT,
    GLOBAL_MAX_CONCURRENT_FILE,
    USER_CLICK_COOLDOWN,
)

MAX_CONCURRENT_PER_USER = 2
user_semaphores      = defaultdict(lambda: Semaphore(MAX_CONCURRENT_PER_USER))

global_semaphore      = Semaphore(GLOBAL_MAX_CONCURRENT)
global_file_semaphore = Semaphore(GLOBAL_MAX_CONCURRENT_FILE)

_user_last_click: dict = {}
_user_last_active: dict = {}
_error_last_sent: dict = {}
_job_locks = {}

def get_job_lock(name: str) -> asyncio.Lock:
    return _job_locks.setdefault(name, asyncio.Lock())

_start_time = time.time()


def rate_limiter(func):
    async def wrapper(update: Update, context):
        if not update or not update.effective_user:
            return await func(update, context)

        user_id = update.effective_user.id
        _user_last_active[user_id] = time.time()

        # Cooldown Anti-Spam (smart debounce) HANYA untuk klik tombol inline (callback query)
        if update.callback_query:
            now = time.time()
            last_click = _user_last_click.get(user_id, 0)
            if now - last_click < USER_CLICK_COOLDOWN:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
                return
            _user_last_click[user_id] = now

        async with global_semaphore:
            async with user_semaphores[user_id]:
                return await func(update, context)

    wrapper.__name__ = func.__name__
    return wrapper


def file_rate_limiter(func):
    async def wrapper(update: Update, context):
        if not update or not update.effective_user:
            return await func(update, context)

        user_id = update.effective_user.id
        _user_last_active[user_id] = time.time()

        # Hanya gunakan 1 global semaphore — tidak ada antrian per-user untuk file
        # User bisa upload banyak file paralel, yang dibatasi hanya total global
        async with global_file_semaphore:
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
    if not update.message:
        return
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
        PECAHTXT_S1:      handle_pecahtxt_per_file,
        RENAME_S1:        handle_rename_name,
        S1:               handle_ttv_contact_name,
        S2:               handle_ttv_per_file,
        S3:               handle_ttv_file_name,
        S4:               handle_ttv_awalan,
        XTV_S1:           handle_xtv_contact_name,
        XTV_S2:           handle_xtv_per_file,
        XTV_S3:           handle_xtv_file_name,
        XTV_S4:           handle_xtv_awalan,
        S_WAIT_TEXT:      handle_manual_text,
        S_WAIT_CONTACTNAME: handle_manual_contact_name,
        S_WAIT_FILENAME:  handle_manual_file_name,
        WALINKWEB_S1:     handle_walinkweb_msg,
        BROADCAST_STATE:  handle_broadcast_msg,
        NEWMEMBER_STATE:  handle_newmember_id,
        DELMEMBER_STATE:  handle_delmember_id,
        MEDIA_BROADCAST_STATE: handle_broadcast_media,
    }
    handler = route.get(state)
    if handler:
        await handler(update, context)


async def file_router(update: Update, context):
    if not update.message:
        return
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
        PECAHTXT_S2:    (handle_pecahtxt_file,    "file TXT berupa DOKUMEN"),
        RENAME_S2:      (handle_rename_file,      "file VCF berupa DOKUMEN"),
        S0:             (handle_ttv_file,         "file TXT berupa DOKUMEN"),
        S5:             (handle_ttv_file,         "file TXT berupa DOKUMEN"),
        XTV_S0:         (handle_xtv_file,         "file XLSX/CSV berupa DOKUMEN"),
        XTV_S5:         (handle_xtv_file,         "file XLSX/CSV berupa DOKUMEN"),
        COUNT_STATE:    (handle_count_file,       "file VCF berupa DOKUMEN"),
        XLSX2TXT_STATE: (handle_xlsxtotxt_file,   "file XLSX/CSV berupa DOKUMEN"),
        DUPLICAT_STATE: (handle_duplikat_file,    "file VCF atau TXT berupa DOKUMEN"),
        WALINK_STATE:   (handle_walink_file,      "file VCF atau TXT berupa DOKUMEN"),
        WALINKWEB_S0:   (handle_walinkweb_file,   "file XLSX, CSV, TXT, atau VCF berupa DOKUMEN"),
        CLEANUP_STATE:  (handle_cleanup_file,     "file VCF atau TXT berupa DOKUMEN"),
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
    
    if update.callback_query:
        await update.callback_query.answer()
        
        class WrappedUpdate:
            def __init__(self, original_update, msg):
                self._update = original_update
                self.message = msg
            def __getattr__(self, name):
                return getattr(self._update, name)
                
        update = WrappedUpdate(update, update.callback_query.message)

    if not sess or not sess.get("state"):
        await update.message.reply_text("Tidak ada proses aktif.")
        return

    state = sess.get("state")
    route = {
        MERGE_STATE:    handle_merge_done,
        VCF2TXT_STATE:  handle_vcftotxt_done,
        PECAHTXT_S2:    handle_pecahtxt_done,
        S0:             handle_ttv_done,
        S5:             handle_ttv_done,
        XTV_S0:         handle_xtv_done,
        XTV_S5:         handle_xtv_done,
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
    Jawab query lalu edit/tampilkan pesan VIP langsung.
    """
    query = update.callback_query
    await query.answer()

    # Hapus pesan peringatan VIP/redirect agar layar tidak menumpuk
    try:
        await query.message.delete()
    except Exception:
        pass

    if VIP_PAKASIR_MODE:
        from handlers.vip_pakasir import cmd_vip
        await cmd_vip(update, context)
    else:
        from handlers.vip import cmd_vip
        await cmd_vip(update, context)


# ── show_referral_menu callback ────────────────────────────────────────────────
async def cb_show_referral_menu(update: Update, context):
    """
    Callback dari tombol 'Dapatkan VIP Gratis' di require_member.
    Jawab query lalu tampilkan halaman referral langsung secara in-place.
    """
    query = update.callback_query
    await query.answer()

    # Hapus pesan peringatan VIP/redirect agar layar tidak menumpuk
    try:
        await query.message.delete()
    except Exception:
        pass

    from handlers.referral import cmd_referral
    await cmd_referral(update, context)


# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def _job_expire_vip(context):
    lock = get_job_lock("expire")
    if lock.locked():
        return
    async with lock:
        count = await adb.expire_vip_members()
        if count:
            logger.info("[JOB] %d VIP expired", count)


async def _job_cleanup_sessions(context):
    lock = get_job_lock("cleanup")
    if lock.locked():
        return
    async with lock:
        MEM_CLEANUP_TIMEOUT = 3600  # 1 jam asinkronus RAM cleanup (anti memory leaks)
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
        now = time.time()
        for uid in list(user_semaphores.keys()):
            last_active = _user_last_active.get(uid, 0)
            if last_active == 0:
                user = db.get_user(uid)
                if user:
                    try:
                        from datetime import datetime
                        last_active_dt = datetime.fromisoformat(dict(user)["last_active"])
                        last_active = last_active_dt.timestamp()
                    except Exception:
                        pass

            if now - last_active > MEM_CLEANUP_TIMEOUT:
                sem = user_semaphores.get(uid)
                if sem is None or sem._value == MAX_CONCURRENT_PER_USER:
                    inactive.append(uid)

        for uid in inactive:
            user_semaphores.pop(uid, None)
            _user_last_click.pop(uid, None)
            _user_last_active.pop(uid, None)
        if inactive:
            logger.info("[JOB] Cleaned %d inactive semaphores, click timers, and active trackers", len(inactive))

        cleaned_cache = await adb.cleanup_stale_sessions()
        if cleaned_cache:
            logger.info("[JOB] Cleaned %d stale session cache", cleaned_cache)

        # Cleanup welcome messages dan locks/timers di seluruh handler untuk mencegah memory leak
        try:
            from handlers.start import _welcome_messages
            import handlers.txttovcf
            import handlers.pecahtxt
            import handlers.pecahvcf
            import handlers.vcftotxt
            import handlers.xlsxtovcf
            import handlers.merge
            import handlers.admin_navy
            import handlers.count
            import handlers.duplikat
            import handlers.rename
            import handlers.walink
            import handlers.walinkweb
            import handlers.xlsxtotxt
            import handlers.cleanup
            import handlers.manual

            active_uids = set(_welcome_messages.keys())
            active_uids.update(handlers.txttovcf._user_locks.keys())
            active_uids.update(handlers.pecahtxt._user_locks.keys())
            active_uids.update(handlers.pecahvcf._processing)
            active_uids.update(handlers.vcftotxt._user_locks.keys())
            active_uids.update(handlers.xlsxtovcf._user_locks.keys())
            active_uids.update(handlers.merge._user_locks.keys())
            active_uids.update(handlers.admin_navy._user_locks.keys())
            active_uids.update(handlers.count._user_locks.keys())
            active_uids.update(handlers.duplikat._user_locks.keys())
            active_uids.update(handlers.rename._user_locks.keys())
            active_uids.update(handlers.walink._processing)
            active_uids.update(handlers.walinkweb._processing)
            active_uids.update(handlers.walinkweb._user_locks.keys())
            active_uids.update(handlers.xlsxtotxt._master_locks.keys())
            active_uids.update(handlers.cleanup._processing)
            active_uids.update(handlers.manual._user_locks.keys())

            inactive_ids = []
            from datetime import datetime
            now = datetime.now()
            for uid in active_uids:
                user = db.get_user(uid)
                if user:
                    try:
                        last = datetime.fromisoformat(dict(user)["last_active"])
                        if (now - last).total_seconds() > MEM_CLEANUP_TIMEOUT:
                            inactive_ids.append(uid)
                    except Exception:
                        inactive_ids.append(uid)
                else:
                    inactive_ids.append(uid)

            if inactive_ids:
                for uid in inactive_ids:
                    _welcome_messages.pop(uid, None)
                
                handlers.txttovcf.cleanup_inactive_users(inactive_ids)
                handlers.pecahtxt.cleanup_inactive_users(inactive_ids)
                handlers.pecahvcf.cleanup_inactive_users(inactive_ids)
                handlers.vcftotxt.cleanup_inactive_users(inactive_ids)
                handlers.xlsxtovcf.cleanup_inactive_users(inactive_ids)
                handlers.merge.cleanup_inactive_users(inactive_ids)
                handlers.admin_navy.cleanup_inactive_users(inactive_ids)
                handlers.count.cleanup_inactive_users(inactive_ids)
                handlers.duplikat.cleanup_inactive_users(inactive_ids)
                handlers.rename.cleanup_inactive_users(inactive_ids)
                handlers.walink.cleanup_inactive_users(inactive_ids)
                handlers.walinkweb.cleanup_inactive_users(inactive_ids)
                handlers.xlsxtotxt.cleanup_inactive_users(inactive_ids)
                handlers.cleanup.cleanup_inactive_users(inactive_ids)
                handlers.manual.cleanup_inactive_users(inactive_ids)
                
                logger.info("[JOB] Cleaned %d inactive users from welcome messages and handler locks/timers", len(inactive_ids))
        except Exception as e_cleanup:
            logger.error("[JOB] Error during active users memory cleanup: %s", e_cleanup)




async def _job_notify_expiry(context):
    lock = get_job_lock("notify")
    if lock.locked():
        return
    async with lock:
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
        except Exception:
            pass


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
            stats = _db.get_in_memory_stats()
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
    # Explicitly initialize SQLite database and connection pool
    from database.db import init_db
    init_db()

    # Health check server (background)
    threading.Thread(target=_run_health_server, daemon=True, name="health-server").start()
    # FIX: gunakan HEALTH_PORT dari config (konsisten)
    logger.info("Health check server started on port %s", HEALTH_PORT)

    async def startup_init(application):
        try:
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            import atexit
            loop = asyncio.get_running_loop()
            executor = ThreadPoolExecutor(max_workers=128, thread_name_prefix="asyncio-worker")
            loop.set_default_executor(executor)
            atexit.register(executor.shutdown, wait=False)
            logger.info("✅ Event loop default executor configured with 128 threads")
        except Exception as e:
            logger.error("Failed to set default executor: %s", e)

        try:
            from database.db_async import adb
            expired = await adb.expire_vip_members()
            if expired:
                logger.info("Startup: %d VIP expired", expired)
        except Exception as e:
            logger.error("Startup VIP expiration failed: %s", e)

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
                    import asyncio
                    from webhook_pakasir import start_webhook_server_thread
                    # Ambil running event loop dari thread utama bot
                    loop = asyncio.get_running_loop()
                    start_webhook_server_thread(WEBHOOK_PORT, application.bot, loop)
                    logger.info("Pakasir webhook server started on port %s", WEBHOOK_PORT)
                except Exception as e:
                    logger.error("Gagal start webhook server: %s", e)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(f"http://localhost:{LOCAL_BOT_API_PORT}/bot")
        .base_file_url(f"http://localhost:{LOCAL_BOT_API_PORT}/file/bot")
        .local_mode(True)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .concurrent_updates(256)      # proses hingga 256 update paralel
        .connection_pool_size(256)    # 256 koneksi ke Telegram API
        .pool_timeout(60)
        .read_timeout(30)
        .write_timeout(120)
        .connect_timeout(30)
        .post_init(startup_init)
        .build()
    )


    # ── Command handlers ──
    app.add_handler(CommandHandler("start",                              rate_limiter(cmd_start)))
    app.add_handler(CommandHandler(["reset", "resetdatabase"],          rate_limiter(cmd_reset)))
    app.add_handler(CommandHandler(["admin", "Admin"],                  rate_limiter(cmd_admin)))
    app.add_handler(CommandHandler("txttovcf",                          rate_limiter(cmd_txttovcf)))
    app.add_handler(CommandHandler("xlsxtovcf",                         rate_limiter(cmd_xlsxtovcf)))
    app.add_handler(CommandHandler("xlsxtotxt",                         rate_limiter(cmd_xlsxtotxt)))
    app.add_handler(CommandHandler("merge",                             rate_limiter(cmd_merge)))
    app.add_handler(CommandHandler("vcftotxt",                          rate_limiter(cmd_vcftotxt)))
    app.add_handler(CommandHandler("pecahvcf",                          rate_limiter(cmd_pecahvcf)))
    app.add_handler(CommandHandler("pecahtxt",                          rate_limiter(cmd_pecahtxt)))
    app.add_handler(CommandHandler("rename",                            rate_limiter(cmd_rename)))
    app.add_handler(CommandHandler("count",                             rate_limiter(cmd_count)))
    app.add_handler(CommandHandler("duplikat",                          rate_limiter(cmd_duplikat)))
    app.add_handler(CommandHandler("walink",                            rate_limiter(cmd_walink)))
    app.add_handler(CommandHandler("walinkweb",                         rate_limiter(cmd_walinkweb)))
    app.add_handler(CommandHandler("cleanup",                           rate_limiter(cmd_cleanup)))
    app.add_handler(CommandHandler("manual",                            rate_limiter(cmd_manual)))
    app.add_handler(CommandHandler(["broadcast", "brodcast", "Brodcast"], rate_limiter(cmd_broadcast)))
    app.add_handler(CommandHandler("mediabroadcast",                    rate_limiter(cmd_media_broadcast)))
    app.add_handler(CommandHandler("stopbroadcast",                     rate_limiter(cmd_stop_broadcast)))
    app.add_handler(CommandHandler("newmember",                         rate_limiter(cmd_newmember)))
    app.add_handler(CommandHandler(["delmember", "copotmember"],        rate_limiter(cmd_delmember)))
    app.add_handler(CommandHandler(["referal", "referral"],             rate_limiter(cmd_referral)))
    app.add_handler(CommandHandler("daftar",                            rate_limiter(cmd_daftar)))
    app.add_handler(CommandHandler("vip",                               rate_limiter(cmd_vip)))
    app.add_handler(CommandHandler("addvip",                            rate_limiter(cmd_addvip)))
    app.add_handler(CommandHandler("delvip",                            rate_limiter(cmd_delvip)))
    app.add_handler(CommandHandler("stat",                              rate_limiter(cmd_stat)))
    app.add_handler(CommandHandler("backup",                            rate_limiter(cmd_backup)))
    app.add_handler(CommandHandler("akun",                             rate_limiter(cmd_akun)))
    app.add_handler(CommandHandler("done",                              rate_limiter(done_router)))

    # ── Callback handlers ──
    app.add_handler(CallbackQueryHandler(rate_limiter(cb_show_vip_menu),       pattern="^show_vip_menu$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(cb_show_referral_menu),  pattern="^show_referral_menu$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_back_to_start),   pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_ttv_style_callback), pattern="^ttv_style_"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_xtv_style_callback), pattern="^xtv_style_"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_duplikat_help_callback), pattern="^show_duplikat_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_count_help_callback), pattern="^show_count_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_txttovcf_help_callback), pattern="^show_txttovcf_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_xlsxtovcf_help_callback), pattern="^show_xlsxtovcf_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_vcftotxt_help_callback), pattern="^show_vcftotxt_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_xlsxtotxt_help_callback), pattern="^show_xlsxtotxt_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_merge_help_callback), pattern="^show_merge_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_pecahvcf_help_callback), pattern="^show_pecahvcf_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_pecahtxt_help_callback), pattern="^show_pecahtxt_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_rename_help_callback), pattern="^show_rename_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_walink_help_callback), pattern="^show_walink_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_walinkweb_help_callback), pattern="^show_walinkweb_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_cleanup_help_callback), pattern="^show_cleanup_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_manual_help_callback), pattern="^show_manual_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_manual_format_callback), pattern="^manual_fmt_"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_show_admin_help_callback), pattern="^show_admin_help$"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_reset_callback),  pattern="^admin_db_reset"))
    app.add_handler(CallbackQueryHandler(rate_limiter(handle_redeem_points),   pattern="^redeem_ref_"))
    app.add_handler(CallbackQueryHandler(rate_limiter(done_router),            pattern="^done$"))

    if VIP_PAKASIR_MODE:
        app.add_handler(CallbackQueryHandler(rate_limiter(handle_buy_vip),         pattern="^buy_vip_"))
        app.add_handler(CallbackQueryHandler(rate_limiter(handle_check_payment),   pattern="^check_payment_"))
        app.add_handler(CallbackQueryHandler(rate_limiter(handle_cancel_payment),  pattern="^cancel_payment_"))
        app.add_handler(CallbackQueryHandler(rate_limiter(handle_vip_history),     pattern="^vip_history$"))

    # ── Message handlers ──
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.ANIMATION,
        file_rate_limiter(file_router),
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rate_limiter(text_router)))

    # ── Error handler ──
    app.add_error_handler(error_handler)





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