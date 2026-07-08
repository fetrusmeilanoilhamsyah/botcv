"""
vcftotxt.py — Disk-based approach to prevent OOM.
"""
import os
import shutil
import asyncio
import io
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file
from core.utils import sanitize_filename

STATE            = "VCF2TXT_COLLECTING"
STATE_NAMING     = "VCF2TXT_NAMING"
STATE_DELIVERY   = "VCF2TXT_DELIVERY"
STATE_PROCESSING = "VCF2TXT_PROCESSING"

from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
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
    file_name = data.get("file_name", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS: {count} VCF]</b>" if count else "<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count}</code>" if count else "Berkas: ➖")
        
    if step == 2:
        parts.append(f"<b>[NAMA FILE]</b>")
    elif step > 2 and file_name:
        parts.append(f"Nama: <code>{file_name}</code>")
    else:
        parts.append("Nama: ➖")
        
    if step == 3:
        parts.append(f"<b>[FORMAT KIRIM]</b>")
    else:
        parts.append("Kirim: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ VCF ➔ TXT CONSOLE ]</b>\n"
                f"{breadcrumbs}\n"
        "\n"
    )


def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.vcf</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"• Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"• Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file"
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
            if sess and sess.get("state") == STATE:
                data = sess["data"]
                jumlah_file = data["count"]
                jumlah_kontak = data.get("total_contacts", 0)
                
                # Hapus welcome messages lama (hanya sekali saat file pertama masuk)
                from handlers.start import _welcome_messages
                welcome_ids = _welcome_messages.pop(user_id, [])
                for w_id in welcome_ids:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=w_id)
                    except Exception:
                        pass
                
                text = (
                    _get_breadcrumbs(data, 1) +
                    f"<b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                    f"Berhasil mengunduh <code>{jumlah_file}</code> berkas VCF.\n"
                    f"Total kontak terdeteksi: <code>{jumlah_kontak}</code> baris.\n\n"
                    f"Silakan pilih tindakan di bawah:"
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
                
                # Kirim pesan baru di paling bawah di bawah berkas yang baru dikirim
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
        import logging
        logging.getLogger(__name__).error("Debounce notify error in vcftotxt: %s", e)


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
    v2t_dir = os.path.join(user_dir, "vcftotxt")
    shutil.rmtree(v2t_dir, ignore_errors=True)


async def cmd_vcftotxt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0, "total_contacts": 0})
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _waiting_text({"count": 0}),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])


