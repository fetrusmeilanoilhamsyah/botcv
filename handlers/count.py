"""
handlers/count.py — Pembuat hitung kontak otomatis untuk berkas TXT dan VCF.
Single-Message Morphing Wizard. Tanya file dulu, edit disatu pesan.
"""
import os
import shutil
import asyncio
import logging
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir

logger = logging.getLogger(__name__)

STATE = "COUNT_COLLECTING"
S2 = "COUNT_PROCESSING"

MAX_FILES = 100
MAX_SIZE_MB = 50

_user_locks: dict = {}
_user_timers: dict = {}

def _get_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS: {count} FILE]</b>" if count else "<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count}</code>" if count else "Berkas: ➖")
        
    if step == 2:
        parts.append("<b>[HITUNG]</b>")
    else:
        parts.append("Hitung: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ CONTACT COUNT CONSOLE ]</b>\n"
                f"<blockquote>{breadcrumbs}</blockquote>\n"
        "\n"
    )

def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.txt</code> atau <code>.vcf</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"• Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"• Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file</blockquote>"
    )

def _count_contacts_sync(filepath: str, ext: str) -> int:
    count = 0
    try:
        if ext == ".txt":
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip():
                        count += 1
        elif ext == ".vcf":
            with open(filepath, "rb") as f:
                count = f.read().upper().count(b"BEGIN:VCARD")
    except Exception as e:
        logger.error("Error counting contacts in %s: %s", filepath, e)
    return count

def cleanup_inactive_users(inactive_ids: list) -> int:
    cleaned = 0
    for uid in inactive_ids:
        _user_locks.pop(uid, None)
        timer = _user_timers.pop(uid, None)
        if timer:
            timer.cancel()
        cleaned += 1
    return cleaned

async def _debounce_notify(user_id: int, context, chat_id: int):
    try:
        await asyncio.sleep(1)
        if _user_timers.get(user_id) is asyncio.current_task():
            from handlers.start import _get_transition_lock, register_welcome_messages, _welcome_messages
            async with _get_transition_lock(user_id):
                sess = db.get_session(user_id)
                if sess and sess.get("state") == STATE:
                    data = sess["data"]
                    jumlah = data["count"]
                    
                    text = (
                        _get_breadcrumbs(data, 1) +
                        f"<blockquote><b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                        f"Berhasil mengunduh <code>{jumlah}</code> berkas.\n\n"
                        f"Silakan pilih tindakan di bawah:</blockquote>"
                    )
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                            InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                        ]
                    ])
                    
                    # 1. Hapus status message lama
                    status_msg_id = data.get("status_msg_id")
                    if status_msg_id:
                        try:
                            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
                        except Exception:
                            pass

                    # 2. Hapus welcome messages lama
                    welcome_ids = _welcome_messages.pop(user_id, [])
                    for w_id in welcome_ids:
                        if w_id != status_msg_id:
                            try:
                                await context.bot.delete_message(chat_id=chat_id, message_id=w_id)
                            except Exception:
                                pass

                    # 3. Kirim baru di bawah berkas
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                    data["status_msg_id"] = msg.message_id
                    db.set_session(user_id, STATE, data)
                    register_welcome_messages(user_id, [msg.message_id])
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error in count: %s", e)

def _reset_timer(user_id, context, chat_id):
    old = _user_timers.get(user_id)
    if old:
        old.cancel()
    _user_timers[user_id] = asyncio.ensure_future(
        _debounce_notify(user_id, context, chat_id)
    )

def _cancel_timer(user_id):
    old = _user_timers.pop(user_id, None)
    if old:
        old.cancel()

def _clear_buffers(user_id: int):
    user_dir = get_user_dir(user_id)
    count_dir = os.path.join(user_dir, "count")
    import asyncio
    async def _bg_clear():
        try:
            import shutil
            await asyncio.to_thread(shutil.rmtree, count_dir, ignore_errors=True)
        except Exception:
            pass
    asyncio.create_task(_bg_clear())

