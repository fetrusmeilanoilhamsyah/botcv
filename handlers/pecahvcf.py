"""
pecahvcf.py — Disk-based approach to prevent OOM, with batch support.
"""
import os
import io
import shutil
import asyncio
import logging
from telegram import Update, InputMediaDocument, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf
from config import MAX_CONTACTS_PER_FILE

logger = logging.getLogger(__name__)

STATE_PER_FILE = "PECAH_PER_FILE"
STATE_WAIT_VCF = "PECAH_WAIT_VCF"

_user_timers: dict = {}
_processing: set[int] = set()  # user yang sedang diproses, tolak file kedua


def _cancel_timer(user_id: int):
    timer = _user_timers.pop(user_id, None)
    if timer:
        timer.cancel()


def cleanup_inactive_users(inactive_ids: list) -> int:
    """Dipanggil oleh job cleanup di main.py."""
    for uid in inactive_ids:
        _cancel_timer(uid)
        _processing.discard(uid)
    return len(inactive_ids)


async def cmd_pecahvcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler, get_start_keyboard
    _cancel_timer(user_id)
    db.set_session(user_id, STATE_PER_FILE, {})
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Berapa kontak per file? Contoh: <b>100</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )



async def handle_pecah_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(
            f"Masukkan angka antara 1 hingga {MAX_CONTACTS_PER_FILE:,}."
        )
        return

    db.set_session(user_id, STATE_WAIT_VCF, {"per_file": per_file})
    from handlers.start import get_start_keyboard
    await update.message.reply_text(
        f"Oke, {per_file} kontak per file. Kirim file <b>.VCF</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    )


async def handle_pecah_vcf_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_WAIT_VCF:
        return

    per_file = sess["data"]["per_file"]

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".vcf"):
        await update.message.reply_text("Kirim file dengan ekstensi .vcf.")
        return

    # Tolak jika sudah ada yang diproses
    if user_id in _processing:
        return

    _processing.add(user_id)
    # Hapus session sekarang — file berikutnya yang datang bersamaan langsung diabaikan
    db.clear_session(user_id)

    # Pesan feedback langsung — user tahu bot sedang proses
    status_msg = await update.message.reply_text(
        f"Memproses <b>{doc.file_name}</b>...",
        parse_mode="HTML",
    )

    user_dir = get_user_dir(user_id)
    pecah_dir = os.path.join(user_dir, f"pecah_{doc.file_id}")
    os.makedirs(pecah_dir, exist_ok=True)
    input_path = os.path.join(pecah_dir, "input.vcf")

    try:
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive(input_path)

        loop = asyncio.get_running_loop()

        def process_pecah():
            contacts = parse_vcf_file(input_path)
            output_files = []
            for idx, i in enumerate(range(0, len(contacts), per_file), start=1):
                chunk = contacts[i:i + per_file]
                out_path = os.path.join(pecah_dir, f"PECAHAN{idx}.vcf")
                with open(out_path, "w", encoding="utf-8") as f_out:
                    f_out.write(contacts_to_vcf(chunk))
                output_files.append(out_path)
            return len(contacts), output_files

        total_contacts, output_files = await loop.run_in_executor(None, process_pecah)
        total_parts = len(output_files)

        # Update pesan status
        try:
            await status_msg.edit_text(
                f"Mengirim <b>{total_parts}</b> file pecahan...",
                parse_mode="HTML",
            )
        except Exception:
            pass

        import time as _time
        _last_edit_time = 0.0

        async def _safe_edit_vcf(text):
            try:
                await status_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        # ── SINGLE DOCUMENT SEQUENTIAL SEND — Anti Lag HP ──
        max_retries = 3
        for file_idx, out_path in enumerate(output_files):
            # Throttle progress: maks 1x edit per 2 detik
            current_time = _time.time()
            if file_idx == 0 or file_idx == total_parts - 1 or (current_time - _last_edit_time >= 2.0):
                progress_pct = int(((file_idx + 1) / total_parts) * 100)
                asyncio.create_task(_safe_edit_vcf(f"Mengirim... {file_idx + 1}/{total_parts} file ({progress_pct}%)"))
                _last_edit_time = current_time

            with open(out_path, "rb") as fd:
                buf = io.BytesIO(fd.read())
            fname = os.path.basename(out_path)
            buf.name = fname

            for attempt in range(max_retries):
                try:
                    buf.seek(0)
                    await update.message.reply_document(
                        document=buf,
                        filename=fname,
                        read_timeout=15, write_timeout=20, connect_timeout=10
                    )
                    # Jeda 300ms antar file — agar setiap file masuk ke update batch
                    # Telegram iOS yang berbeda sehingga animasi muncul 1-per-1 konsisten
                    await asyncio.sleep(0.15)
                    break
                except RetryAfter as e:
                    wait_secs = max(int(e.retry_after), 2) + 1
                    logger.warning(f"[PecahVCF] Flood limit {fname}, tunggu {wait_secs}s")
                    await asyncio.sleep(wait_secs)
                except Exception as ex:
                    logger.error(f"[PecahVCF] Gagal kirim {fname}: {ex}")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(1)

        # Hapus pesan "mengirim..." dan ganti dengan ringkasan
        try:
            await status_msg.delete()
        except Exception:
            pass

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_pecahvcf_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        await update.message.reply_text(
            f"Total kontak: <b>{total_contacts:,}</b>\n"
            f"Kontak per file: <b>{per_file}</b>\n"
            f"File dihasilkan: <b>{total_parts}</b>",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error("PecahVCF error: %s", e)
        try:
            await status_msg.edit_text("❌ Terjadi kesalahan saat memproses file. Coba kirim ulang.")
        except Exception:
            pass
    finally:
        _processing.discard(user_id)
        shutil.rmtree(pecah_dir, ignore_errors=True)


async def handle_show_pecahvcf_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Pecah VCF)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    db.set_session(user_id, STATE_PER_FILE, {})
    from handlers.start import get_start_keyboard

    # Edit the message in-place instead of deleting it to provide a smooth morphing transition
    try:
        await query.message.edit_text(
            text="Berapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML"
        )
    except Exception:
        # Fallback if editing fails
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Berapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )