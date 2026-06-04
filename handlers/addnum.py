"""
handlers/addnum.py — Fitur tambah nomor baru ke dalam file VCF yang sudah ada.
Mendukung pendeteksian otomatis format nama, kustomisasi nama/index urut awal,
dan pengiriman banyak nomor sekaligus dengan UI/UX premium.
"""
import os
import shutil
import asyncio
import io
import re
import logging
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf, add_plus
from core.utils import sanitize_filename
from handlers.manual import extract_numbers_from_text

logger = logging.getLogger(__name__)

# States untuk in-memory session
S0 = "ADDNUM_WAIT_FILE"
S1 = "ADDNUM_WAIT_NUMBERS"
S2 = "ADDNUM_WAIT_LABEL"

_user_locks: dict = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())

def cleanup_inactive_users(inactive_ids: list) -> int:
    cleaned = 0
    for uid in inactive_ids:
        _user_locks.pop(uid, None)
        cleaned += 1
    return cleaned

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict, step: int) -> str:
    filename = data.get("filename", "")
    total_contacts = data.get("total_contacts", 0)
    new_count = len(data.get("new_numbers", []))
    prefix = data.get("prefix", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>» BERKAS «</b>")
    else:
        parts.append(f"Berkas: {_fit(filename)}" if filename else "Berkas ○")
        
    if step == 2:
        parts.append(f"<b>» NOMOR BARU: {new_count} «</b>")
    elif step > 2:
        parts.append(f"Nomor Baru: {new_count}")
    else:
        parts.append("Nomor Baru ○")
        
    if step == 3:
        parts.append(f"<b>» LABEL: {prefix.upper()} «</b>" if prefix else "<b>» LABEL «</b>")
    else:
        parts.append(f"Label: {prefix}" if prefix else "Label ○")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ TAMBAH KONTAK VCF ]</b>\n"
        "────────────────────────────\n"
        f"{breadcrumbs}\n"
        "────────────────────────────\n\n"
    )

def parse_vcf_prefixes(contacts: list) -> dict[str, dict]:
    """
    Scans list of contacts and extracts all unique prefixes, their separator, and maximum numeric index.
    Returns e.g. {"Admin": {"max_idx": 2, "sep": " "}, "Navy": {"max_idx": 3, "sep": " "}}
    """
    prefix_data = {}
    for c in contacts:
        name = c.get("name", "").strip()
        if not name:
            continue
            
        # Match trailing digits, e.g. "Admin 2", "Navy-3", "CV B 10"
        m = re.match(r'^(.*?)([\s\-_]*)(\d+)$', name)
        if m:
            prefix = m.group(1).rstrip()
            sep = m.group(2)
            index = int(m.group(3))
            if not prefix:
                prefix = "FEE"
                sep = " "
            
            if prefix not in prefix_data:
                prefix_data[prefix] = {"max_idx": index, "sep": sep}
            else:
                if index > prefix_data[prefix]["max_idx"]:
                    prefix_data[prefix]["max_idx"] = index
                    prefix_data[prefix]["sep"] = sep
        else:
            if name not in prefix_data:
                prefix_data[name] = {"max_idx": 0, "sep": " "}
                
    return prefix_data

def find_prefix_info(typed_prefix: str, prefix_max_indices: dict) -> tuple[str, dict]:
    """
    Case-insensitive matching to find prefix info from prefix_max_indices.
    Returns (matched_prefix_name, info_dict)
    """
    if typed_prefix in prefix_max_indices:
        return typed_prefix, prefix_max_indices[typed_prefix]
        
    typed_lower = typed_prefix.lower()
    for prefix, info in prefix_max_indices.items():
        if prefix.lower() == typed_lower:
            return prefix, info
            
    return typed_prefix, {"max_idx": 0, "sep": " "}


def _clear_buffers(user_id: int):
    from middleware.session import clear_user_dir
    try:
        clear_user_dir(user_id)
    except Exception:
        pass


async def cmd_addnum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler Command /addnum"""
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _clear_buffers(user_id)
    
    db.set_session(user_id, S0, {"filename": "", "total_contacts": 0})
    
    text = _get_breadcrumbs({"filename": ""}, 1) + "<b>[ ➔ ] Menunggu berkas VCF...</b>\nKirim file <b>.VCF</b> yang ingin Anda tambahkan nomor barunya."
    
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
        db.set_session(user_id, S0, sess["data"])

async def handle_addnum_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler ketika user mengunggah file VCF"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    
    if not sess or sess["state"] != S0:
        return
        
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".vcf"):
        await update.message.reply_text("Kirim file dengan ekstensi .vcf.")
        return
        
    msg_id = update.message.message_id
    try:
        await update.message.delete()
    except Exception:
        pass
        
    # Download file ke disk
    file_obj = await context.bot.get_file(doc.file_id)
    user_dir = get_user_dir(user_id)
    addnum_dir = os.path.join(user_dir, "addnum")
    os.makedirs(addnum_dir, exist_ok=True)
    
    orig_name = doc.file_name
    safe_name = sanitize_filename(orig_name)
    file_path = os.path.join(addnum_dir, f"{msg_id}____{safe_name}")
    
    try:
        await file_obj.download_to_drive(file_path)
        
        # Parse VCF
        contacts = parse_vcf_file(file_path)
        total_contacts = len(contacts)
        
        # Deteksi semua format nama & index tertinggi di VCF
        prefix_max_indices = parse_vcf_prefixes(contacts)
        
        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if not sess or sess["state"] != S0:
                if os.path.exists(file_path):
                    os.remove(file_path)
                return
                
            data = sess["data"]
            data["filename"] = safe_name
            data["file_path"] = file_path
            data["contacts"] = contacts
            data["total_contacts"] = total_contacts
            data["prefix_max_indices"] = prefix_max_indices
            data["new_numbers"] = []
            
            db.set_session(user_id, S1, data)
            
        # Update UI ke menu input nomor
        await _show_wait_numbers_menu(update, context, data)
        
    except Exception as e:
        logger.error("Gagal memproses file di addnum: %s", e)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        await context.bot.send_message(chat_id=chat_id, text="Gagal memproses file VCF. Silakan coba lagi.")

async def _show_wait_numbers_menu(update: Update, context, data: dict):
    status_msg_id = data.get("status_msg_id")
    new_count = len(data["new_numbers"])
    
    text = (
        _get_breadcrumbs(data, 2) +
        f"Berkas: <code>{data['filename']}</code>\n"
        f"Total Kontak: <b>{data['total_contacts']}</b>\n\n"
        f"<b>Menunggu nomor baru...</b>\n"
        f"Kirim nomor baru sekarang (bisa kirim banyak nomor sekaligus dipisahkan baris baru/koma/spasi).\n\n"
        f"Ketik atau klik tombol <b>SELESAI & LANJUT</b> jika sudah selesai mengirim nomor."
    )
    
    # Update status jumlah nomor baru di pesan jika sudah ada
    if new_count > 0:
        # Generate numbers summary (rekapan)
        num_list_str = ""
        for idx, num in enumerate(data["new_numbers"][:15], 1): # list up to 15 numbers
            num_list_str += f"{idx}. <code>{num}</code>\n"
        if len(data["new_numbers"]) > 15:
            num_list_str += f"... dan {len(data['new_numbers']) - 15} nomor lainnya.\n"
            
        text = (
            _get_breadcrumbs(data, 2) +
            f"Berkas: <code>{data['filename']}</code>\n"
            f"Total Kontak: <b>{data['total_contacts']}</b>\n\n"
            f"<b>NOMOR YANG DIMASUKKAN ({new_count}):</b>\n"
            f"{num_list_str}\n"
            f"Kirim nomor baru lagi, atau klik tombol <b>SELESAI & LANJUT</b> di bawah."
        )
        
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("SELESAI & LANJUT", callback_data="addnum_numbers_done", style="success")],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_addnum_numbers_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User mengirim nomor baru dalam bentuk teks"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
        
    data = sess["data"]
    clean_numbers = extract_numbers_from_text(text)
    if not clean_numbers:
        return
        
    # Tambahkan nomor baru (hindari duplikat)
    existing_new_nums = set(data["new_numbers"])
    for num in clean_numbers:
        if num not in existing_new_nums:
            data["new_numbers"].append(num)
            existing_new_nums.add(num)
            
    db.set_session(user_id, S1, data)
    await _show_wait_numbers_menu(update, context, data)

async def handle_addnum_numbers_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback ketika user mengklik SELESAI & LANJUT"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    data = sess["data"]
    new_numbers = data.get("new_numbers", [])
    if not new_numbers:
        # Peringatan jika belum ada nomor
        status_msg_id = data.get("status_msg_id")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("SELESAI & LANJUT", callback_data="addnum_numbers_done", style="success")],
            [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
        ])
        try:
            await query.edit_message_text(
                text=_get_breadcrumbs(data, 2) + "<b>Error: Anda belum mengirimkan nomor baru sama sekali!</b>\n\nSilakan kirim nomor baru terlebih dahulu.",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
        return
        
    db.set_session(user_id, S2, data)
    await _show_wait_label_menu(update, context, data)


async def handle_addnum_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User mengetik 'done' atau 'selesai' di chat"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    data = sess["data"]
    
    # Hapus pesan 'done' user
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass
            
    new_numbers = data.get("new_numbers", [])
    if not new_numbers:
        status_msg_id = data.get("status_msg_id")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("SELESAI & LANJUT", callback_data="addnum_numbers_done", style="success")],
            [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
        ])
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=_get_breadcrumbs(data, 2) + "<b>Error: Anda belum mengirimkan nomor baru sama sekali!</b>\n\nSilakan kirim nomor baru terlebih dahulu.",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
        return
        
    db.set_session(user_id, S2, data)
    await _show_wait_label_menu(update, context, data)

async def _show_wait_label_menu(update: Update, context, data: dict):
    status_msg_id = data.get("status_msg_id")
    prefix_max_indices = data.get("prefix_max_indices", {})
    new_count = len(data["new_numbers"])
    
    # Generate numbers summary (rekapan)
    num_list_str = ""
    for idx, num in enumerate(data["new_numbers"][:10], 1):
        num_list_str += f"{idx}. <code>{num}</code>\n"
    if len(data["new_numbers"]) > 10:
        num_list_str += f"... dan {len(data['new_numbers']) - 10} nomor lainnya.\n"
        
    text = (
        _get_breadcrumbs(data, 3) +
        f"Berkas: <code>{data['filename']}</code>\n"
        f"Total Kontak Asli: <b>{data['total_contacts']}</b>\n"
        f"Nomor Baru ditambahkan: <b>{new_count} nomor</b>\n\n"
        f"<b>REKAPAN NOMOR BARU:</b>\n"
        f"{num_list_str}\n"
        f"<b>Pilih/Ketik Format Nama Kontak Baru:</b>\n"
        f"Pilih salah satu format nama yang terdeteksi di bawah, atau ketik langsung nama kontak baru di chat untuk membuat format baru:"
    )
    
    # Buat tombol dinamis untuk format nama yang terdeteksi
    keyboard_buttons = []
    for prefix, info in prefix_max_indices.items():
        # Batasi panjang tombol
        if len(prefix) > 20 or not prefix.strip():
            continue
        max_idx = info.get("max_idx", 0)
        next_idx = max_idx + 1
        label = f"{prefix.upper()} (Mulai no {next_idx})"
        keyboard_buttons.append([
            InlineKeyboardButton(label, callback_data=f"addnum_lbl_{prefix}")
        ])
        
    # Jika tidak terdeteksi apapun, tampilkan default
    if not keyboard_buttons:
        keyboard_buttons.append([
            InlineKeyboardButton("FEE (Mulai no 1)", callback_data="addnum_lbl_FEE")
        ])
        
    keyboard_buttons.append([InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")])
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard_buttons)
    )

async def handle_addnum_label_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih format nama via tombol inline"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S2:
        return
        
    data = sess["data"]
    # Ambil prefix setelah prefix string 'addnum_lbl_'
    prefix = query.data.replace("addnum_lbl_", "")
    data["prefix"] = prefix
    
    # Ambil nomor urut awal dari cache atau default ke 1
    prefix_max_indices = data.get("prefix_max_indices", {})
    matched_prefix, prefix_info = find_prefix_info(prefix, prefix_max_indices)
    
    # Pastikan kita menggunakan casing asli dari prefix yang terdeteksi!
    data["prefix"] = matched_prefix
    data["next_index"] = prefix_info.get("max_idx", 0) + 1
    data["sep"] = prefix_info.get("sep", " ")
    
    db.set_session(user_id, S2, data)
    await handle_addnum_process(update, context, data)


async def handle_addnum_label_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User mengetik nama kontak kustom"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S2:
        return
        
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
        
    data = sess["data"]
    if not text:
        return
        
    # Cari tahu apakah prefix baru ini sudah ada di VCF (case-insensitive)
    prefix_max_indices = data.get("prefix_max_indices", {})
    matched_prefix, prefix_info = find_prefix_info(text, prefix_max_indices)
    
    # Jika ketemu case-insensitive, gunakan casing asli & separator aslinya
    data["prefix"] = matched_prefix
    data["next_index"] = prefix_info.get("max_idx", 0) + 1
    data["sep"] = prefix_info.get("sep", " ")
    
    db.set_session(user_id, S2, data)
    await handle_addnum_process(update, context, data)

async def handle_addnum_process(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """Proses merge kontak dan pengiriman file"""
    user_id = update.effective_user.id
    status_msg_id = data.get("status_msg_id")
    
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<b>Memproses penggabungan kontak...</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass
        
    try:
        contacts = data["contacts"]
        new_numbers = data["new_numbers"]
        prefix = data["prefix"]
        next_index = data["next_index"]
        sep = data.get("sep", " ")
        
        # Buat kontak baru dengan sequence yang tepat
        new_contacts = []
        for i, num in enumerate(new_numbers):
            idx = next_index + i
            new_contacts.append({
                "name": f"{prefix}{sep}{idx}",
                "tel": add_plus(num)
            })
            
        # Gabungkan ke daftar kontak asli
        final_contacts = contacts + new_contacts
        
        # Generate VCF bytes
        loop = asyncio.get_running_loop()
        vcf_content = await loop.run_in_executor(None, lambda: contacts_to_vcf(final_contacts))
        vcf_bytes = vcf_content.encode("utf-8")
        
        # Bersihkan sesi di database
        db.clear_session(user_id)
        
        # Kirim File Hasil
        out_filename = data["filename"]
        buf = io.BytesIO(vcf_bytes)
        buf.name = out_filename
        
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=buf,
            filename=out_filename
        )
        
        # Hapus status message lama
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass
            
        # Tampilkan Summary Sukses
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_addnum_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        
        from handlers.start import clear_welcome_messages, register_welcome_messages
        clear_welcome_messages(user_id)
        
        start_idx = next_index
        end_idx = next_index + len(new_contacts) - 1
        new_contact_summary = f"{prefix}{sep}{start_idx} - {end_idx}" if len(new_contacts) > 1 else f"{prefix}{sep}{start_idx}"
        
        box_text = (
            f"<pre><b>"
            f"┌────────────────────────────────────────┐\n"
            f"│             PROSES SELESAI             │\n"
            f"├────────────────────────────────────────┤\n"
            f"│ Nama File      : {_fit(out_filename):<22} │\n"
            f"│ Kontak Asli    : {_fit(str(len(contacts))):<22} │\n"
            f"│ Kontak Baru    : {_fit(str(len(new_contacts))):<22} │\n"
            f"│ Format Baru    : {_fit(new_contact_summary):<22} │\n"
            f"│ Total Kontak   : {_fit(str(len(final_contacts))):<22} │\n"
            f"└────────────────────────────────────────┘"
            f"</b></pre>\n\n"
            f"<i>Proses penambahan nomor baru ke VCF selesai! Silakan unduh berkas di atas.</i>"
        )
        
        final_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=box_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        register_welcome_messages(user_id, [final_msg.message_id])
        
    except Exception as e:
        logger.error("Error saat menyimpan penggabungan VCF: %s", e)
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Terjadi kesalahan saat memproses penggabungan berkas. Coba lagi.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        _clear_buffers(user_id)


async def handle_show_addnum_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (AddNum)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    _clear_buffers(user_id)
    db.set_session(user_id, S0, {"filename": "", "total_contacts": 0})
    
    text = _get_breadcrumbs({"filename": ""}, 1) + "<b>[ ➔ ] Menunggu berkas VCF...</b>\nKirim file <b>.VCF</b> yang ingin Anda tambahkan nomor barunya."
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
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
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])
