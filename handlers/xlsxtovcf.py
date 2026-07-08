"""
xlsxtovcf.py — Disk-based, terima paralel, sort by message_id, tampilkan daftar file dulu, lalu kirim semua.
UI/UX Level Dewa: Single-Message Morphing Wizard.
"""
import os
import re
import csv
import shutil
import asyncio
import logging
import io
import zipfile
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import add_plus, contacts_to_vcf
from core.utils import sanitize_filename

logger = logging.getLogger(__name__)

S0 = "XTV_WAIT_FILE"
S1 = "XTV_CONTACT_NAME"
S2 = "XTV_PER_FILE"
S3 = "XTV_FILE_NAME"
S4 = "XTV_AWALAN"
S5 = "XTV_PROCESSING"
S6 = "XTV_DELIVERY"

from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
    MAX_CONTACTS_PER_FILE,
    THREAD_POOL_TIMEOUT,
    SEND_PROGRESS_INTERVAL,
    SEND_MAX_RETRIES,
    SEND_RETRY_DELAY,
    FILE_READ_TIMEOUT,
    FILE_WRITE_TIMEOUT,
    FILE_CONNECT_TIMEOUT,
    SEND_BATCH_SIZE,
    SEND_BATCH_DELAY,
    SEND_FILE_DELAY,
)

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    contact_name = data.get("contact_name", "")
    per_file = data.get("per_file", "")
    file_name = data.get("file_name", "")
    awalan = data.get("awalan", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS: {count} FILE]</b>" if count else "<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count}</code>" if count else "Berkas: ➖")
        
    if step == 2:
        parts.append(f"<b>[NAMA: {contact_name.upper()}]</b>" if contact_name else "<b>[NAMA KONTAK]</b>")
    elif step > 2 and contact_name:
        parts.append(f"Nama: <code>{contact_name}</code>")
    else:
        parts.append("Nama: ➖")
        
    if step == 3:
        parts.append(f"<b>[JUMLAH: {per_file}]</b>" if per_file else "<b>[JUMLAH]</b>")
    elif step > 3 and per_file:
        parts.append(f"Jumlah: <code>{per_file}</code>")
    else:
        parts.append("Jumlah: ➖")
        
    if step == 4:
        parts.append(f"<b>[FILE: {file_name.upper()}]</b>" if file_name else "<b>[NAMA FILE]</b>")
    elif step > 4 and file_name:
        parts.append(f"File: <code>{file_name}</code>")
    else:
        parts.append("File: ➖")
        
    num_style = data.get("file_num_style", "")
    style_suffix = ""
    if num_style == "front":
        style_suffix = " (AWAL)"
    elif num_style == "back":
        style_suffix = " (AKHIR)"
        
    if step == 5:
        parts.append(f"<b>[URUTAN: {awalan}{style_suffix}]</b>" if awalan else "<b>[URUTAN]</b>")
    elif step > 5 and awalan:
        parts.append(f"Urutan: <code>{awalan}{style_suffix}</code>")
    else:
        parts.append("Urutan: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ EXCEL/CSV ➔ VCF CONSOLE ]</b>\n"
                f"<blockquote>{breadcrumbs}</blockquote>\n"
        "\n"
    )


def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.xlsx</code> atau <code>.csv</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"\u2022 Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"\u2022 Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file</blockquote>"
    )


_user_locks: dict = {}
_user_timers: dict = {}

PHONE_REGEX = re.compile(r'\+?(?:\d[\s\-\(\)\.]*){8,16}')


def get_user_lock(user_id: int) -> asyncio.Lock:
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
            from handlers.start import _get_transition_lock, register_welcome_messages, _welcome_messages
            async with _get_transition_lock(user_id):
                sess = db.get_session(user_id)
                if sess and sess.get("state") in [S0]:
                    data = sess["data"]
                    jumlah = data["count"]
                    
                    text = (
                        _get_breadcrumbs(data, 1) +
                        f"<blockquote><b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                        f"Berhasil mengunduh <code>{jumlah}</code> berkas Excel/CSV.\n\n"
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
                    db.set_session(user_id, sess["state"], data)
                    register_welcome_messages(user_id, [msg.message_id])
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error in xlsxtovcf: %s", e)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error in xlsxtovcf: %s", e)


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
    xtv_dir = os.path.join(user_dir, "xlsxtovcf")
    shutil.rmtree(xtv_dir, ignore_errors=True)


