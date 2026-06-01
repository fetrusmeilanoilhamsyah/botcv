"""
txttovcf.py — Disk-based, terima paralel, sort by message_id, tampilkan daftar file dulu, lalu kirim semua.
"""
import os
import shutil
import asyncio
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import add_plus, contacts_to_vcf
from core.utils import sanitize_filename

S0 = "TTV_WAIT_FILE"
S1 = "TTV_CONTACT_NAME"
S2 = "TTV_PER_FILE"
S3 = "TTV_FILE_NAME"
S4 = "TTV_AWALAN"
S5 = "TTV_COLLECTING"

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
                jumlah = sess["data"]["count"]
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success")]
                ])
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{jumlah} file TXT diterima. Ketik /done jika sudah.",
                    reply_markup=keyboard
                )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Debounce notify error in txttovcf: %s", e)


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
    ttv_dir = os.path.join(user_dir, "txttovcf")
    shutil.rmtree(ttv_dir, ignore_errors=True)


async def cmd_txttovcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Kirim file <b>.TXT</b> sekarang. Ketik /done jika sudah.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )



async def handle_ttv_contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S1:
        return
    data = sess["data"]
    data["contact_name"] = update.message.text.strip()
    db.set_session(user_id, S1, data)
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("STANDAR", callback_data="ttv_style_standard", style="primary"),
            InlineKeyboardButton("DENGAN TANGGAL", callback_data="ttv_style_date", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    await update.message.reply_text(
        text=f"Pilih format penamaan untuk kontak <b>{data['contact_name']}</b>:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_ttv_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    data = sess["data"]
    style = "standard" if query.data == "ttv_style_standard" else "date"
    data["naming_style"] = style
    db.set_session(user_id, S2, data)
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    await query.edit_message_text(
        text="Berapa kontak per file? Contoh: <b>100</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_ttv_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def handle_ttv_file_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S3:
        return
    data = sess["data"]
    data["file_name"] = sanitize_filename(update.message.text.strip())
    db.set_session(user_id, S4, data)
    await update.message.reply_text("Nomor urut awal? Contoh: <b>1</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]))


async def handle_ttv_awalan(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    # CRITICAL FIX: Save session sebelum call handle_ttv_process
    db.set_session(user_id, S5, data)
    
    # Trigger processing immediately
    await handle_ttv_process(update, context)


async def handle_ttv_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if sess["state"] not in [S0, S5]:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Kirim file dengan ekstensi .txt.")
        return
        
    msg_id = update.message.message_id

    # Download ke disk
    file_obj = await context.bot.get_file(doc.file_id)
    ttv_dir = os.path.join(get_user_dir(user_id), "txttovcf")
    os.makedirs(ttv_dir, exist_ok=True)
    out_path = os.path.join(ttv_dir, f"{msg_id}.txt")
    
    try:
        await file_obj.download_to_drive(out_path)

        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if sess["state"] not in [S0, S5]:
                # State berubah saat download — hapus file
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

            # Hitung perkiraan kontak — cepat, hanya hitung baris tidak kosong
            lines = 0
            try:
                with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip():
                            lines += 1
            except Exception:
                pass

            data["count"] += 1
            data["total_size"] += doc.file_size
            data["total_contacts"] = data.get("total_contacts", 0) + lines
            db.set_session(user_id, sess["state"], data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Download failed in txttovcf: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


async def handle_ttv_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if sess["state"] == S0:
        data = sess["data"]
        if data["count"] == 0:
            await update.message.reply_text("Belum ada file yang dikirim.")
            return
        
        db.set_session(user_id, S1, data)
        await update.message.reply_text(
            f"{data.get('total_contacts', 0)} kontak terdeteksi. Nama kontak? Contoh: <b>FEE</b>",
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
    await handle_ttv_process(update, context)


async def handle_ttv_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    data = sess["data"]
    
    # Check if already processing
    if data.get("is_processing_final"):
        return
    data["is_processing_final"] = True
    db.set_session(user_id, sess["state"], data)
    
    status_msg = await update.message.reply_text(" Memproses...")

    user_dir = get_user_dir(user_id)
    ttv_dir = os.path.join(user_dir, "txttovcf")
    
    files = []
    if os.path.exists(ttv_dir):
        files = [f for f in os.listdir(ttv_dir) if f.endswith('.txt')]
        files.sort(key=lambda x: int(x.split('.')[0]))

    contact_name = data["contact_name"]
    file_name    = data["file_name"]
    per_file     = data["per_file"]
    awalan       = data["awalan"]

    loop = asyncio.get_running_loop()

    def do_build():
        all_numbers = []
        for f in files:
            path = os.path.join(ttv_dir, f)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as file_in:
                    for line in file_in:
                        num = line.strip()
                        if num:
                            all_numbers.append(add_plus(num))
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
        
        # Build VCF langsung tanpa dict perantara — lebih cepat ~30%
        for i in range(0, len(all_numbers), per_file):
            chunk = all_numbers[i:i + per_file]
            vcf_lines = []
            for j, num in enumerate(chunk):
                idx_num = contact_counter + j
                if naming_style == "date":
                    name = f"{contact_name} {today_str} {idx_num}"
                else:
                    name = f"{contact_name}{idx_num}"
                vcf_lines.append(f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL;TYPE=CELL:{num}\nEND:VCARD")
            contact_counter += len(chunk)
            label = f"{file_name} {file_counter}"
            content = ("\n".join(vcf_lines) + "\n").encode("utf-8")
            results.append((label, content))
            file_counter += 1
        return all_numbers, results

    all_numbers, results = await loop.run_in_executor(None, do_build)

    import io
    from telegram.error import RetryAfter
    import logging as _log
    _logger = _log.getLogger(__name__)

    try:
        if not results:
            await status_msg.edit_text("Gagal. Data tidak ditemukan.")
            return

        total_files = len(results)

        try:
            await status_msg.edit_text(f"Mengirim <b>0 / {total_files}</b> file VCF...")
        except Exception:
            pass

        # ── HIGH-SPEED SEQUENTIAL SEND FOR LOCAL API ──
        # Kirim file satu per satu secara berurutan agar urutan dijamin rapi 100% dari file pertama,
        # tetapi menggunakan jeda kecil (SEND_FILE_DELAY dari config.py) agar kecepatan tetap super cepat.
        sent_count = 0

        async def send_one(idx: int, label: str, content: bytes):
            nonlocal sent_count
            buf = io.BytesIO(content)
            buf.name = f"{label}.vcf"
            for attempt in range(SEND_MAX_RETRIES):
                try:
                    buf.seek(0)
                    await update.message.reply_document(
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
                    _logger.warning(f"[TTV] Flood limit {label}.vcf, tunggu {wait_secs}s")
                    await asyncio.sleep(wait_secs)
                except Exception as ex:
                    _logger.error(f"[TTV] Gagal kirim {label}.vcf attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        sent_count += 1  # tetap count supaya progress tidak stuck
                    else:
                        await asyncio.sleep(SEND_RETRY_DELAY)
            await asyncio.sleep(SEND_FILE_DELAY)

        async def progress_ticker():
            last = -1
            while sent_count < total_files:
                if sent_count != last:
                    last = sent_count
                    try:
                        await status_msg.edit_text(
                            f"Mengirim <b>{sent_count} / {total_files}</b> file VCF..."
                        )
                    except Exception:
                        pass
                await asyncio.sleep(1.5)  # update tiap 1.5 detik — tidak spam edit

        ticker = asyncio.create_task(progress_ticker())
        try:
            for i, (label, content) in enumerate(results):
                await send_one(i, label, content)
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass

        # Update final setelah semua selesai
        try:
            await status_msg.edit_text(
                f"Mengirim <b>{total_files} / {total_files}</b> file VCF..."
            )
        except Exception:
            pass

        try:
            await status_msg.delete()
        except Exception:
            pass

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_txttovcf_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        await update.message.reply_text(
            f"Proses selesai.\n"
            f"Total file: <b>{total_files} VCF</b>\n"
            f"Total kontak: <b>{len(all_numbers)} nomor</b>",
            reply_markup=keyboard
        )

    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)


async def handle_show_txttovcf_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (TXT to VCF)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, S0, {"count": 0, "total_size": 0, "total_contacts": 0})

    try:
        await query.message.edit_text(
            text="Kirim file <b>.TXT</b> sekarang. Ketik /done jika sudah."
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim file <b>.TXT</b> sekarang. Ketik /done jika sudah.",
            reply_markup=ReplyKeyboardRemove()
        )