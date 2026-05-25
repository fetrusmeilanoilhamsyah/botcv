"""
vcftotxt.py — Disk-based approach to prevent OOM.
"""
import os
import shutil
import asyncio
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file
from core.utils import sanitize_filename

STATE        = "VCF2TXT_COLLECTING"
STATE_NAMING = "VCF2TXT_NAMING"

from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
    THREAD_POOL_TIMEOUT
)

_user_locks: dict = {}
_user_timers: dict = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


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
                jumlah_file = sess["data"]["count"]
                jumlah_kontak = sess["data"].get("total_contacts", 0)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{jumlah_file} file diterima ({jumlah_kontak} kontak). Ketik /done jika sudah."
                )
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
    
    from handlers.start import transition_to_handler, get_start_keyboard
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0, "total_contacts": 0})
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Kirim file .VCF sekarang. Ketik /done jika sudah selesai.",
        reply_markup=ReplyKeyboardRemove(),
        update=update
    )



async def handle_vcftotxt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    sess = db.get_session(user_id)
    if sess["state"] != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".vcf"):
        await update.message.reply_text("Kirim file dengan ekstensi .vcf.")
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

        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if sess["state"] != STATE:
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
                await update.message.reply_text(f"Batas {MAX_FILES} file. Ketik /done.")
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if (data.get("total_size", 0) + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                await update.message.reply_text(f"Batas {MAX_SIZE_MB}MB. Ketik /done.")
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            # Hitung jumlah kontak (BEGIN:VCARD)
            c = 0
            try:
                with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "BEGIN:VCARD" in line.upper():
                            c += 1
            except Exception:
                pass

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
    if sess["state"] != STATE:
        return
    if sess["data"]["count"] == 0:
        await update.message.reply_text("Belum ada file yang dikirim.")
        return

    db.set_session(user_id, STATE_NAMING, sess["data"])
    from handlers.start import get_start_keyboard
    await update.message.reply_text(
        f"{sess['data']['count']} file, {sess['data'].get('total_contacts', 0)} kontak. Nama file TXT? Contoh: FEE",
        reply_markup=ReplyKeyboardRemove()
    )


async def handle_vcftotxt_naming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import logging
    logger = logging.getLogger(__name__)

    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != STATE_NAMING:
        return
    data = dict(sess["data"])
    if data.get("is_processing"):
        return
    data["is_processing"] = True
    db.set_session(user_id, STATE_NAMING, data)

    file_name   = sanitize_filename(update.message.text.strip())
    total_files = data["count"]

    progress_msg = await update.message.reply_text(
        f"Memproses {total_files} file... 0%"
    )

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
            await progress_msg.edit_text(f"Memproses {total_files} file... {pct}%")
        except Exception:
            pass

    try:
        if total_files > 10:
            await update_progress(10)

        results_files = await loop.run_in_executor(None, do_export_parallel)

        await update_progress(90)

        import io
        from telegram import InputMediaDocument
        from telegram.error import RetryAfter
        
        chunk_size = 10
        total_created = len(results_files)

        if total_created == 0:
            await progress_msg.edit_text("Gagal. Nomor tidak ditemukan.")
            return

        for i in range(0, total_created, chunk_size):
            chunk = results_files[i:i + chunk_size]

            media_group = []
            bio_list    = []
            for label, content in chunk:
                buf = io.BytesIO(content)
                buf.name = f"{label}.txt"
                bio_list.append(buf)
                media_group.append(InputMediaDocument(media=buf, filename=f"{label}.txt"))

            async def _send_v2t_chunk(_mg=media_group, _bl=bio_list, _ch=chunk):
                if len(_mg) == 1:
                    label_name, _ = _ch[0]
                    _bl[0].seek(0)
                    await update.message.reply_document(
                        document=_bl[0],
                        filename=f"{label_name}.txt",
                        read_timeout=120, connect_timeout=60, write_timeout=120
                    )
                else:
                    for b in _bl: b.seek(0)
                    await update.message.reply_media_group(
                        media=_mg,
                        read_timeout=120, connect_timeout=60, write_timeout=120
                    )

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await _send_v2t_chunk()
                    break  # Success, exit retry loop
                except RetryAfter as e:
                    if attempt == max_retries - 1:
                        logger.warning(f"Max retries reached for vcftotxt chunk {i//chunk_size + 1}")
                        raise
                    # Exponential backoff
                    wait_secs = int(e.retry_after) * (attempt + 1) + 2
                    logger.warning(f"vcftotxt flood limit! Retry {attempt+1}/{max_retries} after {wait_secs}s...")
                    await asyncio.sleep(wait_secs)
                except Exception as e:
                    logger.error(f"vcftotxt gagal kirim chunk {i//chunk_size + 1}: {e}")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(2)

            if i + chunk_size < total_created:
                await asyncio.sleep(0.5)

        try:
            await progress_msg.delete()
        except Exception:
            pass

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_vcftotxt_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")
            ]
        ])
        await update.message.reply_text(
            f"Proses selesai.\n"
            f"Total file VCF  : {total_files}\n"
            f"Total file TXT  : {total_created}\n"
            f"File dikirim    : {total_created} file",
            reply_markup=keyboard
        )

    finally:
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
    from handlers.start import get_start_keyboard

    try:
        await query.message.edit_text(
            text="Kirim file .VCF sekarang. Ketik /done jika sudah selesai."
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim file .VCF sekarang. Ketik /done jika sudah selesai.",
            reply_markup=ReplyKeyboardRemove()
        )