def _extract_numbers_sync(filepath: str, ext: str) -> list:
    numbers = []
    seen = set()
    try:
        def process_cell(cell_value):
            if cell_value is None:
                return
            if isinstance(cell_value, float):
                if cell_value.is_integer():
                    cell_value = str(int(cell_value))
                else:
                    cell_value = str(cell_value)
            else:
                cell_value = str(cell_value).strip()
            
            cleaned = re.sub(r'[\s\-\(\)\.]', '', cell_value)
            if cleaned.isdigit() and len(cleaned) >= 8 and len(cleaned) <= 16:
                standardized = add_plus(cleaned)
                if standardized not in seen:
                    seen.add(standardized)
                    numbers.append(standardized)

        if ext == ".csv":
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                for row in reader:
                    for val in row:
                        process_cell(val)
        else:
            # Excel
            import openpyxl
            wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    for val in row:
                        process_cell(val)
    except Exception as e:
        logger.error("Error extract numbers sync: %s", e)
    return numbers


async def cmd_xlsxtovcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    init_data = {"count": 0, "total_size": 0, "total_contacts": 0}
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
        # FIX Bug#3: pakai init_data langsung, tidak ambil ulang dari db setelah await
        init_data["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, init_data)


async def handle_xtv_contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S1:
        return
    data = sess["data"]
    data["contact_name"] = update.message.text.strip()
    db.set_session(user_id, S1, data)
    
    try:
        await update.message.delete()
    except Exception:
        pass

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("STANDAR", callback_data="xtv_style_standard", style="primary"),
            InlineKeyboardButton("DENGAN TANGGAL", callback_data="xtv_style_date", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    status_msg_id = data.get("status_msg_id")
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=_get_breadcrumbs(data, 2) + f"Pilih format penamaan untuk kontak <b>{data['contact_name']}</b>:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_xtv_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    data = sess["data"]
    style = "standard" if query.data == "xtv_style_standard" else "date"
    data["naming_style"] = style
    db.set_session(user_id, S2, data)
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    await query.edit_message_text(
        text=_get_breadcrumbs(data, 3) + "Berapa kontak per file? Contoh: <b>100</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_xtv_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S2:
        return
    
    text = update.message.text.strip()
    
    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg_id = sess["data"].get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not text.isdigit():
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(sess["data"], 3) + "⚠️ Harap masukkan angka saja.\n\nBerapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
    
    per_file = int(text)
    if per_file < 1 or per_file > MAX_CONTACTS_PER_FILE:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(sess["data"], 3) + f"⚠️ Harap masukkan angka antara 1 sampai {MAX_CONTACTS_PER_FILE:,}.\n\nBerapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return

    data = sess["data"]
    data["per_file"] = per_file
    db.set_session(user_id, S3, data)
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=_get_breadcrumbs(data, 4) + "Nama file? Contoh: <b>FEE</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_xtv_file_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S3:
        return
    
    try:
        await update.message.delete()
    except Exception:
        pass

    data = sess["data"]
    data["file_name"] = sanitize_filename(update.message.text.strip())
    db.set_session(user_id, S4, data)
    
    status_msg_id = data.get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=_get_breadcrumbs(data, 5) + "Nomor urut awal? Contoh: <b>1</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_xtv_awalan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S4:
        return
        
    text = update.message.text.strip()
    
    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg_id = sess["data"].get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not text.isdigit() or int(text) < 1:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(sess["data"], 5) + "⚠️ Harap masukkan angka valid (minimal 1).\n\nNomor urut awal? Contoh: <b>1</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
        
    data = sess["data"]
    data["awalan"] = int(text)
    db.set_session(user_id, S6, data)
    
    file_name = data.get("file_name", "FEE")
    style_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"NOMOR DI AWAL (e.g. 1 {file_name})", callback_data="xtv_numstyle_front", style="primary"),
            InlineKeyboardButton(f"NOMOR DI AKHIR (e.g. {file_name} 1)", callback_data="xtv_numstyle_back", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=_get_breadcrumbs(data, 5) + "Pilih format penomoran nama file:",
        parse_mode="HTML",
        reply_markup=style_keyboard
    )

async def handle_xtv_numstyle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih format penomoran nama file"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S6:
        return
        
    data = sess["data"]
    num_style = "front" if query.data == "xtv_numstyle_front" else "back"
    data["file_num_style"] = num_style
    db.set_session(user_id, S6, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="xtv_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="xtv_deliv_zip", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await query.edit_message_text(
        text=_get_breadcrumbs(data, 6) + "Pilih format pengiriman file VCF:",
        parse_mode="HTML",
        reply_markup=deliv_keyboard
    )


async def handle_xtv_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S6:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "xtv_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    db.set_session(user_id, S5, data)
    await handle_xtv_process(update, context)


async def handle_xtv_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_xtv_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if sess["state"] not in [S0]:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        return
    
    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".xlsx", ".csv"):
        # Format salah — edit status in-place, JANGAN hapus file user
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        f"<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{doc.file_name}</code> bukan berkas <code>.xlsx</code> atau <code>.csv</code>.</blockquote>"
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

    # Download ke disk
    file_obj = await context.bot.get_file(doc.file_id)
    xtv_dir = os.path.join(get_user_dir(user_id), "xlsxtovcf")
    os.makedirs(xtv_dir, exist_ok=True)
    out_path = os.path.join(xtv_dir, f"{msg_id}{ext}")
    
    try:
        await file_obj.download_to_drive(out_path)

        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if sess["state"] not in [S0]:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            data = sess["data"]
            if data.get("is_processing") or data.get("is_processing_final"):
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if data["count"] >= MAX_FILES:
                await update.message.reply_text(f"Batas <b>{MAX_FILES}</b> file. Silakan ketik selesai atau klik PROSES SEKARANG.")
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                await update.message.reply_text(f"Batas <b>{MAX_SIZE_MB}MB</b>. Silakan ketik selesai atau klik PROSES SEKARANG.")
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            data["count"] += 1
            data["total_size"] += doc.file_size
            db.set_session(user_id, sess["state"], data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        logger.error("Download failed in xlsxtovcf: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


async def handle_xtv_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if not sess:
        return
    data = sess["data"]

    # Hapus input teks 'done' jika user mengetik manual
    if update.message and update.message.text in ("done", "selesai", "/done"):
        try:
            await update.message.delete()
        except Exception:
            pass

    if sess["state"] == S0:
        if data["count"] == 0:
            return
        
        db.set_session(user_id, S1, data)
        
        status_msg_id = data.get("status_msg_id")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(data, 2) + f"<b>{data['count']}</b> file Excel/CSV diterima. Nama kontak? Contoh: <b>FEE</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )


async def handle_xtv_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    data = sess["data"]
    
    if data.get("is_processing_final"):
        return
    data["is_processing_final"] = True
    db.set_session(user_id, sess["state"], data)

    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())
    
    status_msg_id = data.get("status_msg_id")
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang mengekstrak dan memproses data...</blockquote>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    user_dir = get_user_dir(user_id)
    xtv_dir = os.path.join(user_dir, "xlsxtovcf")
    
    files = []
    if os.path.exists(xtv_dir):
        files = [f for f in os.listdir(xtv_dir) if f.endswith(('.xlsx', '.csv'))]
        files.sort(key=lambda x: int(os.path.splitext(x)[0]))

    contact_name = data["contact_name"]
    file_name    = data["file_name"]
    per_file     = data["per_file"]
    awalan       = data["awalan"]

    loop = asyncio.get_running_loop()

    def do_build():
        all_numbers = []
        seen_global = set()
        for f in files:
            path = os.path.join(xtv_dir, f)
            ext = os.path.splitext(f)[1].lower()
            try:
                found = _extract_numbers_sync(path, ext)
                for num in found:
                    if num not in seen_global:
                        seen_global.add(num)
                        all_numbers.append(num)
            except Exception:
                pass

        results = []
        contact_counter = 1
        file_counter = awalan
        
        from datetime import datetime, timezone, timedelta
        jakarta_tz = timezone(timedelta(hours=7))
        today_str = datetime.now(jakarta_tz).strftime("%d/%m")
        
        naming_style = data.get("naming_style", "standard")

        for i in range(0, len(all_numbers), per_file):
            chunk = all_numbers[i:i + per_file]
            contacts = []
            for j, num in enumerate(chunk):
                idx_num = contact_counter + j
                if naming_style == "date":
                    c_name = f"{contact_name} {today_str} {idx_num}"
                else:
                    c_name = f"{contact_name}{idx_num}"
                contacts.append({"name": c_name, "tel": num})
            contact_counter += len(chunk)
            num_style = data.get("file_num_style", "back")
            if num_style == "front":
                label = f"{file_counter} {file_name}"
            else:
                label = f"{file_name} {file_counter}"
            content = contacts_to_vcf(contacts).encode("utf-8")
            results.append((label, content))
            file_counter += 1
            
        return all_numbers, results

    try:
        all_numbers, results = await loop.run_in_executor(None, do_build)

        if not results:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="Gagal. Data tidak ditemukan.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        total_files = len(results)
        delivery_mode = data.get("delivery_mode", "single")

        if delivery_mode == "zip":
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
                    logger.error(f"[XTV] Gagal kirim ZIP attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        raise
                    else:
                        await asyncio.sleep(SEND_RETRY_DELAY)

            # Hapus status message lama agar laporan sukses berada di paling bawah
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_xlsxtovcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            total_contacts_str = f"{len(all_numbers):,}"
            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {len(files)} XLSX/CSV\n"
                f"• Berkas Output : {total_files} VCF (ZIP)\n"
                f"• Nama Kontak : {contact_name}\n"
                f"• Jumlah / File : {per_file}\n"
                f"• Nama File VCF : {file_name}\n"
                f"• Urutan Mulai : {f'{awalan} (AWAL)' if data.get('file_num_style') == 'front' else f'{awalan} (AKHIR)'}\n"
                f"• Total Kontak : {total_contacts_str}</blockquote>\n\n"
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
            # Mode "single" menggunakan sequential sending
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote><b>[ SYSTEM: SENDING FILES ]</b>\nSedang mengirim file VCF satu per satu...</blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            sent_count = 0
            from telegram.error import RetryAfter
            from config import SEND_PROGRESS_INTERVAL, SEND_BATCH_SIZE

            for idx, (label, content) in enumerate(results):
                buf = io.BytesIO(content)
                buf.name = f"{label}.vcf"

                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        buf.seek(0)
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=buf,
                            filename=f"{label}.vcf",
                            read_timeout=FILE_READ_TIMEOUT,
                            write_timeout=FILE_WRITE_TIMEOUT,
                            connect_timeout=FILE_CONNECT_TIMEOUT,
                        )
                        sent_count += 1
                        break
                    except RetryAfter as e:
                        wait_secs = max(int(e.retry_after), 2) + 1
                        logger.warning(f"[XTV] Flood limit sekuensial, tunggu {wait_secs}s")
                        await asyncio.sleep(wait_secs)
                    except Exception as ex:
                        logger.error(f"[XTV] Gagal kirim file VCF sekuensial {label} attempt {attempt+1}: {ex}")
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += 1
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)



                if sent_count < total_files:
                    if sent_count % SEND_BATCH_SIZE == 0:
                        await asyncio.sleep(SEND_BATCH_DELAY)
                    else:
                        await asyncio.sleep(SEND_FILE_DELAY)

            # Hapus status message lama
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_xlsxtovcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            total_contacts_str = f"{len(all_numbers):,}"
            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {len(files)} XLSX/CSV\n"
                f"• Berkas Output : {total_files} VCF\n"
                f"• Nama Kontak : {contact_name}\n"
                f"• Jumlah / File : {per_file}\n"
                f"• Nama File VCF : {file_name}\n"
                f"• Urutan Mulai : {f'{awalan} (AWAL)' if data.get('file_num_style') == 'front' else f'{awalan} (AKHIR)'}\n"
                f"• Total Kontak : {total_contacts_str}</blockquote>\n\n"
                f"<i>Silakan unduh file VCF di atas.</i>"
            )
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)


async def handle_show_xlsxtovcf_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Excel/CSV to VCF)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    init_data = {"count": 0, "total_size": 0, "total_contacts": 0}
    db.set_session(user_id, S0, init_data)
    text = _waiting_text(init_data)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup
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
            reply_markup=markup,
            parse_mode="HTML"
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])
