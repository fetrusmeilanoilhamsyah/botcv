"""
cleanup.py — Pembersih dan standardisasi nomor HP secara otomatis (VCF ke VCF, TXT ke TXT).
Single-Message Morphing Wizard. Tanya file dulu, edit disatu pesan, hapus chat iseng/file user.
"""
import os
import io
import shutil
import asyncio
import logging
import re
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf

logger = logging.getLogger(__name__)

STATE = "CLEANUP_WAIT_FILE"
S2 = "CLEANUP_PROCESSING"

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
        parts.append(f"<b>» BERKAS: {count} FILE «</b>" if count else "<b>» BERKAS «</b>")
    else:
        parts.append(f"Berkas: {count} file" if count else "Berkas ○")
        
    if step == 2:
        parts.append("<b>» KIRIM «</b>")
    else:
        parts.append("Kirim ○")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ NUMBER CLEANUP CV ]</b>\n"
                f"{breadcrumbs}\n"
        "\n"
    )


def _clean_number(num: str) -> str:
    """Bersihkan nomor dari spasi, tanda hubung, kurung, dan simbol lainnya."""
    if not num:
        return ""
    
    num = num.strip()
    has_plus = num.startswith("+")
    digits = re.sub(r'\D', '', num)
    
    if not digits:
        return ""
        
    cleaned = ("+" if has_plus else "") + digits
    if 7 <= len(cleaned) <= 17:
        return cleaned
    return ""

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
                from handlers.start import register_welcome_messages
                register_welcome_messages(user_id, [msg.message_id])
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error in cleanup: %s", e)

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
    cleanup_dir = os.path.join(user_dir, "cleanup")
    shutil.rmtree(cleanup_dir, ignore_errors=True)

async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        _get_breadcrumbs({"count": 0}, 1) + "<b>[ ➔ ] Menunggu berkas...</b>\nKirim file <b>.TXT</b> atau <b>.VCF</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])

async def handle_cleanup_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    cleanup_dir = os.path.join(get_user_dir(user_id), "cleanup")
    os.makedirs(cleanup_dir, exist_ok=True)
    out_path = os.path.join(cleanup_dir, f"{msg_id}{ext}")
    
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
        logger.error("Download failed in cleanup: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

async def handle_show_cleanup_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0})

    from handlers.start import transition_to_handler
    msg = await transition_to_handler(
        context.bot,
        user_id,
        query.message.chat_id,
        _get_breadcrumbs({"count": 0}, 1) + "<b>[ ➔ ] Menunggu berkas...</b>\nKirim file <b>.TXT</b> atau <b>.VCF</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
    )
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])

async def handle_cleanup_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = db.get_session(update.effective_user.id)
    if not sess or sess["state"] != STATE:
        return
    data = sess["data"]
    if data["count"] == 0:
        return

    db.set_session(update.effective_user.id, S2, data)
    status_msg_id = data.get("status_msg_id")

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        try:
            await update.callback_query.message.edit_text(
                text="<b>Memproses...</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="<b>Memproses...</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await handle_cleanup_process(update, context)

async def handle_cleanup_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    data = sess["data"]
    status_msg_id = data.get("status_msg_id")
    
    user_dir = get_user_dir(user_id)
    cleanup_dir = os.path.join(user_dir, "cleanup")
    
    files = []
    if os.path.exists(cleanup_dir):
        files = [f for f in os.listdir(cleanup_dir) if (f.endswith('.txt') or f.endswith('.vcf'))]
        files.sort(key=lambda x: int(x.split('.')[0]))

    loop = asyncio.get_running_loop()

    try:
        def do_cleanup():
            results = []
            total_awal = 0
            total_clean = 0
            total_dibuang = 0
            
            for f in files:
                path = os.path.join(cleanup_dir, f)
                ext = os.path.splitext(f)[1].lower()
                
                try:
                    clean_contacts = []
                    seen = set()
                    
                    if ext == ".vcf":
                        parsed = parse_vcf_file(path)
                        total_awal += len(parsed)
                        for c in parsed:
                            clean = _clean_number(c["tel"])
                            if clean and clean not in seen:
                                seen.add(clean)
                                clean_contacts.append({"name": c["name"], "tel": clean})
                        
                        content = contacts_to_vcf(clean_contacts).encode("utf-8")
                        results.append((f, content))
                        total_clean += len(seen)
                    else:
                        with open(path, "r", encoding="utf-8", errors="ignore") as fd:
                            lines = [line.strip() for line in fd if line.strip()]
                        
                        phone_re = re.compile(r'\+?(?:\d[\s\-\(\)\.]*){8,16}')
                        raw_numbers = []
                        for line in lines:
                            matches = phone_re.findall(line)
                            raw_numbers.extend(matches)
                        
                        total_awal += len(raw_numbers)
                        clean_numbers = []
                        for num in raw_numbers:
                            clean = _clean_number(num)
                            if clean and clean not in seen:
                                seen.add(clean)
                                clean_numbers.append(clean)
                        
                        content = ("\n".join(clean_numbers) + "\n").encode("utf-8")
                        results.append((f, content))
                        total_clean += len(seen)
                except Exception:
                    pass
            total_dibuang = total_awal - total_clean
            return results, total_awal, total_clean, total_dibuang

        results, total_awal, total_clean, total_dibuang = await loop.run_in_executor(None, do_cleanup)

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
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_cleanup_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])

        box_text = (
            f"<b>[ PROSES SELESAI ]</b>\n"
            f"<blockquote>"
            f"• Total Berkas : {len(files)} FILE\n"
            f"• Total Awal : {total_awal:,}\n"
            f"• Valid & Unik : {total_clean:,}\n"
            f"• Dibuang : {total_dibuang:,}</blockquote>\n\n"
            f"<i>Pembersihan & standardisasi selesai!</i>"
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
        logger.error("Cleanup process failed: %s", e)
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
