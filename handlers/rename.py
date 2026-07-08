"""
rename.py — Ubah nama kontak di dalam file VCF.
Single-Message Morphing Wizard. Tanya file dulu, edit disatu pesan, hapus chat iseng/file user.
"""
import os
import io
import shutil
import asyncio
import logging
import zipfile
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf
from core.utils import sanitize_filename
from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
    SEND_MAX_RETRIES,
    SEND_RETRY_DELAY,
    FILE_READ_TIMEOUT,
    FILE_WRITE_TIMEOUT,
    FILE_CONNECT_TIMEOUT,
    SEND_BATCH_SIZE,
    SEND_BATCH_DELAY,
    SEND_FILE_DELAY,
)

logger = logging.getLogger(__name__)

S0 = "RENAME_WAIT_FILE"
S1 = "RENAME_WAIT_CONTACT_NAME"
S2 = "RENAME_WAIT_FILE_NAME"
S3 = "RENAME_WAIT_START_NUM"
S4 = "RENAME_WAIT_NUM_STYLE"
S5 = "RENAME_WAIT_DELIVERY"
S6 = "RENAME_PROCESSING"

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    contact_name = data.get("contact_name", "")
    file_name = data.get("file_name", "")
    start_num = data.get("start_num", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS: {count} FILE]</b>" if count else "<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count}</code>" if count else "Berkas: ➖")
        
    if step == 2:
        parts.append(f"<b>[KONTAK: {contact_name.upper()}]</b>" if contact_name else "<b>[NAMA KONTAK]</b>")
    elif step > 2 and contact_name:
        parts.append(f"Kontak: <code>{contact_name}</code>")
    else:
        parts.append("Kontak: ➖")
        
    if step == 3:
        parts.append(f"<b>[FILE: {file_name.upper()}]</b>" if file_name else "<b>[NAMA FILE]</b>")
    elif step > 3 and file_name:
        parts.append(f"File: <code>{file_name}</code>")
    else:
        parts.append("File: ➖")
        
    if step == 4:
        parts.append(f"<b>[URUTAN: {start_num}]</b>" if start_num else "<b>[NOMOR AWAL]</b>")
    elif step > 4 and start_num:
        parts.append(f"Urutan: <code>{start_num}</code>")
    else:
        parts.append("Urutan: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ VCF RENAME CONSOLE ]</b>\n"
                f"<blockquote>{breadcrumbs}</blockquote>\n"
        "\n"
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

_user_locks: dict = {}
_user_timers: dict = {}

def _get_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())

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
            if sess and sess.get("state") == S0:
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
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                        return
                    except Exception:
                        pass
                
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                data["status_msg_id"] = msg.message_id
                db.set_session(user_id, S0, data)
                from handlers.start import register_welcome_messages
                register_welcome_messages(user_id, [msg.message_id])
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
    db.set_session(user_id, S0, init_data)
    
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
        db.set_session(user_id, S0, sess["data"])

async def handle_rename_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S0:
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
                        f"<code>{sent_name}</code> bukan berkas VCF (<code>.vcf</code>).</blockquote>"
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
            if not sess or sess["state"] != S0:
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
            db.set_session(user_id, S0, data)

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
    db.set_session(user_id, S0, init_data)
    text = _waiting_text(init_data)
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, S0, sess["data"])
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
        db.set_session(user_id, S0, sess["data"])

async def handle_rename_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)
    
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S0:
        return
    data = sess["data"]
    if data["count"] == 0:
        return
        
    db.set_session(user_id, S1, data)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    
    status_msg_id = data.get("status_msg_id")
    
    text = _get_breadcrumbs(data, 2) + "<blockquote><b>[ LANGKAH 2: INPUT NAMA KONTAK BARU ]</b>\nKetik nama kontak baru Anda (Contoh: <code>FEE</code>):</blockquote>"
    
    # Edit the message in-place
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text=text,
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
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
            
    db.set_session(user_id, S1, data)

async def handle_rename_contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
    
    data = sess["data"]
    data["contact_name"] = update.message.text.strip()
    
    try:
        await update.message.delete()
    except Exception:
        pass
        
    db.set_session(user_id, S2, data)
    status_msg_id = data.get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    
    if status_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(data, 3) + "<blockquote><b>[ LANGKAH 3: INPUT NAMA FILE BARU ]</b>\nKetik nama file baru Anda (Contoh: <code>CONTOH</code>):</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass

async def handle_rename_file_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S2:
        return
    
    data = sess["data"]
    data["file_name"] = sanitize_filename(update.message.text.strip())
    
    try:
        await update.message.delete()
    except Exception:
        pass
        
    db.set_session(user_id, S3, data)
    status_msg_id = data.get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    
    if status_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(data, 4) + "<blockquote><b>[ LANGKAH 4: INPUT NOMOR URUT AWAL ]</b>\nKetik nomor urut file awal (Contoh: <code>1</code>):</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass

