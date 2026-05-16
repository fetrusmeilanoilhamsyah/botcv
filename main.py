"""
main.py - OPTIMIZED VERSION
Entry point DiBot CV FEE.

CHANGELOG:
- Set concurrent_updates = 8 (max 8 parallel processes)
- Naikin timeout configuration
- Add rate limiting middleware
- Fix logging level
- ADDED: Memory leak fixes for handlers
"""
import logging
import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from logging.handlers import RotatingFileHandler
from config import BOT_TOKEN
from database import db

from handlers.start import cmd_start
from handlers.reset import cmd_reset
from handlers.admin_navy import (
    cmd_admin,
    handle_admin_navy,
    STATES as AN_STATES,
)
from handlers.merge import (
    cmd_merge,
    handle_merge_file,
    handle_merge_done,
    handle_merge_naming,
    STATE as MERGE_STATE,
    STATE_NAMING as MERGE_NAMING,
)
from handlers.vcftotxt import (
    cmd_vcftotxt,
    handle_vcftotxt_file,
    handle_vcftotxt_done,
    handle_vcftotxt_naming,
    STATE as VCF2TXT_STATE,
    STATE_NAMING as VCF2TXT_NAMING,
)
from handlers.count import (
    STATE as COUNT_STATE,
    cmd_count,
    handle_count_file,
    handle_count_done,
)
from handlers.xlsxtotxt import (
    STATE as XLSX2TXT_STATE,
    cmd_xlsxtotxt,
    handle_xlsxtotxt_file,
    handle_xlsxtotxt_done,
)
from handlers.pecahvcf import (
    cmd_pecahvcf,
    handle_pecah_per_file,
    handle_pecah_vcf_file,
    STATE_PER_FILE as PECAH_S1,
    STATE_WAIT_VCF as PECAH_S2,
)
from handlers.rename import (
    cmd_rename,
    handle_rename_name,
    handle_rename_file,
    STATE_NAME as RENAME_S1,
    STATE_FILE as RENAME_S2,
)
from handlers.txttovcf import (
    cmd_txttovcf,
    handle_ttv_contact_name,
    handle_ttv_per_file,
    handle_ttv_file_name,
    handle_ttv_awalan,
    handle_ttv_file,
    handle_ttv_done,
    S0, S1, S2, S3, S4, S5,
)
from handlers.broadcast import (
    cmd_broadcast,
    handle_broadcast_msg,
    STATE as BROADCAST_STATE,
)
from handlers.media_broadcast import (
    cmd_media_broadcast,
    handle_broadcast_media,
    STATE as MEDIA_BROADCAST_STATE,
)
from handlers.new_member import (
    cmd_newmember,
    handle_newmember_id,
    STATE as NEWMEMBER_STATE,
)
from handlers.referral import cmd_referral
from handlers.del_member import (
    cmd_delmember,
    handle_delmember_id,
    STATE as DELMEMBER_STATE,
)
from handlers.daftar import cmd_daftar
from handlers.vip import cmd_vip
from handlers.addvip import cmd_addvip, cmd_delvip
from handlers.stat import cmd_stat

# Rate limiting imports
from asyncio import Semaphore
from collections import defaultdict

from aiohttp import web
import threading
import time

os.makedirs("logs", exist_ok=True)

# ===== OPTIMIZED LOGGING =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # Mencegah spam log getUpdates HTTP
logger = logging.getLogger(__name__)

# Error handler rate limiting
_error_last_sent = {}
from config import (
    ERROR_ALERT_COOLDOWN as _ERROR_COOLDOWN,
    JOB_EXPIRE_VIP_INTERVAL,
    JOB_CLEANUP_SESSION_INTERVAL,
    JOB_NOTIFY_EXPIRY_INTERVAL,
    SESSION_STUCK_TIMEOUT,
    SESSION_INACTIVE_TIMEOUT
)

# Job overlap prevention
_job_running = {"expire": False, "cleanup": False, "notify": False}

