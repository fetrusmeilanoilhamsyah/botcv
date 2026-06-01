"""
xlsxtovcf.py — Disk-based, terima paralel, sort by message_id, tampilkan daftar file dulu, lalu kirim semua.
Sama persis konsepnya dengan txttovcf.py, namun untuk file Excel (.xlsx) dan CSV (.csv).
"""
import os
import re
import csv
import shutil
import asyncio
import logging
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
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
S5 = "XTV_COLLECTING"

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
            sess = db.get_session(user_id)
            if sess and sess.get("state") in [S0, S5]:
                data = sess["data"]
                jumlah = data["count"]
                
                # Hapus welcome messages lama (hanya sekali saat file pertama masuk)
                from handlers.start import _welcome_messages
                welcome_ids = _welcome_messages.pop(user_id, [])
                for w_id in welcome_ids:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=w_id)
                    except Exception:
                        pass
                
                text = f"<b>{jumlah}</b> file Excel/CSV diterima. Silakan pilih tindakan:"
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
                
                # Jika belum ada status_msg_id atau edit gagal, kirim pesan baru
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                data["status_msg_id"] = msg.message_id
                db.set_session(user_id, sess["state"], data)
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
                    cell_value = int(cell_value)
                else:
                    cell_value = f"{cell_value:.0f}"
            text = str(cell_value).strip()
            for m in PHONE_REGEX.findall(text):
                has_plus = m.startswith("+")
                clean = re.sub(r'[^0-9]', '', m)
                if not clean:
                    continue
                if has_plus:
                    clean = "+" + clean
                
                # Gunakan add_plus bawaan
                formatted = add_plus(clean)
                
                # Bersihkan extra jika nomor dimulai dengan +8 (Indonesian number input missing 0 or 62)
                if formatted.startswith("+8") and 11 <= len(formatted) <= 14:
                    formatted = "+62" + formatted[2:]
                
                digits_only = re.sub(r'[^\d]', '', formatted)
                if 8 <= len(digits_only) <= 15 and formatted not in seen:
                    seen.add(formatted)
                    numbers.append(formatted)

        if ext == ".csv":
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for row in csv.reader(f):
                    for cell in row:
                        process_cell(cell)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
            for sheet in wb.sheetnames:
                for row in wb[sheet].iter_rows(values_only=True):
                    for cell in row:
                        process_cell(cell)
            wb.close()
    except Exception as e:
        logger.error("Error ekstrak %s: %s", filepath, e)
    return numbers


async def cmd_xlsxtovcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, S0, {"count": 0, "total_size": 0, "total_contacts": 0})
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Kirim file <b>.xlsx</b> atau <b>.csv</b> sekarang. Ketik <b>/done</b> jika sudah.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )


