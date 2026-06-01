"""
pecahvcf.py — Pecah file VCF menjadi beberapa file VCF kecil.
UI/UX Level Dewa: Inverted Flow, Single-Message Wizard, Fast ZIP/Single delivery.
"""
import os
import io
import shutil
import asyncio
import logging
import zipfile
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.error import RetryAfter
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf
from config import (
    MAX_CONTACTS_PER_FILE,
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
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

logger = logging.getLogger(__name__)

S0 = "PECAH_WAIT_FILE"
S1 = "PECAH_PER_FILE"
S2 = "PECAH_DELIVERY"
S3 = "PECAH_PROCESSING"

_user_timers: dict = {}
_user_locks: dict  = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


def _cancel_timer(user_id: int):
    timer = _user_timers.pop(user_id, None)
    if timer:
        timer.cancel()


def _clear_buffers(user_id: int):
    user_dir = get_user_dir(user_id)
    pecah_dir = os.path.join(user_dir, "pecahvcf")
    shutil.rmtree(pecah_dir, ignore_errors=True)


def cleanup_inactive_users(inactive_ids: list) -> int:
    for uid in inactive_ids:
        _cancel_timer(uid)
        _clear_buffers(uid)
    return len(inactive_ids)


async def _debounce_notify(user_id: int, context, chat_id: int):
    try:
        await asyncio.sleep(1)
        if _user_timers.get(user_id) is asyncio.current_task():
            sess = db.get_session(user_id)
            if sess and sess.get("state") == S0:
                data = sess["data"]
                jumlah = data["count"]
                jumlah_kontak = data.get("total_contacts", 0)
                
                # Hapus welcome messages lama
                from handlers.start import _welcome_messages
                welcome_ids = _welcome_messages.pop(user_id, [])
                for w_id in welcome_ids:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=w_id)
                    except Exception:
                        pass
                
                text = f"<b>{jumlah}</b> file VCF diterima ({jumlah_kontak} kontak). Silakan pilih tindakan:"
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
                db.set_session(user_id, S0, data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error pecahvcf: %s", e)


def _reset_timer(user_id, context, chat_id):
    old = _user_timers.get(user_id)
    if old:
        old.cancel()
    _user_timers[user_id] = asyncio.ensure_future(
        _debounce_notify(user_id, context, chat_id)
    )


async def cmd_pecahvcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Kirim file <b>.VCF</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])