async def handle_vcftotxt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    sess = db.get_session(user_id)
    if sess["state"] != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".vcf"):
        # Edit status message di atas, jangan kirim pesan baru
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                sent_name = doc.file_name if doc and doc.file_name else "file tersebut"
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        f"\u26a0\ufe0f <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{sent_name}</code> bukan berkas <code>.vcf</code>.\n\n"
                        f"Kirim ulang berkas dengan format <code>.vcf</code>."
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
    user_dir = get_user_dir(user_id)
    v2t_dir = os.path.join(user_dir, "vcftotxt")
    os.makedirs(v2t_dir, exist_ok=True)
    
    orig_name = doc.file_name if doc.file_name else f"{msg_id}.vcf"
    safe_name = sanitize_filename(orig_name)
    out_path = os.path.join(v2t_dir, f"{msg_id}____{safe_name}")
    
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
            if sess["state"] != STATE:
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
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=(
                                _get_breadcrumbs(data, 1) +
                                f"\u26a0\ufe0f <b>Batas sesi: <code>{MAX_FILES} file</code> telah tercapai.</b>\n"
                                f"Klik <b>PROSES SEKARANG</b> untuk melanjutkan."
                            ),
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                                InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                            ]])
                        )
                    except Exception:
                        pass
                return

            if (data.get("total_size", 0) + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=(
                                _get_breadcrumbs(data, 1) +
                                f"\u26a0\ufe0f <b>Batas ukuran: <code>{MAX_SIZE_MB} MB</code> telah tercapai.</b>\n"
                                f"Klik <b>PROSES SEKARANG</b> untuk melanjutkan."
                            ),
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                                InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                            ]])
                        )
                    except Exception:
                        pass
                return

            data["count"] += 1
            data["total_size"] = data.get("total_size", 0) + doc.file_size
            data["total_contacts"] = data.get("total_contacts", 0) + c
            
            db.set_session(user_id, STATE, data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Download failed in vcftotxt: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


async def handle_vcftotxt_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE:
        return
    data = sess["data"]

    # Hapus input teks 'done' jika user mengetik manual
    if update.message and update.message.text in ("done", "selesai", "/done"):
        try:
            await update.message.delete()
        except Exception:
            pass

    if data["count"] == 0:
        return

    db.set_session(user_id, STATE_NAMING, data)
    
    status_msg_id = data.get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=(
            _get_breadcrumbs(data, 2) +
            f"<b>[ LANGKAH 2: NAMA FILE OUTPUT ]</b>\n"
            f"Terdeteksi: <code>{data['count']}</code> file VCF (<code>{data.get('total_contacts', 0)}</code> kontak).\n\n"
            f"Ketik nama file TXT output (contoh: <code>FEE</code>):"
        ),
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_vcftotxt_naming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_NAMING:
        return
    data = sess["data"]
    
    # Biarkan input teks user tetap di chat — tidak dihapus
        
    file_name = sanitize_filename(update.message.text.strip())
    if not file_name:
        file_name = "EXPORT"
        
    data["file_name"] = file_name
    db.set_session(user_id, STATE_DELIVERY, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="v2t_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="v2t_deliv_zip", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    status_msg_id = data.get("status_msg_id")
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=(
            _get_breadcrumbs(data, 3) +
            "<b>[ LANGKAH 3: FORMAT PENGIRIMAN ]</b>\n"
            "Pilih format pengiriman berkas TXT pada tombol di bawah:"
        ),
        parse_mode="HTML",
        reply_markup=deliv_keyboard
    )


async def handle_v2t_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_DELIVERY:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "v2t_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    db.set_session(user_id, STATE_PROCESSING, data)
    await handle_vcftotxt_process(update, context)


async def handle_v2t_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_vcftotxt_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import logging
    logger = logging.getLogger(__name__)

    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_PROCESSING:
        return
    data = dict(sess["data"])
    if data.get("is_processing"):
        return
    data["is_processing"] = True
    db.set_session(user_id, STATE_PROCESSING, data)

    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    file_name = data.get("file_name", "EXPORT")
    total_files = data["count"]
    status_msg_id = data.get("status_msg_id")

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memproses dan mengurai data VCF...",
            parse_mode="HTML"
        )
    except Exception:
        pass

    user_dir = get_user_dir(user_id)
    v2t_dir  = os.path.join(user_dir, "vcftotxt")

    files = []
    if os.path.exists(v2t_dir):
        raw_files = [f for f in os.listdir(v2t_dir) if f.lower().endswith(".vcf")]
        
        def extract_msg_id(f_name):
            try:
                return int(f_name.split("____")[0])
            except (ValueError, IndexError):
                return 0
            
        files = sorted(raw_files, key=extract_msg_id)

    loop = asyncio.get_running_loop()

    def parse_one_vcf(fname):
        path = os.path.join(v2t_dir, fname)
        try:
            contacts = parse_vcf_file(path)
            return [c["tel"] for c in contacts]
        except Exception as e:
            logger.error("vcftotxt parse error %s: %s", fname, e)
            return []

    def do_export_parallel():
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout
        results = {}

        with ThreadPoolExecutor(max_workers=min(8, len(files) or 1)) as pool:
            future_to_idx = {pool.submit(parse_one_vcf, f): i for i, f in enumerate(files)}
            try:
                for future in as_completed(future_to_idx, timeout=300):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception:
                        results[idx] = []
            except FutureTimeout:
                logger.error("ThreadPool timeout during vcftotxt - aborting")
                raise RuntimeError("Export timeout - file terlalu besar atau corrupt")

        results_files = []
        for i, fname in enumerate(files):
            nums = results.get(i, [])
            label = f"{file_name} {i+1}"
            content = "\n".join(nums).encode("utf-8")
            results_files.append((label, content))

        return results_files

    async def update_progress(pct: int):
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=f"<b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memproses <code>{total_files}</code> file VCF... <code>{pct}%</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    try:
        if total_files > 10:
            await update_progress(10)

        results_files = await loop.run_in_executor(None, do_export_parallel)

        await update_progress(90)

        total_created = len(results_files)

        if total_created == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="Gagal. Nomor tidak ditemukan.",
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
                    text="<b>[ SYSTEM: COMPRESSING ZIP ]</b>\nMengompresi file ke berkas ZIP...",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            
            import zipfile
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for label, content in results_files:
                    zip_file.writestr(f"{label}.txt", content)
            zip_buffer.seek(0)
            zip_filename = f"{file_name}.zip"

            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<b>[ SYSTEM: SENDING ZIP ]</b>\nMengirim berkas ZIP ke Telegram...",
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
                    logger.error(f"[V2T] Gagal kirim ZIP attempt {attempt+1}: {ex}")
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
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_vcftotxt_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            total_contacts = data.get("total_contacts", 0)
            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {total_files} VCF\n"
                f"• Berkas Output : {total_created} TXT (ZIP)\n"
                f"• Nama File TXT : {file_name}\n"
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
            # Mode "single" menggunakan sequential sending
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=f"<b>[ SYSTEM: SENDING TXT ]</b>\nMengirim berkas TXT: <code>0</code> / <code>{total_created}</code> file terkirim.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            sent_count = 0
            from telegram.error import RetryAfter
            from config import SEND_PROGRESS_INTERVAL, SEND_BATCH_SIZE

            for idx, (label, content) in enumerate(results_files):
                buf = io.BytesIO(content)
                buf.name = f"{label}.txt"

                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        buf.seek(0)
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=buf,
                            filename=f"{label}.txt",
                            read_timeout=FILE_READ_TIMEOUT,
                            write_timeout=FILE_WRITE_TIMEOUT,
                            connect_timeout=FILE_CONNECT_TIMEOUT,
                        )
                        sent_count += 1
                        try:
                            await context.bot.edit_message_text(
                                chat_id=update.effective_chat.id,
                                message_id=status_msg_id,
                                text=f"<b>[ SYSTEM: SENDING TXT ]</b>\nMengirim berkas TXT: <code>{sent_count}</code> / <code>{total_created}</code> file terkirim.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                        break
                    except RetryAfter as e:
                        wait_secs = max(int(e.retry_after), 2) + 1
                        logger.warning(f"[V2T] Flood limit sekuensial, tunggu {wait_secs}s")
                        await asyncio.sleep(wait_secs)
                    except Exception as ex:
                        logger.error(f"[V2T] Gagal kirim file TXT sekuensial {label} attempt {attempt+1}: {ex}")
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += 1
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)



                if sent_count < total_created:
                    if sent_count % SEND_BATCH_SIZE == 0:
                        await asyncio.sleep(SEND_BATCH_DELAY)
                    else:
                        await asyncio.sleep(SEND_FILE_DELAY)

            # Hapus status message lama
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_vcftotxt_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            total_contacts = data.get("total_contacts", 0)
            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {total_files} VCF\n"
                f"• Berkas Output : {total_created} TXT\n"
                f"• Nama File TXT : {file_name}\n"
                f"• Total Kontak : {total_contacts:,}</blockquote>\n\n"
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

    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)


async def handle_show_vcftotxt_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (VCF to TXT)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0, "total_contacts": 0})

    init_data = {"count": 0, "total_size": 0, "total_contacts": 0}
    text = _waiting_text(init_data)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    try:
        await query.message.edit_text(text=text, parse_mode="HTML", reply_markup=keyboard)
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, STATE, sess["data"])
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
        db.set_session(user_id, STATE, sess["data"])