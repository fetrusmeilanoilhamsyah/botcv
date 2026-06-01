"""
handlers/duplikat.py — Pembersih nomor kontak duplikat untuk file VCF dan TXT.
Single-Message Morphing Wizard. Tanya file dulu, edit disatu pesan, hapus chat iseng/file user.
"""
import os
import io
import asyncio
import logging
import shutil
from telegram import ReplyKeyboardRemove, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf, add_plus

logger = logging.getLogger(__name__)

STATE = "DUPLICAT_WAIT_FILE"
S2 = "DUPLICAT_PROCESSING"

MAX_FILES = 100
MAX_SIZE_MB = 50

_user_locks: dict = {}
_user_timers: dict = {}
_button_timers = _user_timers
_active_requests: dict = {}

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
        parts.append(f"<b>» BERKAS: {count} FILE «</b>" if count else "<b>» BERKAS «</b>")
    else:
        parts.append(f"Berkas: {count} file" if count else "Berkas ○")
        
    if step == 2:
        parts.append("<b>» BERSIHKAN «</b>")
    else:
        parts.append("Bersihkan ○")
        
    return " ➔ ".join(parts) + "\n\n"

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
            sess = db.get_session(user_id)
            if sess and sess.get("state") == STATE:
                data = sess["data"]
                jumlah = data["count"]
                
                text = _get_breadcrumbs(data, 1) + f"<b>{jumlah}</b> file diterima. Silakan pilih tindakan:"
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                        InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                    ]
                ])
                
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
                    except Exception:
                        pass
                
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                data["status_msg_id"] = msg.message_id
                db.set_session(user_id, STATE, data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error in duplikat: %s", e)

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
    duplikat_dir = os.path.join(user_dir, "duplikat")
    shutil.rmtree(duplikat_dir, ignore_errors=True)

async def cmd_duplikat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0})
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _get_breadcrumbs({"count": 0}, 1) + "Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])

async def handle_duplikat_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
        
    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".txt", ".vcf"):
        try:
            await update.message.delete()
        except Exception:
            pass
        return
        
    msg_id = update.message.message_id
    duplikat_dir = os.path.join(get_user_dir(user_id), "duplikat")
    os.makedirs(duplikat_dir, exist_ok=True)
    out_path = os.path.join(duplikat_dir, f"{msg_id}{ext}")
    
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
        logger.error("Download failed in duplikat: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

async def handle_show_duplikat_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0})
    
    try:
        await query.message.edit_text(
            text=_get_breadcrumbs({"count": 0}, 1) + "Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Duplikat akan dibersihkan.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, STATE, sess["data"])
    except Exception:
        # Fallback if editing fails
        try:
            await query.message.delete()
        except Exception:
            pass
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=_get_breadcrumbs({"count": 0}, 1) + "Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Duplikat akan dibersihkan.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])

async def handle_duplikat_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = db.get_session(update.effective_user.id)
    if not sess or sess["state"] != STATE:
        return
    data = sess["data"]
    if data["count"] == 0:
        return
        
    db.set_session(update.effective_user.id, S2, data)
    
    status_msg_id = data.get("status_msg_id")
    
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text="<b>Memproses...</b>",
            parse_mode="HTML"
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<b>Memproses...</b>",
            parse_mode="HTML"
        )
    
    await handle_duplikat_process(update, context)

async def handle_duplikat_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    data = sess["data"]
    status_msg_id = data.get("status_msg_id")
    
    user_dir = get_user_dir(user_id)
    duplikat_dir = os.path.join(user_dir, "duplikat")
    
    files = []
    if os.path.exists(duplikat_dir):
        files = [f for f in os.listdir(duplikat_dir) if (f.endswith('.txt') or f.endswith('.vcf'))]
        files.sort(key=lambda x: int(x.split('.')[0]))

    loop = asyncio.get_running_loop()

    try:
        def do_duplikat():
            results = []
            total_awal = 0
            total_unik = 0
            total_duplikat = 0
            
            for f in files:
                path = os.path.join(duplikat_dir, f)
                ext = os.path.splitext(f)[1].lower()
                try:
                    seen = set()
                    
                    if ext == ".vcf":
                        contacts = parse_vcf_file(path)
                        total_awal += len(contacts)
                        unique_contacts = []
                        for c in contacts:
                            num = c["tel"]
                            if num not in seen:
                                seen.add(num)
                                unique_contacts.append(c)
                        total_unik += len(unique_contacts)
                        content = contacts_to_vcf(unique_contacts).encode("utf-8")
                        results.append((f, content))
                    else:
                        with open(path, "r", encoding="utf-8-sig", errors="ignore") as fd:
                            lines = [line.strip() for line in fd if line.strip()]
                        total_awal += len(lines)
                        unique_numbers = []
                        for line in lines:
                            cleaned = add_plus(line)
                            if cleaned and cleaned not in seen:
                                seen.add(cleaned)
                                unique_numbers.append(cleaned)
                        total_unik += len(unique_numbers)
                        content = ("\n".join(unique_numbers) + "\n").encode("utf-8")
                        results.append((f, content))
                except Exception:
                    pass
            total_duplikat = total_awal - total_unik
            return results, total_awal, total_unik, total_duplikat

        results, total_awal, total_unik, total_duplikat = await loop.run_in_executor(None, do_duplikat)

        if not results:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Gagal. Data tidak ditemukan.",
                parse_mode="HTML"
            )
            _clear_buffers(user_id)
            db.clear_session(user_id)
            return

        # Kirim status mengirim
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<b>Mengirim file hasil...</b>",
            parse_mode="HTML"
        )

        # Kirim berkas-berkas hasil
        for f, content in results:
            buf = io.BytesIO(content)
            buf.name = f"CLEAN_{f.split('.')[0]}.{f.split('.')[-1]}"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=buf,
                filename=buf.name,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=60,
            )

        # Hapus status message
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass

        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_duplikat_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])

        total_file = len(files)

        box_text = (
            f"<pre><b>"
            f"┌────────────────────────────────────────┐\n"
            f"│             PROSES SELESAI             │\n"
            f"├────────────────────────────────────────┤\n"
            f"│ Total Berkas   : {_fit(f'{total_file} FILE'):<22} │\n"
            f"│ Total Awal     : {_fit(f'{total_awal:,}'):<22} │\n"
            f"│ Dihapus (Dup)  : {_fit(f'{total_duplikat:,}'):<22} │\n"
            f"│ Total Unik     : {_fit(f'{total_unik:,}'):<22} │\n"
            f"└────────────────────────────────────────┘"
            f"</b></pre>\n\n"
            f"<i>Pembersihan duplikat selesai!</i>"
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
        logger.error("Deduplication process failed: %s", e)
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Terjadi kesalahan saat memproses.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        _clear_buffers(user_id)
        db.clear_session(user_id)
        unregister_active_task(user_id)