async def handle_pecahvcf_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S0:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".vcf"):
        await update.message.reply_text("Kirim file dengan ekstensi .vcf.")
        return

    msg_id = update.message.message_id
    file_obj = await context.bot.get_file(doc.file_id)

    pecah_dir = os.path.join(get_user_dir(user_id), "pecahvcf", "input")
    os.makedirs(pecah_dir, exist_ok=True)
    out_path = os.path.join(pecah_dir, f"{msg_id}.vcf")

    try:
        await file_obj.download_to_drive(out_path)

        # Hitung jumlah kontak DI LUAR lock
        c = 0
        try:
            with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "BEGIN:VCARD" in line:
                        c += 1
        except Exception:
            pass

        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if not sess or sess["state"] != S0:
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            data = sess["data"]
            if data["count"] >= MAX_FILES:
                await update.message.reply_text(f"Batas <b>{MAX_FILES}</b> file. Silakan ketik selesai atau klik PROSES SEKARANG.")
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                await update.message.reply_text(f"Batas <b>{MAX_SIZE_MB}MB</b>. Silakan ketik selesai atau klik PROSES SEKARANG.")
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            data["count"] += 1
            data["total_size"] += doc.file_size
            data["total_contacts"] = data.get("total_contacts", 0) + c
            db.set_session(user_id, S0, data)

        _reset_timer(user_id, context, chat_id)

    except Exception as e:
        logger.error("PecahVCF download error: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


async def handle_pecahvcf_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if not sess or sess["state"] != S0:
        return
    data = sess["data"]

    if update.message and update.message.text in ("done", "selesai", "/done"):
        try:
            await update.message.delete()
        except Exception:
            pass

    if data["count"] == 0:
        return

    db.set_session(user_id, S1, data)
    
    status_msg_id = data.get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=f"<b>{data.get('total_contacts', 0)}</b> kontak terdeteksi. Berapa kontak per file? Contoh: <b>100</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_pecahvcf_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
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
    db.set_session(user_id, S2, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="pecahvcf_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="pecahvcf_deliv_zip", style="primary")
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


async def handle_pecahvcf_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S2:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "pecahvcf_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    db.set_session(user_id, S3, data)
    await handle_pecahvcf_process(update, context)


async def handle_pecahvcf_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_pecahvcf_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S3:
        return
    data = dict(sess["data"])
    if data.get("is_processing"):
        return
    data["is_processing"] = True
    db.set_session(user_id, S3, data)

    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    total_files = data["count"]
    per_file = data["per_file"]
    status_msg_id = data.get("status_msg_id")

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="⏳ Memproses...",
            parse_mode="HTML"
        )
    except Exception:
        pass

    pecah_input_dir = os.path.join(get_user_dir(user_id), "pecahvcf", "input")
    pecah_out_dir   = os.path.join(get_user_dir(user_id), "pecahvcf", "output")
    os.makedirs(pecah_out_dir, exist_ok=True)

    try:
        loop = asyncio.get_running_loop()

        def process():
            all_contacts = []
            files = sorted(
                [f for f in os.listdir(pecah_input_dir) if f.endswith(".vcf")],
                key=lambda x: int(x.split(".")[0])
            )
            for fname in files:
                path = os.path.join(pecah_input_dir, fname)
                try:
                    contacts = parse_vcf_file(path)
                    all_contacts.extend(contacts)
                except Exception:
                    pass

            output_files = []
            for idx, i in enumerate(range(0, len(all_contacts), per_file), start=1):
                chunk = all_contacts[i:i + per_file]
                out_path = os.path.join(pecah_out_dir, f"PECAHAN {idx}.vcf")
                with open(out_path, "w", encoding="utf-8") as f_out:
                    f_out.write(contacts_to_vcf(chunk))
                output_files.append(out_path)

            return len(all_contacts), output_files

        total_contacts, output_files = await loop.run_in_executor(None, process)
        total_parts = len(output_files)

        if total_parts == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="Gagal. Tidak ada kontak yang ditemukan.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        delivery_mode = data.get("delivery_mode", "single")

        if delivery_mode == "zip":
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="Mengompresi file ke ZIP...",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for out_path in output_files:
                    fname = os.path.basename(out_path)
                    with open(out_path, "rb") as f:
                        zip_file.writestr(fname, f.read())
            zip_buffer.seek(0)
            zip_filename = "pecahan_vcf.zip"

            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="Mengirim file ZIP...",
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
                    logger.error(f"[PecahVCF] Gagal kirim ZIP attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        raise
                    else:
                        await asyncio.sleep(SEND_RETRY_DELAY)

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_pecahvcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Proses selesai.\n"
                    f"Total kontak: <b>{total_contacts:,}</b>\n"
                    f"Kontak per file: <b>{per_file}</b>\n"
                    f"File ZIP: <b>{total_parts} pecahan</b>\n\n"
                    f"Silakan unduh file ZIP di atas."
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

        else:
            # Mode "single" menggunakan send_media_group chunked
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=f"Mengirim <b>0 / {total_parts}</b> file VCF...",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            sent_count = 0
            chunk_size = 10

            async def send_chunk(chunk_paths, idx):
                await asyncio.sleep(idx * 0.1)
                nonlocal sent_count
                media_group = []
                bio_list = []
                for out_path in chunk_paths:
                    with open(out_path, "rb") as fd:
                        content = fd.read()
                    fname = os.path.basename(out_path)
                    buf = io.BytesIO(content)
                    buf.name = fname
                    bio_list.append(buf)
                    media_group.append(InputMediaDocument(media=buf, filename=fname))

                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        if len(media_group) == 1:
                            bio_list[0].seek(0)
                            await context.bot.send_document(
                                chat_id=update.effective_chat.id,
                                document=bio_list[0],
                                filename=chunk_paths[0],
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
                        sent_count += len(chunk_paths)
                        break
                    except Exception as ex:
                        from telegram.error import RetryAfter
                        if isinstance(ex, RetryAfter):
                            wait_secs = max(int(ex.retry_after), 2) + 1
                            logger.warning(f"[PecahVCF] Flood limit chunk, tunggu {wait_secs}s")
                            await asyncio.sleep(wait_secs)
                            continue
                        logger.error(f"[PecahVCF] Gagal kirim chunk VCF attempt {attempt+1}: {ex}")
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += len(chunk_paths)
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)
                await asyncio.sleep(SEND_BATCH_DELAY)

            async def progress_ticker():
                last = -1
                while sent_count < total_parts:
                    if sent_count != last:
                        last = sent_count
                        try:
                            await context.bot.edit_message_text(
                                chat_id=update.effective_chat.id,
                                message_id=status_msg_id,
                                text=f"Mengirim <b>{sent_count} / {total_parts}</b> file VCF...",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)

            ticker = asyncio.create_task(progress_ticker())
            try:
                tasks = []
                for i in range(0, len(output_files), chunk_size):
                    chunk_paths = output_files[i:i + chunk_size]
                    tasks.append(send_chunk(chunk_paths, i // chunk_size))
                await asyncio.gather(*tasks)
            finally:
                ticker.cancel()
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_pecahvcf_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Proses selesai.\n"
                    f"Total kontak: <b>{total_contacts:,}</b>\n"
                    f"Kontak per file: <b>{per_file}</b>\n"
                    f"File dihasilkan: <b>{total_parts} file</b>"
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

    except Exception as e:
        logger.error("PecahVCF done error: %s", e)
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Terjadi kesalahan. Coba kirim ulang.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)


async def handle_show_pecahvcf_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, S0, {"count": 0, "total_size": 0, "total_contacts": 0})

    try:
        await query.message.edit_text(text="Kirim file <b>.VCF</b> sekarang.")
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim file <b>.VCF</b> sekarang.",
            reply_markup=ReplyKeyboardRemove()
        )