# ===== RATE LIMITING =====
# Command: max 2 operasi paralel per user (cegah spam)
# File upload: max 16 agar file ke-3, 4, dst tidak antri saat user kirim banyak sekaligus
MAX_CONCURRENT_PER_USER = 2
MAX_CONCURRENT_FILE_UPLOAD = 16
user_semaphores      = defaultdict(lambda: Semaphore(MAX_CONCURRENT_PER_USER))
user_file_semaphores = defaultdict(lambda: Semaphore(MAX_CONCURRENT_FILE_UPLOAD))


def rate_limiter(func):
    """Decorator untuk rate limiting per user (untuk command)"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text if update.message else "NON-TEXT"
        logger.debug(f"Incoming: User {user_id} -> {text}")
        
        semaphore = user_semaphores[user_id]
        
        async with semaphore:
            return await func(update, context)
    
    return wrapper


def file_rate_limiter(func):
    """Decorator untuk rate limiting file upload — limit lebih longgar agar tidak antri"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        logger.debug(f"Incoming: User {user_id} -> None")
        
        semaphore = user_file_semaphores[user_id]
        
        async with semaphore:
            return await func(update, context)
    
    return wrapper


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import NetworkError, TimedOut, Conflict
    import time
    
    # NetworkError / TimedOut / Conflict = koneksi/polling issue — PTB auto-retry
    if isinstance(context.error, (NetworkError, TimedOut, Conflict)):
        logger.warning("Network/Conflict error (akan auto-retry): %s", context.error)
        return

    logger.error("Exception saat handle update:", exc_info=context.error)

    # Rate limiting untuk error alerts ke admin
    now = time.time()
    error_type = type(context.error).__name__
    
    # Check if we sent this error type recently
    if error_type in _error_last_sent:
        if now - _error_last_sent[error_type] < _ERROR_COOLDOWN:
            logger.warning("Error alert throttled: %s (sent %ds ago)", 
                          error_type, int(now - _error_last_sent[error_type]))
            return
    
    _error_last_sent[error_type] = now

    # Kirim alert ke semua admin di Telegram
    from config import ADMIN_IDS
    import traceback
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    short_tb = tb[-1500:] if len(tb) > 1500 else tb
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"<b>ERROR BOT</b>\n<code>{error_type}</code>\n<pre>{short_tb}</pre>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            "Terjadi kesalahan. Ketik /reset untuk mereset sesi."
        )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess:
        return

    state = sess.get("state")

    # Selesaikan proses jika kata kunci "selesai" atau "done" dikirim
    # PENGECUALIAN: Jangan tangkap jika dalam mode broadcast agar admin bisa kirim kata tersebut
    if update.message and update.message.text:
        text_lower = update.message.text.strip().lower()
        if text_lower in ["selesai", "done"]:
            if state not in [BROADCAST_STATE, MEDIA_BROADCAST_STATE]:
                await done_router(update, context)
                return

    if state in AN_STATES.values():
        await handle_admin_navy(update, context)
    elif state == MERGE_NAMING:
        await handle_merge_naming(update, context)
    elif state == VCF2TXT_NAMING:
        await handle_vcftotxt_naming(update, context)
    elif state == PECAH_S1:
        await handle_pecah_per_file(update, context)
    elif state == RENAME_S1:
        await handle_rename_name(update, context)
    elif state == S1:
        await handle_ttv_contact_name(update, context)
    elif state == S2:
        await handle_ttv_per_file(update, context)
    elif state == S3:
        await handle_ttv_file_name(update, context)
    elif state == S4:
        await handle_ttv_awalan(update, context)
    elif state == BROADCAST_STATE:
        await handle_broadcast_msg(update, context)
    elif state == NEWMEMBER_STATE:
        await handle_newmember_id(update, context)
    elif state == DELMEMBER_STATE:
        await handle_delmember_id(update, context)
    elif state == MEDIA_BROADCAST_STATE:
        await handle_broadcast_media(update, context)


async def file_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess:
        return
    state = sess.get("state")
    
    # Validasi Media: Kebanyakan handler hanya menerima dokumen
    is_doc = bool(update.message.document)
    
    if state == MERGE_STATE:
        if not is_doc:
            await update.message.reply_text("Kirim file VCF atau TXT berupa DOKUMEN.")
            return
        await handle_merge_file(update, context)
    elif state == VCF2TXT_STATE:
        if not is_doc:
            await update.message.reply_text("Kirim file VCF berupa DOKUMEN.")
            return
        await handle_vcftotxt_file(update, context)
    elif state == PECAH_S2:
        if not is_doc:
            await update.message.reply_text("Kirim file VCF berupa DOKUMEN.")
            return
        await handle_pecah_vcf_file(update, context)
    elif state == RENAME_S2:
        if not is_doc:
            await update.message.reply_text("Kirim file VCF berupa DOKUMEN.")
            return
        await handle_rename_file(update, context)
    elif state in [S0, S5]:
        if not is_doc:
            await update.message.reply_text("Kirim file TXT berupa DOKUMEN.")
            return
        await handle_ttv_file(update, context)
    elif state == COUNT_STATE:
        if not is_doc:
            await update.message.reply_text("Kirim file VCF berupa DOKUMEN.")
            return
        await handle_count_file(update, context)
    elif state == XLSX2TXT_STATE:
        if not is_doc:
            await update.message.reply_text("Kirim file XLSX/CSV berupa DOKUMEN.")
            return
        await handle_xlsxtotxt_file(update, context)
    elif state == MEDIA_BROADCAST_STATE:
        # Media broadcast mendukung foto/video/animasi
        await handle_broadcast_media(update, context)

# The done_router function is now integrated into text_router for "selesai", "/done", "done" messages.
# However, the CommandHandler("done", ...) still needs a function.
# We can keep a simplified done_router for the /done command specifically.
async def done_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess:
        await update.message.reply_text("Tidak ada proses aktif.")
        return
    state = sess.get("state")

    if state == MERGE_STATE:
        await handle_merge_done(update, context)
    elif state == VCF2TXT_STATE:
        await handle_vcftotxt_done(update, context)
    elif state in [S0, S5]:
        await handle_ttv_done(update, context)
    elif state == COUNT_STATE:
        await handle_count_done(update, context)
    elif state == XLSX2TXT_STATE:
        await handle_xlsxtotxt_done(update, context)
    else:
        await update.message.reply_text("Tidak ada proses aktif yang bisa diselesaikan.")


# Health check HTTP server
async def health_check(request):
    """Simple health check endpoint untuk monitoring"""
    try:
        stats = db.get_db_stats()
        return web.json_response({
            "status": "healthy",
            "uptime_seconds": int(time.time() - _start_time),
            "database": stats,
            "active_semaphores": len(user_semaphores),
            "active_timers": sum(len(d) for d in [_user_timers] if hasattr(globals().get('_user_timers', {}), '__len__'))
        })
    except Exception as e:
        return web.json_response({
            "status": "unhealthy",
            "error": str(e)
        }, status=500)

def run_health_server():
    """Run health check server on port 8080"""
    app_http = web.Application()
    app_http.router.add_get('/health', health_check)
    web.run_app(app_http, host='0.0.0.0', port=8080, print=None)


