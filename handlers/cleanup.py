"""
handlers/cleanup.py — Pembersih dan standardisasi nomor HP secara otomatis (VCF ke VCF, TXT ke TXT).
"""
import os
import io
import shutil
import asyncio
import logging
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf
import re

logger = logging.getLogger(__name__)

STATE = "CLEANUP_WAIT_FILE"
_processing: set[int] = set()
_button_timers: dict[int, asyncio.Task] = {}


def _clean_number(num: str) -> str:
    """Bersihkan nomor ke format standard 628xxx."""
    digits = re.sub(r'\D', '', num)
    if digits.startswith("08"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    if 9 <= len(digits) <= 15:
        return digits
    return ""


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    db.set_session(user_id, STATE, {})
    from handlers.start import transition_to_handler
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Format nomor akan dibersihkan dan distandardisasi.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )


async def handle_cleanup_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        await update.message.reply_text("Kirim file dokumen .txt or .vcf.")
        return

    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".txt", ".vcf"):
        await update.message.reply_text("Format tidak didukung. Kirim file .TXT atau .VCF.")
        return

    if user_id in _processing:
        return
    _processing.add(user_id)
    db.clear_session(user_id)

    status_msg = await update.message.reply_text(
        f"Memproses <b>{doc.file_name}</b>...",
        parse_mode="HTML"
    )

    user_dir = get_user_dir(user_id)
    work_dir = os.path.join(user_dir, f"cleanup_{doc.file_id}")

    try:
        os.makedirs(work_dir, exist_ok=True)
        input_path = os.path.join(work_dir, f"input{ext}")
        # Download file
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive(input_path)

        loop = asyncio.get_running_loop()

        def do_cleanup():
            total_awal = 0
            clean_contacts = []
            seen = set()

            if ext == ".vcf":
                parsed = parse_vcf_file(input_path)
                total_awal = len(parsed)
                for c in parsed:
                    clean = _clean_number(c["tel"])
                    if clean and clean not in seen:
                        seen.add(clean)
                        clean_contacts.append({"name": c["name"], "tel": clean})
                
                content = contacts_to_vcf(clean_contacts).encode("utf-8")
            else:
                # TXT file
                with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [line.strip() for line in f if line.strip()]
                
                # Saring nomor HP via regex
                phone_re = re.compile(r'\+?(?:\d[\s\-\(\)\.]*){8,16}')
                raw_numbers = []
                for line in lines:
                    matches = phone_re.findall(line)
                    raw_numbers.extend(matches)
                
                total_awal = len(raw_numbers)
                clean_numbers = []
                for num in raw_numbers:
                    clean = _clean_number(num)
                    if clean and clean not in seen:
                        seen.add(clean)
                        clean_numbers.append(clean)
                
                content = ("\n".join(clean_numbers) + "\n").encode("utf-8")

            total_clean = len(seen)
            total_dibuang = total_awal - total_clean
            return total_awal, total_clean, total_dibuang, content

        total_awal, total_clean, total_dibuang, cleaned_content = await loop.run_in_executor(None, do_cleanup)

        # Hapus loading status
        try:
            await status_msg.delete()
        except Exception:
            pass

        if total_clean == 0:
            await update.message.reply_text("Tidak ada nomor HP valid yang ditemukan dalam file.")
            return

        # Kirim berkas bersih
        out_name = f"CLEAN_{doc.file_name}"
        buf = io.BytesIO(cleaned_content)
        buf.name = out_name

        await update.message.reply_document(
            document=buf,
            filename=out_name,
            caption=(
                f"Pembersihan selesai.\n\n"
                f"Total awal: <b>{total_awal}</b>\n"
                f"Valid & Unik: <b>{total_clean}</b>\n"
                f"Dibuang: <b>{total_dibuang}</b>"
            ),
            parse_mode="HTML"
        )

        # Trigger debounced final keyboard
        old_timer = _button_timers.pop(user_id, None)
        if old_timer and not old_timer.done():
            old_timer.cancel()

        async def _send_buttons_debounced(uid, chat_id, bot):
            await asyncio.sleep(1.5)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_cleanup_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            from handlers.start import clear_welcome_messages
            clear_welcome_messages(uid)
            await bot.send_message(
                chat_id=chat_id,
                text="Proses selesai. Silakan unduh file bersih di atas.",
                reply_markup=keyboard
            )

        task = asyncio.create_task(_send_buttons_debounced(user_id, update.effective_chat.id, context.bot))
        _button_timers[user_id] = task

    except Exception as e:
        logger.error("Error di cleanup: %s", e, exc_info=True)
        try:
            await status_msg.edit_text("Terjadi kesalahan saat memproses file. Coba lagi.")
        except Exception:
            pass
    finally:
        _processing.discard(user_id)
        shutil.rmtree(work_dir, ignore_errors=True)


async def handle_show_cleanup_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db.set_session(user_id, STATE, {})

    try:
        await query.message.edit_text(
            text="Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Format nomor akan dibersihkan dan distandardisasi.",
            parse_mode="HTML"
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Format nomor akan dibersihkan dan distandardisasi.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