async def handle_rename_start_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S3:
        return
    
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
        
    status_msg_id = sess["data"].get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not text.isdigit() or int(text) < 1:
        if status_msg_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(sess["data"], 4) + "<blockquote>⚠️ <b>Harap masukkan angka valid (minimal 1).</b>\n\nNomor urut awal? Contoh: <b>1</b></blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        return
        
    data = sess["data"]
    data["start_num"] = int(text)
    db.set_session(user_id, S4, data)
    
    file_name = data.get("file_name", "CONTOH")
    style_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"NOMOR DI AWAL (e.g. 1 {file_name})", callback_data="rename_numstyle_front", style="primary"),
            InlineKeyboardButton(f"NOMOR DI AKHIR (e.g. {file_name} 1)", callback_data="rename_numstyle_back", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    if status_msg_id:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(data, 4) + "<blockquote><b>[ LANGKAH 5: PILIH FORMAT PENOMORAN FILE ]</b>\nPilih format penomoran nama file:</blockquote>",
            parse_mode="HTML",
            reply_markup=style_keyboard
        )

async def handle_rename_numstyle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S4:
        return
        
    data = sess["data"]
    num_style = "front" if query.data == "rename_numstyle_front" else "back"
    data["file_num_style"] = num_style
    db.set_session(user_id, S5, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="rename_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="rename_deliv_zip", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await query.edit_message_text(
        text=_get_breadcrumbs(data, 4) + "<blockquote><b>[ LANGKAH 6: PILIH FORMAT PENGIRIMAN ]</b>\nPilih format pengiriman file VCF:</blockquote>",
        parse_mode="HTML",
        reply_markup=deliv_keyboard
    )

async def handle_rename_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S5:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "rename_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    db.set_session(user_id, S6, data)
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

    contact_name = data["contact_name"]
    file_name = data["file_name"]
    start_num = data["start_num"]
    num_style = data.get("file_num_style", "back")
    delivery_mode = data.get("delivery_mode", "single")

    loop = asyncio.get_running_loop()

    process_text = "<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memproses rename file VCF...</blockquote>"
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=process_text,
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        def do_rename():
            results = []
            contact_counter = 1
            file_counter = start_num
            
            for f in files:
                path = os.path.join(rename_dir, f)
                try:
                    contacts = parse_vcf_file(path)
                    renamed = []
                    for c in contacts:
                        renamed.append({"name": f"{contact_name} {contact_counter}", "tel": c["tel"]})
                        contact_counter += 1
                    
                    vcf_content = contacts_to_vcf(renamed)
                    
                    if num_style == "front":
                        label = f"{file_counter} {file_name}"
                    else:
                        label = f"{file_name} {file_counter}"
                        
                    results.append((label, vcf_content.encode("utf-8")))
                    file_counter += 1
                except Exception:
                    pass
            return results, contact_counter - 1, file_counter - start_num

        results, total_contacts, total_files = await loop.run_in_executor(None, do_rename)

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

        if delivery_mode == "zip":
            # Compress to ZIP
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote><b>[ SYSTEM: COMPRESSING ]</b>\nMengompresi file ke format ZIP...</blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for label, content in results:
                    zip_file.writestr(f"{label}.vcf", content)
            zip_buffer.seek(0)
            zip_filename = f"{file_name}.zip"

            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote><b>[ SYSTEM: SENDING FILES ]</b>\nSedang mengirim file ZIP...</blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            for attempt in range(SEND_MAX_RETRIES):
                try:
                    zip_buffer.seek(0)
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=zip_buffer,
                        filename=zip_filename,
                        read_timeout=FILE_READ_TIMEOUT,
                        write_timeout=FILE_WRITE_TIMEOUT,
                        connect_timeout=FILE_CONNECT_TIMEOUT,
                    )
                    break
                except Exception as ex:
                    logger.error(f"[Rename] Gagal kirim ZIP attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        raise
                    else:
                        await asyncio.sleep(SEND_RETRY_DELAY)

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
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {len(files)} VCF\n"
                f"• Berkas Output : {total_files} VCF (ZIP)\n"
                f"• Format Kontak : {contact_name}\n"
                f"• Format File : {file_name}\n"
                f"• Total Kontak : {total_contacts:,}</blockquote>\n\n"
                f"<i>Silakan unduh file ZIP di atas.</i>"
            )
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

        else:
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote><b>[ SYSTEM: SENDING FILES ]</b>\nSedang mengirim file VCF hasil...</blockquote>",
                    parse_mode="HTML"
                )

            sent_count = 0
            for label, content in results:
                buf = io.BytesIO(content)
                buf.name = f"{label}.vcf"
                
                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        buf.seek(0)
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=buf,
                            filename=buf.name,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=60,
                        )
                        sent_count += 1
                        break
                    except RetryAfter as e:
                        wait_secs = max(int(e.retry_after), 2) + 1
                        await asyncio.sleep(wait_secs)
                    except Exception as ex:
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += 1
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)

                if sent_count < total_files:
                    if sent_count % SEND_BATCH_SIZE == 0:
                        await asyncio.sleep(SEND_BATCH_DELAY)
                    else:
                        await asyncio.sleep(SEND_FILE_DELAY)

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
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {len(files)} VCF\n"
                f"• Berkas Output : {total_files} VCF\n"
                f"• Format Kontak : {contact_name}\n"
                f"• Format File : {file_name}\n"
                f"• Total Kontak : {total_contacts:,}</blockquote>\n\n"
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