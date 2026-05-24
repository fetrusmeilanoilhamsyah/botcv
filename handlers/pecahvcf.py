"""
pecahvcf.py — Disk-based approach to prevent OOM, with batch support.
"""
import os
import io
import shutil
import asyncio
import logging
from telegram import Update, InputMediaDocument
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
    
    from handlers.start import delete_welcome_messages
    await delete_welcome_messages(context.bot, user_id, update.effective_chat.id)

    _cancel_timer(user_id)
    db.set_session(user_id, STATE_PER_FILE, {})
    from handlers.start import get_start_keyboard
    await update.message.reply_text(
        "✂️ <b>Pecah VCF</b>\n\n"
        "Berapa kontak per file?\n"
        "<i>Contoh: 50</i>",
        parse_mode="HTML",
        reply_markup=get_start_keyboard()
    )


async def handle_pecah_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_PER_FILE:
        return

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Masukkan angka yang valid. Contoh: 50")
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
        f"✅ Kontak per file: <b>{per_file}</b>\n\n"
        f"Sekarang kirim file VCF (SATU VCF PER SESI).",
        parse_mode="HTML",
        reply_markup=get_start_keyboard()
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
        f"⏳ Memproses <b>{doc.file_name}</b>...",
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
                f"📤 Mengirim <b>{total_parts}</b> file pecahan...",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Kirim file hasil dalam batch 10
        BATCH = 10
        for i in range(0, total_parts, BATCH):
            batch = output_files[i:i + BATCH]
            bio_list = []
            media_group = []

            for out_path in batch:
                with open(out_path, "rb") as fd:
                    buf = io.BytesIO(fd.read())
                fname = os.path.basename(out_path)
                buf.name = fname
                bio_list.append(buf)
                media_group.append(InputMediaDocument(media=buf, filename=fname))

            async def _send(_mg=media_group, _bl=bio_list, _bt=batch):
                if len(_mg) == 1:
                    _bl[0].seek(0)
                    await update.message.reply_document(
                        document=_bl[0],
                        filename=os.path.basename(_bt[0]),
                        read_timeout=120, write_timeout=120, connect_timeout=60,
                    )
                else:
                    for b in _bl:
                        b.seek(0)
                    await update.message.reply_media_group(
                        media=_mg,
                        read_timeout=120, write_timeout=120, connect_timeout=60,
                    )

            try:
                await _send()
            except RetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 2)
                try:
                    await _send()
                except Exception as e2:
                    logger.error("PecahVCF kirim ulang gagal: %s", e2)
            except Exception as e:
                logger.error("PecahVCF send error: %s", e)

        # Hapus pesan "mengirim..." dan ganti dengan ringkasan
        try:
            await status_msg.delete()
        except Exception:
            pass

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_pecahvcf_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")
            ]
        ])
        await update.message.reply_text(
            f"✅ <b>Selesai dipecah!</b>\n"
            f"{'─' * 20}\n"
            f"📋 Total kontak  : <b>{total_contacts:,}</b>\n"
            f"✂️  Kontak/file   : <b>{per_file}</b>\n"
            f"📁 File dihasilkan: <b>{total_parts}</b>\n"
            f"{'─' * 20}",
            parse_mode="HTML",
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
    try:
        await query.message.delete()
    except Exception:
        pass
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    db.set_session(user_id, STATE_PER_FILE, {})
    from handlers.start import get_start_keyboard
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "✂️ <b>Pecah VCF</b>\n\n"
            "Berapa kontak per file?\n"
            "<i>Contoh: 50</i>"
        ),
        parse_mode="HTML",
        reply_markup=get_start_keyboard()
    )
