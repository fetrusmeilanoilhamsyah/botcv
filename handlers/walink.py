"""
handlers/walink.py — Pembuat tautan WhatsApp instan dalam format Excel (.xlsx) premium.
"""
import os
import io
import shutil
import asyncio
import logging
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from telegram import Update, InputMediaDocument, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, add_plus
import re

logger = logging.getLogger(__name__)

STATE = "WALINK_WAIT_FILE"
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


async def cmd_walink(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Link WhatsApp akan dibuat dalam format Excel.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )


async def handle_walink_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        await update.message.reply_text("Kirim file dokumen .txt atau .vcf.")
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
    work_dir = os.path.join(user_dir, f"walink_{doc.file_id}")
    os.makedirs(work_dir, exist_ok=True)
    input_path = os.path.join(work_dir, f"input{ext}")

    try:
        # Download file
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive(input_path)

        loop = asyncio.get_running_loop()

        def do_process_xlsx():
            contacts = []
            seen = set()

            if ext == ".vcf":
                parsed = parse_vcf_file(input_path)
                for c in parsed:
                    clean = _clean_number(c["tel"])
                    if clean and clean not in seen:
                        seen.add(clean)
                        contacts.append({"name": c["name"], "tel": clean})
            else:
                # TXT file
                with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [line.strip() for line in f if line.strip()]
                
                # Saring nomor HP via regex
                phone_re = re.compile(r'\+?(?:\d[\s\-\(\)\.]*){8,16}')
                counter = 1
                for line in lines:
                    matches = phone_re.findall(line)
                    for m in matches:
                        clean = _clean_number(m)
                        if clean and clean not in seen:
                            seen.add(clean)
                            contacts.append({"name": f"Kontak {counter}", "tel": clean})
                            counter += 1

            # Membuat Workbook Excel
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "WhatsApp Links"

            # Style Header
            header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
            header_align = Alignment(horizontal="center", vertical="center")

            headers = ["No", "Nama Kontak", "Nomor HP", "Link WhatsApp"]
            ws.append(headers)

            for col_num, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_num)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align

            # Isi Baris
            row_align_center = Alignment(horizontal="center", vertical="center")
            row_align_left = Alignment(horizontal="left", vertical="center")
            
            for idx, c in enumerate(contacts, 1):
                wa_link = f"https://wa.me/{c['tel']}"
                ws.append([idx, c["name"], c["tel"], wa_link])
                
                # Formatting link biar bisa langsung diklik sebagai hyperlink di excel
                cell_link = ws.cell(row=idx+1, column=4)
                cell_link.hyperlink = wa_link
                cell_link.font = Font(name="Arial", size=10, color="0000FF", underline="single")
                
                # Alignment
                ws.cell(row=idx+1, column=1).alignment = row_align_center
                ws.cell(row=idx+1, column=2).alignment = row_align_left
                ws.cell(row=idx+1, column=3).alignment = row_align_center
                ws.cell(row=idx+1, column=4).alignment = row_align_left

            # Atur Lebar Kolom secara otomatis
            for col in ws.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                col_letter = openpyxl.utils.get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            return len(contacts), buf

        total_links, excel_buf = await loop.run_in_executor(None, do_process_xlsx)

        # Hapus loading status
        try:
            await status_msg.delete()
        except Exception:
            pass

        if total_links == 0:
            await update.message.reply_text("Tidak ada nomor HP valid yang ditemukan dalam file.")
            return

        # Kirim file Excel kembali ke user
        base_name = os.path.splitext(doc.file_name)[0]
        out_name = f"WA_LINKS_{base_name}.xlsx"
        excel_buf.name = out_name

        await update.message.reply_document(
            document=excel_buf,
            filename=out_name,
            caption=(
                f"WhatsApp link berhasil dibuat.\n\n"
                f"Total nomor: <b>{total_links}</b>"
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
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_walink_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            from handlers.start import clear_welcome_messages
            clear_welcome_messages(uid)
            await bot.send_message(
                chat_id=chat_id,
                text="Proses selesai. Silakan unduh file Excel di atas.",
                reply_markup=keyboard
            )

        task = asyncio.get_event_loop().create_task(_send_buttons_debounced(user_id, update.effective_chat.id, context.bot))
        _button_timers[user_id] = task

    except Exception as e:
        logger.error("Error di walink: %s", e, exc_info=True)
        try:
            await status_msg.edit_text("Terjadi kesalahan saat memproses file. Coba lagi.")
        except Exception:
            pass
    finally:
        _processing.discard(user_id)
        shutil.rmtree(work_dir, ignore_errors=True)


async def handle_show_walink_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db.set_session(user_id, STATE, {})

    try:
        await query.message.edit_text(
            text="Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Link WhatsApp akan dibuat dalam format Excel.",
            parse_mode="HTML"
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim file <b>.TXT</b> atau <b>.VCF</b> sekarang. Link WhatsApp akan dibuat dalam format Excel.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
