"""
pecahtxt.py — Pecah file TXT (nomor HP) menjadi beberapa file TXT kecil.
Mendukung banyak file sekaligus: kirim semua TXT, ketik /done, hasil dikirim.
"""
import os
import io
import shutil
import asyncio
import logging
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

STATE_PER_FILE   = "PECAHTXT_PER_FILE"
STATE_COLLECTING = "PECAHTXT_COLLECTING"

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
    return len(inactive_ids)


# ── Debounce notif saat file diterima ─────────────────────────────────────────

async def _debounce_notify(user_id: int, context, chat_id: int):
    try:
        await asyncio.sleep(1)
        if _user_timers.get(user_id) is asyncio.current_task():
            sess = db.get_session(user_id)
            if sess and sess.get("state") == STATE_COLLECTING:
                jumlah = sess["data"]["count"]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{jumlah} file diterima. Ketik /done jika sudah."
                )
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


# ── /pecahtxt ─────────────────────────────────────────────────────────────────

async def cmd_pecahtxt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, STATE_PER_FILE, {})
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Berapa nomor per file? Contoh: <b>100</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )


# ── Input: jumlah nomor per file ──────────────────────────────────────────────

async def handle_pecahtxt_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_PER_FILE:
        return

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Masukkan angka. Contoh: <b>100</b>")
        return

    per_file = int(text)
    if per_file < 1 or per_file > MAX_CONTACTS_PER_FILE:
        await update.message.reply_text(f"Masukkan angka antara 1 hingga {MAX_CONTACTS_PER_FILE:,}.")
        return

    db.set_session(user_id, STATE_COLLECTING, {"per_file": per_file, "count": 0, "total_size": 0})
    await update.message.reply_text(
        f"Oke, {per_file} nomor per file. Kirim file <b>.TXT</b> sekarang. Ketik /done jika sudah.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    )


# ── Input: file TXT (boleh banyak) ────────────────────────────────────────────

async def handle_pecahtxt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_COLLECTING:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Kirim file dengan ekstensi .txt.")
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
            if not sess or sess["state"] != STATE_COLLECTING:
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            data = sess["data"]
            if data["count"] >= MAX_FILES:
                await update.message.reply_text(f"Batas <b>{MAX_FILES}</b> file. Ketik /done.")
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                await update.message.reply_text(f"Batas <b>{MAX_SIZE_MB}MB</b>. Ketik /done.")
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return

            data["count"] += 1
            data["total_size"] += doc.file_size
            db.set_session(user_id, STATE_COLLECTING, data)

        _reset_timer(user_id, context, chat_id)

    except Exception as e:
        logger.error("PecahTXT download error: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


# ── /done handler ─────────────────────────────────────────────────────────────

async def handle_pecahtxt_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_COLLECTING:
        return

    data = sess["data"]
    if data["count"] == 0:
        await update.message.reply_text("Belum ada file yang dikirim.")
        return

    per_file = data["per_file"]
    status_msg = await update.message.reply_text("Memproses...")

    pecah_input_dir = os.path.join(get_user_dir(user_id), "pecahtxt", "input")
    pecah_out_dir   = os.path.join(get_user_dir(user_id), "pecahtxt", "output")
    os.makedirs(pecah_out_dir, exist_ok=True)

    db.clear_session(user_id)

    try:
        loop = asyncio.get_running_loop()

        def process():
            # Kumpulkan semua nomor dari semua file, urutkan berdasarkan message_id
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
            await status_msg.edit_text("Tidak ada nomor yang ditemukan dalam file.")
            return

        # ── SEQUENTIAL SEND FOR LOCAL API ──
        # Tanpa batching/delay, kirim secepat mungkin.
        # Update progress tiap 10 file untuk menghindari client choke / message queue overflow.
        for idx, out_path in enumerate(output_files):
            with open(out_path, "rb") as fd:
                buf = io.BytesIO(fd.read())
            fname = os.path.basename(out_path)
            buf.name = fname

            if idx % SEND_PROGRESS_INTERVAL == 0:
                progress_pct = int(((idx + 1) / total_parts) * 100)
                try:
                    await status_msg.edit_text(
                        f"Mengirim <b>{idx + 1} / {total_parts}</b> file ({progress_pct}%)",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            for attempt in range(SEND_MAX_RETRIES):
                try:
                    buf.seek(0)
                    await update.message.reply_document(
                        document=buf,
                        filename=fname,
                        read_timeout=FILE_READ_TIMEOUT,
                        write_timeout=FILE_WRITE_TIMEOUT,
                        connect_timeout=FILE_CONNECT_TIMEOUT
                    )
                    break
                except RetryAfter as e:
                    wait_secs = max(int(e.retry_after), 2) + 1
                    logger.warning(f"[PecahTXT] Flood limit {fname}, tunggu {wait_secs}s")
                    await asyncio.sleep(wait_secs)
                except Exception as ex:
                    logger.error(f"[PecahTXT] Gagal kirim {fname} attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(SEND_RETRY_DELAY)

            if (idx + 1) % SEND_BATCH_SIZE == 0:
                await asyncio.sleep(SEND_BATCH_DELAY)
            else:
                await asyncio.sleep(SEND_FILE_DELAY)

        # Update final setelah loop selesai
        try:
            await status_msg.edit_text(
                f"Mengirim <b>{total_parts} / {total_parts}</b> file (100%)",
                parse_mode="HTML"
            )
        except Exception:
            pass

        try:
            await status_msg.delete()
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
        await update.message.reply_text(
            f"Total nomor: <b>{total_nomor:,}</b>\n"
            f"Nomor per file: <b>{per_file}</b>\n"
            f"File dihasilkan: <b>{total_parts}</b>",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error("PecahTXT done error: %s", e)
        try:
            await status_msg.edit_text("Terjadi kesalahan. Coba kirim ulang.")
        except Exception:
            pass
    finally:
        _clear_buffers(user_id)


# ── Callback: PROSES FILE LAIN ────────────────────────────────────────────────

async def handle_show_pecahtxt_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, STATE_PER_FILE, {})

    try:
        await query.message.edit_text(text="Berapa nomor per file? Contoh: <b>100</b>")
    except Exception:
        try:
            await query.message.edit_text(text="Berapa nomor per file? Contoh: <b>100</b>")
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Berapa nomor per file? Contoh: <b>100</b>",
            reply_markup=ReplyKeyboardRemove()
        )
