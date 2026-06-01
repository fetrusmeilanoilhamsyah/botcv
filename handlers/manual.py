"""
handlers/manual.py — Fitur pembuatan kontak secara manual (TXT, VCF, EXCEL).
Mendukung input teks manual dari berbagai negara, tanpa emoji, dan dengan UI premium.
"""
import os
import io
import re
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill

from database import db
from database.db_async import adb
from middleware.auth import require_member
from core.vcf_parser import add_plus
from core.utils import sanitize_filename

logger = logging.getLogger(__name__)

# States untuk in-memory session
S_WAIT_TEXT = "MANUAL_WAIT_TEXT"
S_WAIT_FORMAT = "MANUAL_WAIT_FORMAT"
S_WAIT_CONTACTNAME = "MANUAL_WAIT_CONTACTNAME"
S_WAIT_FILENAME = "MANUAL_WAIT_FILENAME"

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


def extract_numbers_from_text(text: str) -> list[str]:
    """Pecah teks berdasarkan baris, koma, titik koma, pipa, atau tab.
    Secara cerdas menghapus awalan nomor urut / bullets agar nomor HP tidak rusak."""
    if not text:
        return []
        
    segments = re.split(r'[\n\r,;|\t]+', text)
    clean_numbers = []
    seen = set()
    
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        
        # Bersihkan awalan penomoran seperti "1. ", "2) ", "- ", "* ", "[1] ", "No. 1: "
        seg = re.sub(r'^(?:\d+[\.\)\:\s\-]+|[\-\*\+\•]\s*|no\.\s*\d+\s*[\.\:\-]*\s*)', '', seg, flags=re.IGNORECASE).strip()
        if not seg:
            continue
        
        # Ekstrak nomor
        has_plus = seg.startswith("+")
        digits = re.sub(r'\D', '', seg)
        if not digits:
            continue
        
        cleaned = ("+" if has_plus else "") + digits
        
        # Batasan panjang nomor telepon standar internasional (7 sampai 17 karakter)
        if 7 <= len(cleaned) <= 17:
            if cleaned not in seen:
                seen.add(cleaned)
                clean_numbers.append(cleaned)
                
    return clean_numbers


