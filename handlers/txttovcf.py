"""
txttovcf.py — Disk-based, terima paralel, sort by message_id.
UI/UX Level Dewa: Single-Message Morphing Wizard. Semua teks di-edit di satu pesan bot,
dan seluruh input/upload user langsung dihapus secara instan agar chat bersih 100%.
"""
import os
import shutil
import asyncio
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import add_plus
from core.utils import sanitize_filename

S0 = "TTV_WAIT_FILE"
S1 = "TTV_CONTACT_NAME"
S2 = "TTV_PER_FILE"
S3 = "TTV_FILE_NAME"
S4 = "TTV_AWALAN"
S5 = "TTV_COLLECTING"
S6 = "TTV_DELIVERY"

from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
    MAX_CONTACTS_PER_FILE,
    SEND_MAX_RETRIES,
    SEND_RETRY_DELAY,
    FILE_READ_TIMEOUT,
    FILE_WRITE_TIMEOUT,
    FILE_CONNECT_TIMEOUT,
    SEND_FILE_DELAY,
    SEND_BATCH_DELAY,
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
                data = sess["data"]
                jumlah = data["count"]
                
                text = f"<b>{jumlah}</b> file TXT diterima. Silakan pilih tindakan:"
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
                
                # Kirim pesan baru di paling bawah di bawah berkas yang diunggah
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
    """Handler Command /txttovcf"""
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    db.set_session(user_id, S0, {"count": 0, "total_size": 0, "total_contacts": 0})
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Kirim file <b>.TXT</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])


