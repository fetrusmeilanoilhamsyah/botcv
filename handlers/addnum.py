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

MAX_FILES = 100
MAX_SIZE_MB = 50

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
    new_count = len(data.get("new_numbers", []))
    prefix = data.get("prefix", "")
    
    parts = []
    if step == 1:
        parts.append(f"<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{_fit(filename)}</code>" if filename else "Berkas: ➖")
        
    if step == 2:
        parts.append(f"<b>[NOMOR BARU: {new_count}]</b>")
    elif step > 2:
        parts.append(f"Nomor: <code>{new_count}</code>")
    else:
        parts.append("Nomor: ➖")
        
    if step == 3:
        parts.append(f"<b>[LABEL: {prefix.upper()}]</b>" if prefix else "<b>[LABEL KONTAK]</b>")
    else:
        parts.append(f"Label: <code>{prefix}</code>" if prefix else "Label: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ TAMBAH KONTAK VCF CONSOLE ]</b>\n"
                f"<blockquote>{breadcrumbs}</blockquote>\n"
        "\n"
    )

def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu file <code>.vcf</code> sumber sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"\u2022 Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code></blockquote>"
    )

def parse_vcf_prefixes(contacts: list) -> dict[str, dict]:
    prefix_data = {}
    for c in contacts:
        name = c.get("name", "").strip()
        if not name:
            continue
            
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
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _clear_buffers(user_id)
    
    init_data = {"filename": "", "total_contacts": 0}
    db.set_session(user_id, S0, init_data)
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _waiting_text(init_data),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])

async def handle_addnum_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    
    if not sess or sess["state"] != S0:
        return
        
    doc = update.message.document
    ext = os.path.splitext(doc.file_name)[1].lower() if doc and doc.file_name else ""
    if not doc or not doc.file_name or ext != ".vcf":
        # Format salah — edit status message in-place, JANGAN hapus file user
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            sent_name = doc.file_name if doc and doc.file_name else "file tersebut"
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        f"<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{sent_name}</code> bukan berkas VCF (<code>.vcf</code>).</blockquote>"
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
        
    # Download file ke disk
    file_obj = await context.bot.get_file(doc.file_id)
    user_dir = get_user_dir(user_id)
    addnum_dir = os.path.join(user_dir, "addnum")
    os.makedirs(addnum_dir, exist_ok=True)
    
    msg_id = update.message.message_id
    orig_name = doc.file_name
    safe_name = sanitize_filename(orig_name)
    file_path = os.path.join(addnum_dir, f"{msg_id}____{safe_name}")
    
    try:
        await file_obj.download_to_drive(file_path)
        contacts = parse_vcf_file(file_path)
        total_contacts = len(contacts)
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
            
        await _show_wait_numbers_menu(update, context, data, user_id=user_id)
        
    except Exception as e:
        logger.error("Gagal memproses file di addnum: %s", e)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Gagal memproses file VCF. Silakan coba lagi.</b></blockquote>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
                )
        except Exception:
            pass

async def _show_wait_numbers_menu(update: Update, context, data: dict, user_id: int = None):
    """Tampilkan menu input nomor baru. Selalu hapus pesan status lama & kirim pesan baru di bawah."""
    status_msg_id = data.get("status_msg_id")
    new_count = len(data["new_numbers"])
    chat_id = update.effective_chat.id
    
    text = (
        _get_breadcrumbs(data, 2) +
        f"<blockquote><b>[ LANGKAH 1: KIRIM NOMOR BARU ]</b>\n"
        f"Sumber: <code>{data['filename']}</code> ({data['total_contacts']} kontak)\n\n"
        f"Kirim nomor baru sekarang (dipisahkan baris baru/koma/spasi).\n"
        f"Ketik atau klik tombol <b>SELESAI & LANJUT</b> jika sudah selesai.</blockquote>"
    )
    
    if new_count > 0:
        num_list_str = ""
        for idx, num in enumerate(data["new_numbers"][:15], 1):
            num_list_str += f"{idx}. <code>{num}</code>\n"
        if len(data["new_numbers"]) > 15:
            num_list_str += f"... dan {len(data['new_numbers']) - 15} nomor lainnya.\n"
            
        text = (
            _get_breadcrumbs(data, 2) +
            f"<blockquote><b>[ LANGKAH 1: KIRIM NOMOR BARU ]</b>\n"
            f"Sumber: <code>{data['filename']}</code> ({data['total_contacts']} kontak)\n\n"
            f"<b>NOMOR YANG DIMASUKKAN ({new_count}):</b>\n"
            f"{num_list_str}\n"
            f"Kirim nomor baru lagi, atau klik tombol <b>SELESAI & LANJUT</b>.</blockquote>"
        )
        
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("SELESAI & LANJUT", callback_data="addnum_numbers_done", style="success")],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])

    # Jika dipanggil setelah upload file (new_count == 0 = pertama kali setelah file masuk):
    # hapus status_msg lama → kirim pesan BARU di bawah file
    if new_count == 0 and status_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass
        new_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        # Update status_msg_id ke pesan baru
        if user_id is None and update.effective_user:
            user_id = update.effective_user.id
        if user_id:
            sess = db.get_session(user_id)
            if sess:
                sess["data"]["status_msg_id"] = new_msg.message_id
                db.set_session(user_id, S1, sess["data"])
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [new_msg.message_id])
    else:
        # Saat user sudah kirim nomor sebelumnya → edit in-place saja
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass

async def handle_addnum_numbers_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        
    existing_new_nums = set(data["new_numbers"])
    for num in clean_numbers:
        if num not in existing_new_nums:
            data["new_numbers"].append(num)
            existing_new_nums.add(num)
            
    db.set_session(user_id, S1, data)
    await _show_wait_numbers_menu(update, context, data)

async def handle_addnum_numbers_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    data = sess["data"]
    new_numbers = data.get("new_numbers", [])
    if not new_numbers:
        status_msg_id = data.get("status_msg_id")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("SELESAI & LANJUT", callback_data="addnum_numbers_done", style="success")],
            [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
        ])
        try:
            await query.edit_message_text(
                text=_get_breadcrumbs(data, 2) + "<blockquote>⚠️ <b>[ ERROR ]</b>\nAnda belum mengirimkan nomor baru sama sekali! Silakan kirim nomor baru terlebih dahulu.</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass
        return
        
    db.set_session(user_id, S2, data)
    await _show_wait_label_menu(update, context, data)

async def handle_addnum_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S1:
        return
        
    data = sess["data"]
    
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
                text=_get_breadcrumbs(data, 2) + "<blockquote>⚠️ <b>[ ERROR ]</b>\nAnda belum mengirimkan nomor baru sama sekali! Silakan kirim nomor baru terlebih dahulu.</blockquote>",
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
    
    num_list_str = ""
    for idx, num in enumerate(data["new_numbers"][:10], 1):
        num_list_str += f"{idx}. <code>{num}</code>\n"
    if len(data["new_numbers"]) > 10:
        num_list_str += f"... dan {len(data['new_numbers']) - 10} nomor lainnya.\n"
        
    text = (
        _get_breadcrumbs(data, 3) +
        f"<blockquote><b>[ LANGKAH 2: FORMAT NAMA KONTAK BARU ]</b>\n"
        f"Sumber: <code>{data['filename']}</code>\n"
        f"Kontak Baru: <code>{new_count} nomor</code>\n\n"
        f"<b>REKAPAN:</b>\n"
        f"{num_list_str}\n"
        f"<b>Pilih atau ketik format nama baru:</b>\n"
        f"Pilih format di bawah, atau ketik langsung nama kustom di chat untuk membuat format baru:</blockquote>"
    )
    
    keyboard_buttons = []
    for prefix, info in prefix_max_indices.items():
        if len(prefix) > 20 or not prefix.strip():
            continue
        max_idx = info.get("max_idx", 0)
        next_idx = max_idx + 1
        label = f"{prefix.upper()} (Mulai no {next_idx})"
        keyboard_buttons.append([
            InlineKeyboardButton(label, callback_data=f"addnum_lbl_{prefix}")
        ])
        
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
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != S2:
        return
        
    data = sess["data"]
    prefix = query.data.replace("addnum_lbl_", "")
    data["prefix"] = prefix
    
    prefix_max_indices = data.get("prefix_max_indices", {})
    matched_prefix, prefix_info = find_prefix_info(prefix, prefix_max_indices)
    
    data["prefix"] = matched_prefix
    data["next_index"] = prefix_info.get("max_idx", 0) + 1
    data["sep"] = prefix_info.get("sep", " ")
    
    db.set_session(user_id, S2, data)
    await handle_addnum_process(update, context, data)

async def handle_addnum_label_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        
    prefix_max_indices = data.get("prefix_max_indices", {})
    matched_prefix, prefix_info = find_prefix_info(text, prefix_max_indices)
    
    data["prefix"] = matched_prefix
    data["next_index"] = prefix_info.get("max_idx", 0) + 1
    data["sep"] = prefix_info.get("sep", " ")
    
    db.set_session(user_id, S2, data)
    await handle_addnum_process(update, context, data)

async def handle_addnum_process(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    user_id = update.effective_user.id
    status_msg_id = data.get("status_msg_id")
    
    process_text = "<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memproses penggabungan nomor baru ke berkas VCF...</blockquote>"
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=process_text,
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
        
        new_contacts = []
        for i, num in enumerate(new_numbers):
            idx = next_index + i
            new_contacts.append({
                "name": f"{prefix}{sep}{idx}",
                "tel": add_plus(num)
            })
            
        final_contacts = contacts + new_contacts
        
        loop = asyncio.get_running_loop()
        vcf_content = await loop.run_in_executor(None, lambda: contacts_to_vcf(final_contacts))
        vcf_bytes = vcf_content.encode("utf-8")
        
        db.clear_session(user_id)
        
        # Kirim status mengirim
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="<blockquote><b>[ SYSTEM: SENDING FILES ]</b>\nSedang mengirim berkas VCF hasil...</blockquote>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        out_filename = data["filename"]
        buf = io.BytesIO(vcf_bytes)
        buf.name = out_filename
        
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=buf,
            filename=out_filename
        )
        
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass
            
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
            f"<b>[ PROSES SELESAI ]</b>\n"
            f"<blockquote>"
            f"• Nama File : {out_filename}\n"
            f"• Kontak Asli : {len(contacts)}\n"
            f"• Kontak Baru : {len(new_contacts)}\n"
            f"• Format Baru : {new_contact_summary}\n"
            f"• Total Kontak : {len(final_contacts)}</blockquote>\n\n"
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
        if status_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Terjadi kesalahan saat memproses penggabungan berkas.</b></blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    finally:
        _clear_buffers(user_id)

async def handle_show_addnum_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    _clear_buffers(user_id)
    init_data = {"filename": "", "total_contacts": 0}
    db.set_session(user_id, S0, init_data)
    text = _waiting_text(init_data)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup
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
            reply_markup=markup
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, S0, sess["data"])