async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler Command /manual"""
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    
    db.set_session(user_id, S_WAIT_TEXT, {"numbers": []})
    
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Kirim daftar nomor HP secara manual sekarang.\n(Satu nomor per baris atau dipisahkan dengan spasi/koma)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )


async def handle_manual_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler ketika user mengirim teks berisi daftar nomor HP"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != S_WAIT_TEXT:
        return

    text = update.message.text
    if not text:
        await update.message.reply_text("Kirim daftar nomor HP dalam bentuk teks.")
        return

    clean_numbers = extract_numbers_from_text(text)

    if not clean_numbers:
        await update.message.reply_text("Tidak ditemukan nomor HP yang valid. Silakan kirimkan kembali daftar nomor HP.")
        return

    data = sess["data"]
    data["numbers"] = clean_numbers
    db.set_session(user_id, S_WAIT_FORMAT, data)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("TXT", callback_data="manual_fmt_txt", style="primary"),
            InlineKeyboardButton("VCF", callback_data="manual_fmt_vcf", style="primary"),
            InlineKeyboardButton("EXCEL", callback_data="manual_fmt_excel", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])

    await update.message.reply_text(
        text=f"Ditemukan <b>{len(clean_numbers)}</b> nomor HP unik.\n\nPilih format output:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_manual_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler ketika user memilih format output"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != S_WAIT_FORMAT:
        return

    fmt = query.data.split("_")[-1] # txt, vcf, excel
    data = sess["data"]
    data["format"] = fmt

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if fmt in ("txt", "excel"):
        db.set_session(user_id, S_WAIT_FILENAME, data)
        await query.edit_message_text(
            text=f"Masukkan nama file untuk hasil {fmt.upper()}:",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    elif fmt == "vcf":
        db.set_session(user_id, S_WAIT_CONTACTNAME, data)
        await query.edit_message_text(
            text="Masukkan nama kontak untuk hasil VCF:",
            parse_mode="HTML",
            reply_markup=keyboard
        )


async def handle_manual_contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler VCF: User memasukkan nama kontak"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != S_WAIT_CONTACTNAME:
        return

    contact_name = update.message.text.strip()
    if not contact_name:
        await update.message.reply_text("Masukkan nama kontak yang valid.")
        return

    data = sess["data"]
    data["contact_name"] = contact_name
    db.set_session(user_id, S_WAIT_FILENAME, data)

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    await update.message.reply_text(
        text=f"Masukkan nama file untuk hasil VCF:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_manual_file_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler TXT/EXCEL/VCF: User memasukkan nama file"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != S_WAIT_FILENAME:
        return

    filename = sanitize_filename(update.message.text.strip())
    if not filename:
        await update.message.reply_text("Masukkan nama file yang valid.")
        return

    data = sess["data"]
    data["file_name"] = filename

    db.clear_session(user_id)
    await handle_manual_process(update, context, data)


async def handle_manual_process(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """Proses pembuatan dan pengiriman file"""
    user_id = update.effective_user.id
    fmt = data.get("format")
    numbers = data.get("numbers", [])
    file_name = data.get("file_name", "kontak")

    status_msg = await update.message.reply_text("Memproses...")

    try:
        loop = asyncio.get_running_loop()

        if fmt == "txt":
            # Generate TXT
            def do_txt():
                content = ("\n".join(numbers) + "\n").encode("utf-8")
                return content
            
            content = await loop.run_in_executor(None, do_txt)
            
            buf = io.BytesIO(content)
            buf.name = f"{file_name}.txt"

            await status_msg.delete()
            await update.message.reply_document(
                document=buf,
                filename=f"{file_name}.txt",
                caption=f"Proses selesai.\nTotal: <b>{len(numbers)}</b> nomor HP.",
                parse_mode="HTML"
            )

        elif fmt == "excel":
            # Generate Excel
            def do_excel():
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Daftar Kontak"

                # Style Header
                header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
                header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
                header_align = Alignment(horizontal="center", vertical="center")

                headers = ["No", "Nama Kontak", "Nomor HP"]
                ws.append(headers)

                for col_num, header in enumerate(headers, 1):
                    cell = ws.cell(row=1, column=col_num)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = header_align

                # Isi data
                row_align_center = Alignment(horizontal="center", vertical="center")
                row_align_left = Alignment(horizontal="left", vertical="center")

                for idx, num in enumerate(numbers, 1):
                    ws.append([idx, f"Kontak {idx}", num])
                    ws.cell(row=idx+1, column=1).alignment = row_align_center
                    ws.cell(row=idx+1, column=2).alignment = row_align_left
                    ws.cell(row=idx+1, column=3).alignment = row_align_center

                # Atur lebar kolom otomatis
                for col in ws.columns:
                    max_len = max(len(str(cell.value or '')) for cell in col)
                    col_letter = openpyxl.utils.get_column_letter(col[0].column)
                    ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

                buf = io.BytesIO()
                wb.save(buf)
                buf.seek(0)
                return buf.getvalue()

            content = await loop.run_in_executor(None, do_excel)
            buf = io.BytesIO(content)
            buf.name = f"{file_name}.xlsx"

            await status_msg.delete()
            await update.message.reply_document(
                document=buf,
                filename=f"{file_name}.xlsx",
                caption=f"Proses selesai.\nTotal: <b>{len(numbers)}</b> nomor HP.",
                parse_mode="HTML"
            )

        elif fmt == "vcf":
            contact_name = data.get("contact_name", "FEE")

            def do_vcf():
                vcf_lines = []
                for idx, num in enumerate(numbers, 1):
                    name = f"{contact_name}{idx}"
                    vcf_lines.append(f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL;TYPE=CELL:{add_plus(num)}\nEND:VCARD")
                
                content = ("\n".join(vcf_lines) + "\n").encode("utf-8")
                return content

            content = await loop.run_in_executor(None, do_vcf)

            buf = io.BytesIO(content)
            buf.name = f"{file_name}.vcf"

            await status_msg.delete()
            await update.message.reply_document(
                document=buf,
                filename=f"{file_name}.vcf",
                caption=f"Proses selesai.\nTotal: <b>{len(numbers)}</b> kontak.",
                parse_mode="HTML"
            )

        # Keyboard selesai
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES MANUAL LAIN", callback_data="show_manual_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        await update.message.reply_text("Silakan pilih tindakan:", reply_markup=keyboard)

    except Exception as e:
        logger.error("Error di proses manual: %s", e, exc_info=True)
        try:
            await status_msg.edit_text("Terjadi kesalahan saat memproses data. Coba lagi.")
        except Exception:
            pass


async def handle_show_manual_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES MANUAL LAIN"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    db.set_session(user_id, S_WAIT_TEXT, {"numbers": []})

    try:
        await query.message.edit_text(
            text="Kirim daftar nomor HP secara manual sekarang.\n(Satu nomor per baris atau dipisahkan dengan spasi/koma)"
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Kirim daftar nomor HP secara manual sekarang.\n(Satu nomor per baris atau dipisahkan dengan spasi/koma)",
            reply_markup=ReplyKeyboardRemove()
        )