def main():
    """
    OPTIMIZED APPLICATION BUILDER UNTUK 50-100 USER BERSAMAAN
    """
    # Start health check server di background thread
    global _start_time
    _start_time = time.time()
    
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    logger.info("🏥 Health check server running on http://0.0.0.0:8080/health")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(32)       # Max 32 request diproses paralel (match pool size)
        .connection_pool_size(100)    # Naikkan pool network request ke Telegram API
        .pool_timeout(30)
        .read_timeout(30)             # Lebih pendek agar restart cepat (was 60)
        .write_timeout(120)
        .connect_timeout(30)
        .build()
    )

    # Command handlers
    app.add_handler(CommandHandler("start", rate_limiter(cmd_start)))
    app.add_handler(CommandHandler(["reset", "resetdatabase"], rate_limiter(cmd_reset)))
    app.add_handler(CommandHandler(["admin", "Admin"], rate_limiter(cmd_admin)))
    app.add_handler(CommandHandler("txttovcf", rate_limiter(cmd_txttovcf)))
    app.add_handler(CommandHandler("xlsxtotxt", rate_limiter(cmd_xlsxtotxt)))
    app.add_handler(CommandHandler("merge", rate_limiter(cmd_merge)))
    app.add_handler(CommandHandler("vcftotxt", rate_limiter(cmd_vcftotxt)))
    app.add_handler(CommandHandler("pecahvcf", rate_limiter(cmd_pecahvcf)))
    app.add_handler(CommandHandler("rename", rate_limiter(cmd_rename)))
    app.add_handler(CommandHandler("count", rate_limiter(cmd_count)))
    app.add_handler(CommandHandler(["broadcast", "brodcast", "Brodcast"], rate_limiter(cmd_broadcast)))
    app.add_handler(CommandHandler("mediabroadcast", rate_limiter(cmd_media_broadcast)))
    app.add_handler(CommandHandler("newmember", rate_limiter(cmd_newmember)))
    app.add_handler(CommandHandler(["delmember", "copotmember"], rate_limiter(cmd_delmember)))
    app.add_handler(CommandHandler(["referal", "referral"], rate_limiter(cmd_referral)))
    app.add_handler(CommandHandler("daftar", rate_limiter(cmd_daftar)))
    app.add_handler(CommandHandler("vip", rate_limiter(cmd_vip)))
    app.add_handler(CommandHandler("addvip", rate_limiter(cmd_addvip)))
    app.add_handler(CommandHandler("delvip", rate_limiter(cmd_delvip)))
    app.add_handler(CommandHandler("stat", rate_limiter(cmd_stat)))
    app.add_handler(CommandHandler("done", rate_limiter(done_router)))

    # Callback Query Handlers
    from handlers.reset import handle_reset_callback

    async def cb_show_vip_menu(update, context):
        query = update.callback_query
        await query.answer()
        # Fake update object for cmd_vip since it expects a message
        class FakeUpdate:
            message = query.message
            effective_user = update.effective_user
        await cmd_vip(FakeUpdate(), context)

    app.add_handler(CallbackQueryHandler(cb_show_vip_menu, pattern="^show_vip_menu$"))
    app.add_handler(CallbackQueryHandler(handle_reset_callback, pattern="^admin_db_reset"))

    # Message handlers
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.ANIMATION, file_rate_limiter(file_router)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rate_limiter(text_router)))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("🚀 DiBot CV FEE berjalan (OPTIMIZED VERSION)...")
    print("🚀 DiBot CV FEE berjalan (OPTIMIZED VERSION)...")
    print(f"📊 Max concurrent updates: 32")
    print(f"⏱️  Timeouts: pool=30s, read=30s, write=120s, connect=30s")
    print(f"🔐 Rate limit: Max {MAX_CONCURRENT_PER_USER} operations per user")

    # Auto-expire VIP members on startup
    expired_count = db.expire_vip_members()
    if expired_count:
        logger.info("%d VIP member expired direset saat startup", expired_count)

    # ── Scheduled jobs via PTB JobQueue ────────────────────────────────────────
    async def job_expire_vip(context):
        """Setiap 1 jam — expire VIP yang habis masa berlaku"""
        if _job_running["expire"]:
            logger.warning("[JOB] Previous expire_vip still running, skip")
            return
        
        _job_running["expire"] = True
        try:
            count = db.expire_vip_members()
            if count:
                logger.info("[JOB] %d VIP member expired", count)
        finally:
            _job_running["expire"] = False

    async def job_cleanup_sessions(context):
        """Cleanup direktori tmp sesi yang stuck + cleanup semaphores & locks"""
        if _job_running["cleanup"]:
            logger.warning("[JOB] Previous cleanup still running, skip")
            return
        
        _job_running["cleanup"] = True
        try:
            from middleware.session import clear_user_dir
            from datetime import datetime
            
            tmp_base = os.path.join("tmp", "sessions")
            if not os.path.exists(tmp_base):
                return
            
            now   = time.time()
            cleaned = 0
            
            try:
                for uid_str in os.listdir(tmp_base):
                    if not uid_str.isdigit():
                        continue
                    path = os.path.join(tmp_base, uid_str)
                    try:
                        if os.path.isdir(path) and (now - os.path.getmtime(path)) > SESSION_STUCK_TIMEOUT:
                            sess = db.get_session(int(uid_str))
                            if not sess or sess.get("state") is None:
                                clear_user_dir(int(uid_str))
                                cleaned += 1
                    except Exception:
                        pass
            except Exception:
                pass
            
            if cleaned:
                logger.info("[JOB] Cleaned %d stuck session dirs", cleaned)
            
            # Cleanup semaphores untuk user inactive
            inactive_users = []
            
            for uid in list(user_semaphores.keys()):
                sess = db.get_session(uid)
                if not sess or sess.get("state") is None:
                    user = db.get_user(uid)
                    if user:
                        try:
                            from datetime import datetime
                            last_active = datetime.fromisoformat(dict(user)["last_active"])
                            if (datetime.now() - last_active).total_seconds() > SESSION_INACTIVE_TIMEOUT:
                                inactive_users.append(uid)
                        except:
                            pass
            
            cleaned_sem = 0
            for uid in inactive_users:
                user_semaphores.pop(uid, None)
                user_file_semaphores.pop(uid, None)
                cleaned_sem += 1
            
            if cleaned_sem:
                logger.info("[JOB] Cleaned %d inactive user semaphores", cleaned_sem)

            # ====== ADDED: FIX BUG #26 & #27 (Cleanup locks and timers) ======
            cleaned_handler_locks = 0
            try:
                from handlers.xlsxtotxt import cleanup_inactive_locks as cleanup_xlsx
                from handlers.merge import cleanup_inactive_users as cleanup_merge
                from handlers.txttovcf import cleanup_inactive_users as cleanup_ttv
                from handlers.vcftotxt import cleanup_inactive_users as cleanup_v2t
                from handlers.admin_navy import cleanup_inactive_users as cleanup_an
                from handlers.count import cleanup_inactive_users as cleanup_count
                from handlers.pecahvcf import cleanup_inactive_users as cleanup_pecah
                from handlers.rename import cleanup_inactive_users as cleanup_rename

                cleaned_handler_locks += cleanup_xlsx(inactive_users)
                cleaned_handler_locks += cleanup_merge(inactive_users)
                cleaned_handler_locks += cleanup_ttv(inactive_users)
                cleaned_handler_locks += cleanup_v2t(inactive_users)
                cleaned_handler_locks += cleanup_an(inactive_users)
                cleaned_handler_locks += cleanup_count(inactive_users)
                cleaned_handler_locks += cleanup_pecah(inactive_users)
                cleaned_handler_locks += cleanup_rename(inactive_users)
            except Exception as e:
                logger.error("[JOB] Error cleaning handler locks: %s", e)

            if cleaned_handler_locks:
                logger.info("[JOB] Cleaned %d inactive handler locks/timers", cleaned_handler_locks)
            # =================================================================
                
            # Cleanup stale session cache
            cleaned_cache = db.cleanup_stale_sessions()
            if cleaned_cache:
                logger.info("[JOB] Cleaned %d stale session cache entries", cleaned_cache)
        finally:
            _job_running["cleanup"] = False

    async def job_notify_expiry(context):
        """Cek user yang akan habis masa berlakunya dalam 24 jam"""
        if _job_running["notify"]:
            logger.warning("[JOB] Previous notify_expiry still running, skip")
            return
        
        _job_running["notify"] = True
        try:
            users = db.get_users_for_expiry_notif()
            for u in users:
                uid = u["id"]
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text="Pemberitahuan: Masa aktif VIP kamu tinggal 24 jam lagi. Yuk perpanjang agar fitur premium tetap aktif."
                    )
                    db.mark_expiry_notified(uid)
                except Exception:
                    pass
        finally:
            _job_running["notify"] = False

    app.job_queue.run_repeating(job_expire_vip,    interval=JOB_EXPIRE_VIP_INTERVAL, first=60)
    app.job_queue.run_repeating(job_cleanup_sessions, interval=JOB_CLEANUP_SESSION_INTERVAL, first=120)
    app.job_queue.run_repeating(job_notify_expiry,   interval=JOB_NOTIFY_EXPIRY_INTERVAL, first=300)
    # ───────────────────────────────────────────────────────────────────────────

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()