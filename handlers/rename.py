"""
rename.py — Ubah nama kontak di dalam file VCF.
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
from core.vcf_parser import parse_vcf_file, contacts_to_vcf

logger = logging.getLogger(__name__)

STATE_FILE = "RENAME_WAIT_FILE"
STATE_NAME = "RENAME_WAIT_NAME"
S2 = "RENAME_PROCESSING"

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
    base_name = data.get("base_name", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS: {count} FILE]</b>" if count else "<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count}</code>" if count else "Berkas: ➖")
        
    if step == 2:
        parts.append(f"<b>[NAMA: {base_name.upper()}]</b>" if base_name else "<b>[NAMA KONTAK]</b>")
    elif step > 2 and base_name:
        parts.append(f"Nama: <code>{base_name}</code>")
    else:
        parts.append("Nama: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ VCF RENAME CONSOLE ]</b>\n"
        "────────────────────────────\n"
        f"<blockquote>{breadcrumbs}</blockquote>\n"
        "────────────────────────────\n\n"
    )

def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.vcf</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"\u2022 Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"\u2022 Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file</blockquote>"
    )

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
            if sess and sess.get("state") == STATE_FILE:
                data = sess["data"]
                jumlah = data["count"]
                
                text = (
                    _get_breadcrumbs(data, 1) +
                    f"<blockquote><b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                    f"Berhasil mengunduh <code>{jumlah}</code> berkas VCF.\n\n"
                    f"Silakan pilih tindakan di bawah:</blockquote>"
                )
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
                db.set_session(user_id, STATE_FILE, data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error in rename: %s", e)

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
    rename_dir = os.path.join(user_dir, "rename")
    shutil.rmtree(rename_dir, ignore_errors=True)

async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    init_data = {"count": 0, "total_size": 0}
    db.set_session(user_id, STATE_FILE, init_data)
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _waiting_text(init_data),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE_FILE, sess["data"])

async def handle_rename_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_FILE:
        return

    doc = update.message.document
    ext = os.path.splitext(doc.file_name)[1].lower() if doc and doc.file_name else ""
    if not doc or not doc.file_name or ext != ".vcf":
        # Format salah — edit status message in-place, JANGAN hapus file user
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            sent_name = doc.file_name if doc and doc.file_name else "file tersebut"
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        f"<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{sent_name}</code> bukan berkas <code>.vcf</code>.</blockquote>"
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
    rename_dir = os.path.join(get_user_dir(user_id), "rename")
    os.makedirs(rename_dir, exist_ok=True)
    out_path = os.path.join(rename_dir, f"{msg_id}.vcf")
    
    try:
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive(out_path)

        async with _get_lock(user_id):
            sess = db.get_session(user_id)
            if not sess or sess["state"] != STATE_FILE:
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
            db.set_session(user_id, STATE_FILE, data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        logger.error("Download failed in rename: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

async def handle_show_rename_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    init_data = {"count": 0, "total_size": 0}
    db.set_session(user_id, STATE_FILE, init_data)
    text = _waiting_text(init_data)
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, STATE_FILE, sess["data"])
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
        db.set_session(user_id, STATE_FILE, sess["data"])

async def handle_rename_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = db.get_session(update.effective_user.id)
    if not sess or sess["state"] != STATE_FILE:
        return
    data = sess["data"]
    if data["count"] == 0:
        return
        
    db.set_session(update.effective_user.id, STATE_NAME, data)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    
    status_msg_id = data.get("status_msg_id")
    
    # Edit the message in-place
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text=_get_breadcrumbs(data, 2) + "<blockquote><b>[ LANGKAH 2: INPUT NAMA KONTAK ]</b>\nKetik nama kontak baru Anda (Contoh: <code>FEE</code>):</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            data["status_msg_id"] = update.callback_query.message.message_id
        except Exception:
            pass
    elif status_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(data, 2) + "<blockquote><b>[ LANGKAH 2: INPUT NAMA KONTAK ]</b>\nKetik nama kontak baru Anda (Contoh: <code>FEE</code>):</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
            
    db.set_session(update.effective_user.id, STATE_NAME, data)

async def handle_rename_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_NAME:
        return
    
    data = sess["data"]
    data["base_name"] = update.message.text.strip()
    
    try:
        await update.message.delete()
    except Exception:
        pass
        
    db.set_session(user_id, S2, data)
    
    status_msg_id = data.get("status_msg_id")
    process_text = "<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memproses rename file VCF...</blockquote>"
    
    if status_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=process_text,
                parse_mode="HTML"
            )
        except Exception:
            pass
    
    await handle_rename_process(update, context)

async def handle_rename_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    data = sess["data"]
    status_msg_id = data.get("status_msg_id")
    
    user_dir = get_user_dir(user_id)
    rename_dir = os.path.join(user_dir, "rename")
    
    files = []
    if os.path.exists(rename_dir):
        files = [f for f in os.listdir(rename_dir) if f.endswith('.vcf')]
        files.sort(key=lambda x: int(x.split('.')[0]))

    base_name = data["base_name"]
    loop = asyncio.get_running_loop()

    try:
        def do_rename():
            results = []
            contact_counter = 1
            
            for f in files:
                path = os.path.join(rename_dir, f)
                try:
                    contacts = parse_vcf_file(path)
                    renamed = []
                    for c in contacts:
                        renamed.append({"name": f"{base_name} {contact_counter}", "tel": c["tel"]})
                        contact_counter += 1
                    
                    vcf_content = contacts_to_vcf(renamed)
                    results.append((f, vcf_content.encode("utf-8")))
                except Exception:
                    pass
            return results, contact_counter - 1

        results, total_contacts = await loop.run_in_executor(None, do_rename)

        if not results:
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Gagal. Data tidak ditemukan.</b></blockquote>",
                    parse_mode="HTML"
                )
            _clear_buffers(user_id)
            db.clear_session(user_id)
            return

        # Kirim status mengirim
        if status_msg_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="<blockquote><b>[ SYSTEM: SENDING FILES ]</b>\nSedang mengirim file VCF hasil...</blockquote>",
                parse_mode="HTML"
            )

        # Kirim berkas-berkas hasil rename
        for f, content in results:
            buf = io.BytesIO(content)
            buf.name = f"CLEAN_{f.split('.')[0]}.vcf"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=buf,
                filename=buf.name,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=60,
            )

        # Hapus status message
        if status_msg_id:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_rename_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])

        box_text = (
            f"<pre><b>"
            f"┌────────────────────────────────────────┐\n"
            f"│             PROSES SELESAI             │\n"
            f"├────────────────────────────────────────┤\n"
            f"│ Total Berkas   : {_fit(f'{len(files)} VCF'):<22} │\n"
            f"│ Total Kontak   : {_fit(f'{total_contacts:,}'):<22} │\n"
            f"│ Format Nama    : {_fit(base_name):<22} │\n"
            f"│ Range Urutan   : {_fit(f'1 - {total_contacts}'):<22} │\n"
            f"└────────────────────────────────────────┘"
            f"</b></pre>\n\n"
            f"<i>Rename selesai! Silakan unduh file di atas.</i>"
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
        logger.error("Rename process failed: %s", e)
        if status_msg_id:
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