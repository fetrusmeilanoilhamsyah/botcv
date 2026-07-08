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


def cleanup_inactive_users(inactive_ids: list) -> int:
    cleaned = 0
    for uid in inactive_ids:
        _processing.discard(uid)
        task = _button_timers.pop(uid, None)
        if task:
            task.cancel()
        cleaned += 1
    return cleaned


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


def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    parts = []
    if step == 1:
        parts.append(f"<b>» BERKAS: {count} FILE «</b>" if count else "<b>» BERKAS «</b>")
    else:
        parts.append(f"Berkas: {count} file" if count else "Berkas ○")
        
    if step == 2:
        parts.append("<b>» EXCEL «</b>")
    else:
        parts.append("Excel ○")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ WALINK EXCEL CV ]</b>\n"
        "────────────────────────────\n"
        f"{breadcrumbs}\n"
        "────────────────────────────\n\n"
    )


async def cmd_walink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    db.set_session(user_id, STATE, {"count": 0, "total_size": 0})
    from handlers.start import transition_to_handler
    
    text = _get_breadcrumbs({"count": 0}, 1) + "<b>[ ➔ ] Menunggu berkas...</b>\nKirim file <b>.TXT</b> atau <b>.VCF</b> sekarang."
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])


async def handle_walink_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    doc = update.message.document
    try:
        await update.message.delete()
    except Exception:
        pass

    data = sess["data"]
    status_msg_id = data.get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not doc or not doc.file_name:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(data, 1) + "Kirim file dokumen .txt atau .vcf.",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
        return

    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".txt", ".vcf"):
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(data, 1) + "Format tidak didukung. Kirim file .TXT atau .VCF.",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
        return

    if user_id in _processing:
        return
    _processing.add(user_id)
    
    # Update status to processing
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<b>Memproses berkas...</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    user_dir = get_user_dir(user_id)
    work_dir = os.path.join(user_dir, f"walink_{doc.file_id}")

    try:
        os.makedirs(work_dir, exist_ok=True)
        input_path = os.path.join(work_dir, f"input{ext}")
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

        db.set_session(user_id, STATE, {
            "total_links": total_links,
            "file_name": doc.file_name
        })

        if total_links == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=_get_breadcrumbs(data, 1) + "Tidak ada nomor HP valid yang ditemukan dalam file.",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass
            db.clear_session(user_id)
            return

        # Kirim file Excel kembali ke user
        base_name = os.path.splitext(doc.file_name)[0]
        out_name = f"WA_LINKS_{base_name}.xlsx"
        excel_buf.name = out_name

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=excel_buf,
            filename=out_name
        )

        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass

        # Trigger debounced final keyboard
        old_timer = _button_timers.pop(user_id, None)
        if old_timer and not old_timer.done():
            old_timer.cancel()

        async def _send_buttons_debounced(uid, chat_id, bot):
            await asyncio.sleep(1.5)
            sess = db.get_session(uid)
            if not sess or sess.get("state") != STATE:
                return
            s_data = sess["data"]
            t_links = s_data.get("total_links", 0)
            fname = s_data.get("file_name", "")

            keyboard_done = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_walink_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])

            def _fit(val, max_len=22) -> str:
                s = str(val)
                if len(s) > max_len:
                    return s[:max_len-3] + "..."
                return s

            from handlers.start import clear_welcome_messages, register_welcome_messages
            clear_welcome_messages(uid)

            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• File Input : {fname}\n"
                f"• Berkas Output : 1 EXCEL (.xlsx)\n"
                f"• Total Link WA : {t_links:,}</blockquote>\n\n"
                f"<i>Pembuatan WA Link Excel selesai! Silakan unduh file di atas.</i>"
            )
            final_msg = await bot.send_message(
                chat_id=chat_id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard_done
            )
            register_welcome_messages(uid, [final_msg.message_id])
            db.clear_session(uid)

        task = asyncio.create_task(_send_buttons_debounced(user_id, update.effective_chat.id, context.bot))
        _button_timers[user_id] = task

    except Exception as e:
        logger.error("Error di walink: %s", e, exc_info=True)
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Terjadi kesalahan saat memproses file. Coba lagi.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        _processing.discard(user_id)
        shutil.rmtree(work_dir, ignore_errors=True)


async def handle_show_walink_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db.set_session(user_id, STATE, {"count": 0, "total_size": 0})

    text = _get_breadcrumbs({"count": 0}, 1) + "<b>[ ➔ ] Menunggu berkas...</b>\nKirim file <b>.TXT</b> atau <b>.VCF</b> sekarang."

    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, STATE, sess["data"])