async def cmd_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    init_data = {"count": 0, "total_size": 0}
    db.set_session(user_id, STATE, init_data)
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _waiting_text(init_data),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    
    if msg:
        # FIX Bug#3: pakai init_data langsung, tidak ambil ulang dari db setelah await
        init_data["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, init_data)

async def handle_count_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not os.path.splitext(doc.file_name)[1].lower() in (".txt", ".vcf"):
        # Format salah (User file tidak didelete)
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                sent_name = doc.file_name if doc and doc.file_name else "file tersebut"
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        f"<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{sent_name}</code> bukan berkas <code>.txt</code> atau <code>.vcf</code>.\n\n"
                        f"Kirim ulang berkas dengan format <code>.txt</code> atau <code>.vcf</code>.</blockquote>"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
                )
                await asyncio.sleep(10)
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=_waiting_text(sess["data"]),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
                )
        except Exception:
            pass
        return
        
    msg_id = update.message.message_id
    count_dir = os.path.join(get_user_dir(user_id), "count")
    os.makedirs(count_dir, exist_ok=True)
    ext = os.path.splitext(doc.file_name)[1].lower()
    out_path = os.path.join(count_dir, f"{msg_id}{ext}")
    
    try:
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive(out_path)

        async with _get_lock(user_id):
            sess = db.get_session(user_id)
            if not sess or sess["state"] != STATE:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            data = sess["data"]
            if data.get("is_processing"):
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if data["count"] >= MAX_FILES:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            data["count"] += 1
            data["total_size"] += doc.file_size
            db.set_session(user_id, STATE, data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        logger.error("Download failed in count: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

async def handle_show_count_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    init_data = {"count": 0, "total_size": 0}
    db.set_session(user_id, STATE, init_data)
    text = _waiting_text(init_data)
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, STATE, sess["data"])
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])

async def handle_count_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = db.get_session(update.effective_user.id)
    if not sess or sess["state"] != STATE:
        return
    data = sess["data"]
    if data["count"] == 0:
        return
        
    db.set_session(update.effective_user.id, S2, data)
    
    status_msg_id = data.get("status_msg_id")
    process_text = "<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang menghitung kontak...</blockquote>"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text=process_text,
            parse_mode="HTML"
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=process_text,
            parse_mode="HTML"
        )
    
    await handle_count_process(update, context)

async def handle_count_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    if not sess or not sess.get("data"):
        unregister_active_task(user_id)
        return
    data = sess["data"]
    status_msg_id = data.get("status_msg_id")
    
    user_dir = get_user_dir(user_id)
    count_dir = os.path.join(user_dir, "count")
    
    files = []
    if os.path.exists(count_dir):
        files = [f for f in os.listdir(count_dir) if (f.endswith('.txt') or f.endswith('.vcf'))]
        def _safe_sort_key(x):
            try:
                return int(x.split('.')[0])
            except (ValueError, IndexError):
                return 0
        files.sort(key=_safe_sort_key)

    loop = asyncio.get_running_loop()

    try:
        def do_count():
            total_kontak = 0
            for f in files:
                path = os.path.join(count_dir, f)
                ext = os.path.splitext(f)[1].lower()
                try:
                    cnt = _count_contacts_sync(path, ext)
                    total_kontak += cnt
                except Exception:
                    pass
            return total_kontak

        total_kontak = await loop.run_in_executor(None, do_count)

        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass

        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("HITUNG FILE LAIN", callback_data="show_count_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])

        total_file = len(files)
        avg = total_kontak // total_file if total_file else 0

        box_text = (
            f"<b>[ PROSES SELESAI ]</b>\n"
            f"<blockquote>"
            f"• Total Berkas : {total_file} FILE\n"
            f"• Total Kontak : {total_kontak:,}\n"
            f"• Rerata / Berkas : {avg:,}</blockquote>\n\n"
            f"<i>Hitung kontak selesai!</i>"
        )
        
        final_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=box_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        
        from handlers.start import register_welcome_messages
        register_welcome_messages(user_id, [final_msg.message_id])

    except Exception as e:
        logger.error("Count process failed: %s", e)
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="<blockquote>⚠️ <b>Terjadi kesalahan saat memproses.</b></blockquote>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        _clear_buffers(user_id)
        db.clear_session(user_id)
        unregister_active_task(user_id)