async def handle_ttv_contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memasukkan nama kontak"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S1:
        return
    data = sess["data"]
    data["contact_name"] = update.message.text.strip()
    db.set_session(user_id, S1, data)
    
    # Hapus input teks user
    try:
        await update.message.delete()
    except Exception:
        pass
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("STANDAR", callback_data="ttv_style_standard", style="primary"),
            InlineKeyboardButton("DENGAN TANGGAL", callback_data="ttv_style_date", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    status_msg_id = data.get("status_msg_id")
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=f"Pilih format penamaan untuk kontak <b>{data['contact_name']}</b>:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_ttv_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih format penamaan"""
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
    """User memasukkan jumlah kontak per file"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S2:
        return
    
    text = update.message.text.strip()
    
    # Hapus input teks user
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
            text="⚠️ Harap masukkan angka saja.\n\nBerapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
    
    per_file = int(text)
    if per_file < 1 or per_file > MAX_CONTACTS_PER_FILE:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=f"⚠️ Harap masukkan angka antara 1 sampai {MAX_CONTACTS_PER_FILE:,}.\n\nBerapa kontak per file? Contoh: <b>100</b>",
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
        text="Nama file? Contoh: <b>FEE</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_ttv_file_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memasukkan nama file"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S3:
        return
    
    # Hapus input teks user
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
        text="Nomor urut awal? Contoh: <b>1</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_ttv_awalan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memasukkan nomor urut awal"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != S4:
        return
    
    # Hapus input teks user
    try:
        await update.message.delete()
    except Exception:
        pass

    text = update.message.text.strip()
    status_msg_id = sess["data"].get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not text.isdigit() or int(text) < 1:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="⚠️ Harap masukkan angka valid (minimal 1).\n\nNomor urut awal? Contoh: <b>1</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
        
    data = sess["data"]
    data["awalan"] = int(text)
    
    db.set_session(user_id, S6, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="ttv_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="ttv_deliv_zip", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text="Pilih format pengiriman file VCF:",
        parse_mode="HTML",
        reply_markup=deliv_keyboard
    )


async def handle_ttv_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih format pengiriman (VCF satu per satu atau ZIP)"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S6:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "ttv_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    # Pindah ke state pemrosesan S5 dan jalankan handle_ttv_process
    db.set_session(user_id, S5, data)
    await handle_ttv_process(update, context)


async def handle_ttv_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hapus chat iseng dari user saat berada di state pemilihan pengiriman"""
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_ttv_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima kiriman file TXT dari user"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if sess["state"] not in [S0, S5]:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        try:
            await update.message.delete()
        except Exception:
            pass
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

            # Hitung kontak
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
    """User memicu selesai kirim file"""
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

    status_msg_id = data.get("status_msg_id")

    if sess["state"] == S0:
        if data["count"] == 0:
            return
        
        db.set_session(user_id, S1, data)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=f"<b>{data.get('total_contacts', 0)}</b> kontak terdeteksi. Nama kontak? Contoh: <b>FEE</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return

    if sess["state"] != S5:
        return
    
    if data["count"] == 0:
        return
    if data.get("is_processing"):
        return

    data["is_processing"] = True
    db.set_session(user_id, sess["state"], data)
    await handle_ttv_process(update, context)


async def handle_ttv_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses pembuatan berkas VCF"""
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    data = sess["data"]
    
    if data.get("is_processing_final"):
        return
    data["is_processing_final"] = True
    db.set_session(user_id, sess["state"], data)
    
    status_msg_id = data.get("status_msg_id")
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text="Memproses...",
        parse_mode="HTML"
    )

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
        
        from datetime import datetime, timezone, timedelta
        jakarta_tz = timezone(timedelta(hours=7))
        today_str = datetime.now(jakarta_tz).strftime("%d/%m")
        
        naming_style = data.get("naming_style", "standard")
        
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
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Gagal. Data tidak ditemukan.",
                parse_mode="HTML"
            )
            return

        total_files = len(results)
        delivery_mode = data.get("delivery_mode", "single")

        if delivery_mode == "zip":
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Mengompresi file ke ZIP...",
                parse_mode="HTML"
            )
            
            import zipfile
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for label, content in results:
                    zip_file.writestr(f"{label}.vcf", content)
            zip_buffer.seek(0)
            zip_filename = f"{file_name}.zip"

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Mengirim file ZIP...",
                parse_mode="HTML"
            )

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
                    _logger.error(f"[TTV] Gagal kirim ZIP attempt {attempt+1}: {ex}")
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
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_txttovcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Proses selesai.\n"
                    f"Total file: <b>{total_files} VCF (ZIP)</b>\n"
                    f"Total kontak: <b>{len(all_numbers)} nomor</b>\n\n"
                    f"Silakan unduh file ZIP di atas."
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            # Daftarkan welcome message baru agar callback berikutnya (batal/proses lain) mengedit pesan ini
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

        else:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=f"Mengirim <b>0 / {total_files}</b> file VCF...",
                parse_mode="HTML"
            )

            sent_count = 0
            chunk_size = 10

            async def send_chunk(chunk_results):
                nonlocal sent_count
                media_group = []
                bio_list = []
                for label, content in chunk_results:
                    buf = io.BytesIO(content)
                    buf.name = f"{label}.vcf"
                    bio_list.append(buf)
                    media_group.append(InputMediaDocument(media=buf, filename=f"{label}.vcf"))

                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        if len(media_group) == 1:
                            bio_list[0].seek(0)
                            await context.bot.send_document(
                                chat_id=update.effective_chat.id,
                                document=bio_list[0],
                                filename=f"{chunk_results[0][0]}.vcf",
                                read_timeout=FILE_READ_TIMEOUT,
                                write_timeout=FILE_WRITE_TIMEOUT,
                                connect_timeout=FILE_CONNECT_TIMEOUT,
                            )
                        else:
                            for b in bio_list:
                                b.seek(0)
                            await context.bot.send_media_group(
                                chat_id=update.effective_chat.id,
                                media=media_group,
                                read_timeout=120,
                                write_timeout=120,
                                connect_timeout=60,
                            )
                        sent_count += len(chunk_results)
                        break
                    except Exception as ex:
                        _logger.error(f"[TTV] Gagal kirim chunk VCF attempt {attempt+1}: {ex}")
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += len(chunk_results)
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)
                await asyncio.sleep(SEND_BATCH_DELAY)

            async def progress_ticker():
                last = -1
                while sent_count < total_files:
                    if sent_count != last:
                        last = sent_count
                        try:
                            await context.bot.edit_message_text(
                                chat_id=update.effective_chat.id,
                                message_id=status_msg_id,
                                text=f"Mengirim <b>{sent_count} / {total_files}</b> file VCF...",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)

            ticker = asyncio.create_task(progress_ticker())
            try:
                for i in range(0, len(results), chunk_size):
                    chunk_results = results[i:i + chunk_size]
                    await send_chunk(chunk_results)
            finally:
                ticker.cancel()
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass

            # Hapus status message lama agar laporan sukses berada di paling bawah
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_txttovcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Proses selesai.\n"
                    f"Total file: <b>{total_files} VCF</b>\n"
                    f"Total kontak: <b>{len(all_numbers)} nomor</b>\n\n"
                    f"Silakan unduh file VCF di atas."
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            # Daftarkan welcome message baru agar callback berikutnya (batal/proses lain) mengedit pesan ini
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)


async def handle_show_txttovcf_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, S0, {"count": 0, "total_size": 0, "total_contacts": 0})

    text = "Kirim file <b>.TXT</b> sekarang."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    try:
        await query.message.edit_text(text=text, parse_mode="HTML", reply_markup=keyboard)
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
            reply_markup=keyboard
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])