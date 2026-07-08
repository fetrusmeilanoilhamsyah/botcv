"""
pecahtxt.py — Pecah file TXT (nomor HP) menjadi beberapa file TXT kecil.
UI/UX Level Dewa: Inverted Flow, Single-Message Wizard, Fast ZIP/Single delivery.
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
from core.txt_splitter import split_txt
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

S0 = "PECAHTXT_WAIT_FILE"
S1 = "PECAHTXT_PER_FILE"
S2 = "PECAHTXT_DELIVERY"
S3 = "PECAHTXT_PROCESSING"

DEBOUNCE_SECONDS = 1.2

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    per_file = data.get("per_file", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS: {count} FILE]</b>" if count else "<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count}</code>" if count else "Berkas: ➖")
        
    if step == 2:
        parts.append(f"<b>[PECAH / {per_file}]</b>" if per_file else "<b>[PECAH / FILE]</b>")
    elif step > 2 and per_file:
        parts.append(f"Pecah: <code>{per_file}</code>")
    else:
        parts.append("Pecah: ➖")
        
    if step == 3:
        parts.append("<b>[KIRIM]</b>")
    else:
        parts.append("Kirim: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ TXT SPLIT CONSOLE ]</b>\n"
        "────────────────────────────\n"
        f"<blockquote>{breadcrumbs}</blockquote>\n"
        "────────────────────────────\n\n"
    )

def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.txt</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"\u2022 Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"\u2022 Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file</blockquote>"
    )

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
    pecah_dir = os.path.join(user_dir, "pecahtxt")
    shutil.rmtree(pecah_dir, ignore_errors=True)

def cleanup_inactive_users(inactive_ids: list) -> int:
    for uid in inactive_ids:
        _cancel_timer(uid)
        _clear_buffers(uid)
        _user_locks.pop(uid, None)
    return len(inactive_ids)

async def _debounce_notify(user_id: int, context, chat_id: int):
    try:
        await asyncio.sleep(1)
        if _user_timers.get(user_id) is asyncio.current_task():
            sess = db.get_session(user_id)
            if sess and sess.get("state") == S0:
                data = sess["data"]
                jumlah = data["count"]
                
                # Hapus welcome messages lama
                from handlers.start import _welcome_messages
                welcome_ids = _welcome_messages.pop(user_id, [])
                for w_id in welcome_ids:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=w_id)
                    except Exception:
                        pass
                
                text = (
                    _get_breadcrumbs(data, 1) +
                    f"<blockquote><b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                    f"Berhasil mengunduh <code>{jumlah}</code> berkas TXT.\n\n"
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
                db.set_session(user_id, S0, data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Debounce notify error pecahtxt: %s", e)

def _reset_timer(user_id, context, chat_id):
    old = _user_timers.get(user_id)
    if old:
        old.cancel()
    _user_timers[user_id] = asyncio.ensure_future(
        _debounce_notify(user_id, context, chat_id)
    )

async def cmd_pecahtxt(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def handle_pecahtxt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S0:
        return

    doc = update.message.document
    ext = os.path.splitext(doc.file_name)[1].lower() if doc and doc.file_name else ""
    if not doc or not doc.file_name or ext != ".txt":
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
                        f"<code>{doc.file_name}</code> bukan berkas TXT (<code>.txt</code>).</blockquote>"
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
    file_obj = await context.bot.get_file(doc.file_id)

    pecah_dir = os.path.join(get_user_dir(user_id), "pecahtxt", "input")
    os.makedirs(pecah_dir, exist_ok=True)
    out_path = os.path.join(pecah_dir, f"{msg_id}.txt")

    try:
        await file_obj.download_to_drive(out_path)

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
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            data["count"] += 1
            data["total_size"] += doc.file_size
            db.set_session(user_id, S0, data)

        _reset_timer(user_id, context, chat_id)

    except Exception as e:
        logger.error("PecahTXT download error: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

async def handle_pecahtxt_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    # Edit the message in-place
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text=_get_breadcrumbs(data, 2) + f"<blockquote><b>[ LANGKAH 2: JUMLAH NOMOR PER FILE ]</b>\nTerdeteksi: <code>{data.get('count', 0)}</code> file TXT.\n\nKetik jumlah nomor per file (contoh: <code>100</code>):</blockquote>",
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
                text=_get_breadcrumbs(data, 2) + f"<blockquote><b>[ LANGKAH 2: JUMLAH NOMOR PER FILE ]</b>\nTerdeteksi: <code>{data.get('count', 0)}</code> file TXT.\n\nKetik jumlah nomor per file (contoh: <code>100</code>):</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass

    db.set_session(user_id, S1, data)

async def handle_pecahtxt_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        if status_msg_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(sess["data"], 2) + "<blockquote>⚠️ <b>Harap masukkan angka saja.</b>\n\nBerapa nomor per file? Contoh: <b>100</b></blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        return

    per_file = int(text)
    if per_file < 1 or per_file > MAX_CONTACTS_PER_FILE:
        if status_msg_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(sess["data"], 2) + f"<blockquote>⚠️ <b>Harap masukkan angka antara 1 sampai {MAX_CONTACTS_PER_FILE:,}.</b>\n\nBerapa nomor per file? Contoh: <b>100</b></blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        return

    data = sess["data"]
    data["per_file"] = per_file
    db.set_session(user_id, S2, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="pecahtxt_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="pecahtxt_deliv_zip", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    if status_msg_id:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(data, 3) + "<blockquote><b>[ LANGKAH 3: FORMAT PENGIRIMAN ]</b>\nPilih format pengiriman file TXT:</blockquote>",
            parse_mode="HTML",
            reply_markup=deliv_keyboard
        )

async def handle_pecahtxt_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S2:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "pecahtxt_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    db.set_session(user_id, S3, data)
    await handle_pecahtxt_process(update, context)

async def handle_pecahtxt_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

async def handle_pecahtxt_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    process_text = "<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memecah berkas TXT...</blockquote>"
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=process_text,
            parse_mode="HTML"
        )
    except Exception:
        pass

    pecah_input_dir = os.path.join(get_user_dir(user_id), "pecahtxt", "input")
    pecah_out_dir   = os.path.join(get_user_dir(user_id), "pecahtxt", "output")
    os.makedirs(pecah_out_dir, exist_ok=True)

    try:
        loop = asyncio.get_running_loop()

        def process():
            all_lines = []
            files = sorted(
                [f for f in os.listdir(pecah_input_dir) if f.endswith(".txt")],
                key=lambda x: int(x.split(".")[0])
            )
            for fname in files:
                path = os.path.join(pecah_input_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped:
                                all_lines.append(stripped)
                except Exception:
                    pass

            return all_lines, split_txt(all_lines, pecah_out_dir, per_file)

        all_lines, output_files = await loop.run_in_executor(None, process)
        total_nomor = len(all_lines)
        total_parts = len(output_files)

        if total_parts == 0:
            if status_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=status_msg_id,
                        text="<blockquote>⚠️ <b>Gagal. Tidak ada nomor yang ditemukan.</b></blockquote>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            return

        delivery_mode = data.get("delivery_mode", "single")

        if delivery_mode == "zip":
            if status_msg_id:
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
                for out_path in output_files:
                    fname = os.path.basename(out_path)
                    with open(out_path, "rb") as f:
                        zip_file.writestr(fname, f.read())
            zip_buffer.seek(0)
            zip_filename = "pecahan_txt.zip"

            if status_msg_id:
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
                    logger.error(f"[PecahTXT] Gagal kirim ZIP attempt {attempt+1}: {ex}")
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
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_pecahtxt_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            box_text = (
                f"<pre><b>"
                f"┌────────────────────────────────────────┐\n"
                f"│             PROSES SELESAI             │\n"
                f"├────────────────────────────────────────┤\n"
                f"│ Total Berkas   : {_fit(f'{total_files} TXT'):<22} │\n"
                f"│ Berkas Output  : {_fit(f'{total_parts} TXT (ZIP)'):<22} │\n"
                f"│ Nomor / File   : {_fit(per_file):<22} │\n"
                f"│ Total Nomor    : {_fit(f'{total_nomor:,}'):<22} │\n"
                f"└────────────────────────────────────────┘\n"
                f"</b></pre>\n\n"
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
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=status_msg_id,
                        text="<blockquote><b>[ SYSTEM: SENDING FILES ]</b>\nSedang mengirim file TXT satu per satu...</blockquote>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            sent_count = 0
            from telegram.error import RetryAfter
            from config import SEND_PROGRESS_INTERVAL, SEND_BATCH_SIZE

            for idx, out_path in enumerate(output_files):
                fname = os.path.basename(out_path)
                try:
                    with open(out_path, "rb") as fd:
                        content = fd.read()
                except Exception as ex:
                    logger.error(f"[PecahTXT] Gagal membaca file pecahan {out_path}: {ex}")
                    sent_count += 1
                    continue

                buf = io.BytesIO(content)
                buf.name = fname

                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        buf.seek(0)
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=buf,
                            filename=fname,
                            read_timeout=FILE_READ_TIMEOUT,
                            write_timeout=FILE_WRITE_TIMEOUT,
                            connect_timeout=FILE_CONNECT_TIMEOUT,
                        )
                        sent_count += 1
                        break
                    except RetryAfter as e:
                        wait_secs = max(int(e.retry_after), 2) + 1
                        logger.warning(f"[PecahTXT] Flood limit sekuensial, tunggu {wait_secs}s")
                        await asyncio.sleep(wait_secs)
                    except Exception as ex:
                        logger.error(f"[PecahTXT] Gagal kirim file TXT sekuensial {fname} attempt {attempt+1}: {ex}")
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += 1
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)

                if sent_count < total_parts:
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
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_pecahtxt_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            box_text = (
                f"<pre><b>"
                f"┌────────────────────────────────────────┐\n"
                f"│             PROSES SELESAI             │\n"
                f"├────────────────────────────────────────┤\n"
                f"│ Total Berkas   : {_fit(f'{total_files} TXT'):<22} │\n"
                f"│ Berkas Output  : {_fit(f'{total_parts} TXT'):<22} │\n"
                f"│ Nomor / File   : {_fit(per_file):<22} │\n"
                f"│ Total Nomor    : {_fit(f'{total_nomor:,}'):<22} │\n"
                f"└────────────────────────────────────────┘\n"
                f"</b></pre>\n\n"
                f"<i>Silakan unduh file TXT di atas.</i>"
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
        logger.error("PecahTXT done error: %s", e)
        if status_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Terjadi kesalahan. Coba kirim ulang.</b></blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)

async def handle_show_pecahtxt_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    init_data = {"count": 0, "total_size": 0}
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