async def handle_xtv_contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S1:
        return
    data = sess["data"]
    data["contact_name"] = update.message.text.strip()
    db.set_session(user_id, S1, data)
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("STANDAR", callback_data="xtv_style_standard", style="primary"),
            InlineKeyboardButton("DENGAN TANGGAL", callback_data="xtv_style_date", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    await update.message.reply_text(
        text=f"Pilih format penamaan untuk kontak <b>{data['contact_name']}</b>:",
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
        text="Berapa kontak per file? Contoh: <b>100</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_xtv_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S2:
        return
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Masukkan angka. Contoh: 100")
        return
    
    per_file = int(text)
    if per_file < 1 or per_file > MAX_CONTACTS_PER_FILE:
        await update.message.reply_text(f"Harap masukkan angka antara 1 sampai {MAX_CONTACTS_PER_FILE:,}.")
        return

    data = sess["data"]
    data["per_file"] = per_file
    db.set_session(user_id, S3, data)
    await update.message.reply_text("Nama file? Contoh: <b>FEE</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]))


async def handle_xtv_file_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S3:
        return
    data = sess["data"]
    data["file_name"] = sanitize_filename(update.message.text.strip())
    db.set_session(user_id, S4, data)
    await update.message.reply_text("Nomor urut awal? Contoh: <b>1</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]))


async def handle_xtv_awalan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S4:
        return
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("Input angka valid.")
        return
    data = sess["data"]
    data["awalan"] = int(text)
    
    db.set_session(user_id, S5, data)
    await handle_xtv_process(update, context)


async def handle_xtv_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if sess["state"] not in [S0, S5]:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        return
    
    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".xlsx", ".csv"):
        await update.message.reply_text("Kirim file dengan ekstensi .xlsx atau .csv.")
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
            if sess["state"] not in [S0, S5]:
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
                await update.message.reply_text(f"Batas <b>{MAX_FILES}</b> file. Ketik /done.")
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                await update.message.reply_text(f"Batas <b>{MAX_SIZE_MB}MB</b>. Ketik /done.")
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            # Tidak parse isi file saat upload — hanya catat count & size
            # Parse sesungguhnya terjadi saat /done di do_build (sekali saja)
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

    # Hapus status message agar tidak menumpuk
    status_msg_id = data.get("status_msg_id")
    if status_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass

    if sess["state"] == S0:
        if data["count"] == 0:
            await update.message.reply_text("Belum ada file yang dikirim.")
            return
        
        db.set_session(user_id, S1, data)
        await update.message.reply_text(
            f"{data['count']} file Excel/CSV diterima. Nama kontak? Contoh: <b>FEE</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        return

    if sess["state"] != S5:
        return
    
    data = sess["data"]
    if data["count"] == 0:
        await update.message.reply_text("Belum ada file yang dikirim.")
        return
    if data.get("is_processing"):
        return

    data["is_processing"] = True
    db.set_session(user_id, sess["state"], data)
    await handle_xtv_process(update, context)


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
    
    send_status = await update.message.reply_text("⏳ Memproses...")

    user_dir = get_user_dir(user_id)
    xtv_dir = os.path.join(user_dir, "xlsxtovcf")
    
    files = []
    if os.path.exists(xtv_dir):
        # Ambil semua file xlsx dan csv
        files = [f for f in os.listdir(xtv_dir) if f.endswith(('.xlsx', '.csv'))]
        # Urutkan berdasarkan ID pesan di nama file
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
        
        # Ambil tanggal hari ini di WIB (Jakarta)
        from datetime import datetime, timezone, timedelta
        jakarta_tz = timezone(timedelta(hours=7))
        today_str = datetime.now(jakarta_tz).strftime("%d/%m") # format: DD/MM
        
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
            label = f"{file_name} {file_counter}"
            content = contacts_to_vcf(contacts).encode("utf-8")
            results.append((label, content))
            file_counter += 1
            
        return all_numbers, results

    all_numbers, results = await loop.run_in_executor(None, do_build)

    import io
    from telegram.error import RetryAfter

    try:
        if not results:
            await update.message.reply_text("Gagal. Data tidak ditemukan.")
            return

        total_files = len(results)

        header_text = f"{len(all_numbers)} kontak -> {total_files} file\n"
        lines = [f"{file_name} {awalan + i}.vcf" for i in range(total_files)]

        CHUNK = 50
        for i in range(0, len(lines), CHUNK):
            msg = (header_text if i == 0 else "") + "\n".join(lines[i:i + CHUNK])
            await update.message.reply_text(msg)

        # ── SEQUENTIAL SEND FOR LOCAL API ──
        # Tanpa batching/delay, kirim secepat mungkin.
        # Update progress tiap 10 file untuk menghindari client choke / message queue overflow.
        for idx, (label, content) in enumerate(results):
            buf = io.BytesIO(content)
            buf.name = f"{label}.vcf"

            if idx % SEND_PROGRESS_INTERVAL == 0:
                progress_pct = int(((idx + 1) / total_files) * 100)
                try:
                    await send_status.edit_text(
                        f"Mengirim <b>{idx + 1} / {total_files}</b> file ({progress_pct}%)",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            for attempt in range(SEND_MAX_RETRIES):
                try:
                    buf.seek(0)
                    await update.message.reply_document(
                        document=buf,
                        filename=f"{label}.vcf",
                        read_timeout=FILE_READ_TIMEOUT,
                        write_timeout=FILE_WRITE_TIMEOUT,
                        connect_timeout=FILE_CONNECT_TIMEOUT
                    )
                    break
                except RetryAfter as e:
                    wait_secs = max(int(e.retry_after), 2) + 1
                    logger.warning(f"[XTV] Flood limit {label}.vcf, tunggu {wait_secs}s")
                    await asyncio.sleep(wait_secs)
                except Exception as ex:
                    logger.error(f"[XTV] Gagal kirim {label}.vcf attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(SEND_RETRY_DELAY)

            if (idx + 1) % SEND_BATCH_SIZE == 0:
                await asyncio.sleep(SEND_BATCH_DELAY)
            else:
                await asyncio.sleep(SEND_FILE_DELAY)

        # Update final setelah loop selesai
        try:
            await send_status.edit_text(
                f"Mengirim <b>{total_files} / {total_files}</b> file (100%)",
                parse_mode="HTML"
            )
        except Exception:
            pass

        try:
            try:
                await send_status.delete()
            except Exception:
                pass

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_xlsxtovcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            await update.message.reply_text(
                f"Proses selesai.\n"
                f"Total file: <b>{total_files} VCF</b>\n"
                f"Total kontak: <b>{len(all_numbers)} nomor</b>",
                reply_markup=keyboard
            )
        except Exception:
            pass

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
    db.set_session(user_id, S0, {"count": 0, "total_size": 0, "total_contacts": 0})

    try:
        await query.message.edit_text(
            text="Kirim file <b>.xlsx</b> atau <b>.csv</b> sekarang. Ketik <b>/done</b> jika sudah."
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim file <b>.xlsx</b> atau <b>.csv</b> sekarang. Ketik <b>/done</b> jika sudah.",
            reply_markup=ReplyKeyboardRemove()
        )
