"""
vcfsimple.py - Disk-based, terima paralel, sort by message_id.
UI/UX Level Dewa: Single-Message Morphing Wizard, bebas emoji.
Nama kontak otomatis dikunci menggunakan nomor telepon kontak itu sendiri.
Nama file keluaran otomatis dikunci menggunakan nama file TXT asli yang diunggah.
"""
import os
import shutil
import asyncio
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import add_plus
from core.utils import sanitize_filename

VS_WAIT_FILE = "VS_WAIT_FILE"
VS_PER_FILE = "VS_PER_FILE"
VS_AWALAN = "VS_AWALAN"
VS_COLLECTING = "VS_COLLECTING"
VS_DELIVERY = "VS_DELIVERY"

from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
    MAX_CONTACTS_PER_FILE,
    SEND_MAX_RETRIES,
    SEND_RETRY_DELAY,
    FILE_READ_TIMEOUT,
    FILE_WRITE_TIMEOUT,
    FILE_CONNECT_TIMEOUT,
    SEND_FILE_DELAY,
    SEND_BATCH_DELAY,
)

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    per_file = data.get("per_file", "")
    file_name = data.get("file_name", "")
    awalan = data.get("awalan", "")
    
    parts = []
    
    if step == 1:
        parts.append(f"<b>[ BERKAS: {count} FILE ]</b>" if count else "<b>[ BERKAS ]</b>")
    else:
        parts.append(f"Berkas: {count} file" if count else "Berkas o")
        
    parts.append("Nama: Sesuai Nomor")
        
    if file_name:
        parts.append(f"File: {file_name}")
    else:
        parts.append("File o")
        
    if step == 2:
        if per_file:
            parts.append(f"<b>[ JUMLAH: {per_file} ]</b>")
        else:
            parts.append("<b>[ JUMLAH ]</b>")
    elif step > 2 and per_file:
        parts.append(f"Jumlah: {per_file}")
    else:
        parts.append("Jumlah o")
        
    num_style = data.get("file_num_style", "")
    style_suffix = ""
    if num_style == "front":
        style_suffix = " (AWAL)"
    elif num_style == "back":
        style_suffix = " (AKHIR)"
        
    if step == 3:
        if awalan:
            parts.append(f"<b>[ URUTAN: {awalan}{style_suffix} ]</b>")
        else:
            parts.append("<b>[ URUTAN ]</b>")
    elif step > 3 and awalan:
        parts.append(f"Urutan: {awalan}{style_suffix}")
    else:
        parts.append("Urutan o")
        
    breadcrumbs = " -> ".join(parts)
    return (
        "<b>[ TXT -> VCF SIMPLE ]</b>\n"
        "------------------------------------\n"
        f"{breadcrumbs}\n"
        "------------------------------------\n\n"
    )

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

async def _debounce_notify(user_id: int, context, chat_id: int):
    try:
        await asyncio.sleep(1)
        if _user_timers.get(user_id) is asyncio.current_task():
            sess = db.get_session(user_id)
            if sess and sess.get("state") in [VS_WAIT_FILE, VS_COLLECTING]:
                data = sess["data"]
                jumlah = data["count"]
                
                text = _get_breadcrumbs(data, 1) + f"<b>{jumlah}</b> file TXT diterima. Silakan pilih tindakan:"
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                        InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                    ]
                ])
                
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
                    except Exception:
                        pass
                
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                data["status_msg_id"] = msg.message_id
                db.set_session(user_id, sess["state"], data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Debounce notify error in vcfsimple: %s", e)

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
    vs_dir = os.path.join(user_dir, "vcfsimple")
    shutil.rmtree(vs_dir, ignore_errors=True)

async def cmd_vcfsimple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler Command /vcfsimple"""
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    db.set_session(user_id, VS_WAIT_FILE, {"count": 0, "total_size": 0, "total_contacts": 0, "file_name": ""})
    
    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _get_breadcrumbs({"count": 0}, 1) + "<b>Menunggu berkas...</b>\nKirim file <b>.TXT</b> sekarang.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )
    
    if msg:
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, VS_WAIT_FILE, sess["data"])

async def handle_vs_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima kiriman file TXT dari user"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sess = db.get_session(user_id)
    if sess["state"] not in [VS_WAIT_FILE, VS_COLLECTING]:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        try:
            await update.message.delete()
        except Exception:
            pass
        return
        
    msg_id = update.message.message_id

    # Download ke disk
    file_obj = await context.bot.get_file(doc.file_id)
    vs_dir = os.path.join(get_user_dir(user_id), "vcfsimple")
    os.makedirs(vs_dir, exist_ok=True)
    out_path = os.path.join(vs_dir, f"{msg_id}_{sanitize_filename(doc.file_name)}")
    
    try:
        await file_obj.download_to_drive(out_path)

        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if sess["state"] not in [VS_WAIT_FILE, VS_COLLECTING]:
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
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            # Hitung kontak
            lines = 0
            try:
                with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip():
                            lines += 1
            except Exception:
                pass

            data["count"] += 1
            data["total_size"] += doc.file_size
            data["total_contacts"] = data.get("total_contacts", 0) + lines
            
            # Jika file pertama, kunci file_name menggunakan nama file TXT asli
            if data["count"] == 1:
                base_name = os.path.splitext(doc.file_name)[0]
                data["file_name"] = sanitize_filename(base_name)
                
            db.set_session(user_id, sess["state"], data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Download failed in vcfsimple: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise

async def handle_vs_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memicu selesai kirim file"""
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if not sess:
        return
    data = sess["data"]
    
    # Hapus input teks 'done' jika user mengetik manual
    if update.message and update.message.text in ("done", "selesai", "/done"):
        try:
            await update.message.delete()
        except Exception:
            pass

    status_msg_id = data.get("status_msg_id")

    if sess["state"] == VS_WAIT_FILE:
        if data["count"] == 0:
            return
        
        db.set_session(user_id, VS_PER_FILE, data)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(data, 2) + f"<b>{data.get('total_contacts', 0)}</b> kontak terdeteksi. Nama kontak dikunci sesuai nomor masing-masing kontak.\n\nBerapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return

    if sess["state"] != VS_COLLECTING:
        return
    
    if data["count"] == 0:
        return
    if data.get("is_processing"):
        return

    data["is_processing"] = True
    db.set_session(user_id, sess["state"], data)
    await handle_vs_process(update, context)

async def handle_vs_per_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memasukkan jumlah kontak per file"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != VS_PER_FILE:
        return
    
    text = update.message.text.strip()
    
    # Hapus input teks user
    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg_id = sess["data"].get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not text.isdigit():
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(sess["data"], 2) + "Harap masukkan angka saja.\n\nBerapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
    
    per_file = int(text)
    if per_file < 1 or per_file > MAX_CONTACTS_PER_FILE:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(sess["data"], 2) + f"Harap masukkan angka antara 1 sampai {MAX_CONTACTS_PER_FILE:,}.\n\nBerapa kontak per file? Contoh: <b>100</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return

    data = sess["data"]
    data["per_file"] = per_file
    db.set_session(user_id, VS_AWALAN, data)
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=_get_breadcrumbs(data, 3) + "Nomor urut awal? Contoh: <b>1</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def handle_vs_awalan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memasukkan nomor urut awal"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if sess["state"] != VS_AWALAN:
        return
    
    # Hapus input teks user
    try:
        await update.message.delete()
    except Exception:
        pass

    text = update.message.text.strip()
    status_msg_id = sess["data"].get("status_msg_id")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    if not text.isdigit() or int(text) < 1:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text=_get_breadcrumbs(sess["data"], 3) + "Harap masukkan angka valid (minimal 1).\n\nNomor urut awal? Contoh: <b>1</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
        
    data = sess["data"]
    data["awalan"] = int(text)
    
    db.set_session(user_id, VS_DELIVERY, data)
    
    file_name = data.get("file_name", "FEE")
    style_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"NOMOR DI AWAL (e.g. 1 {file_name})", callback_data="vs_numstyle_front", style="primary"),
            InlineKeyboardButton(f"NOMOR DI AKHIR (e.g. {file_name} 1)", callback_data="vs_numstyle_back", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text=_get_breadcrumbs(data, 3) + "Pilih format penomoran nama file:",
        parse_mode="HTML",
        reply_markup=style_keyboard
    )

async def handle_vs_numstyle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih format penomoran nama file"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != VS_DELIVERY:
        return
        
    data = sess["data"]
    num_style = "front" if query.data == "vs_numstyle_front" else "back"
    data["file_num_style"] = num_style
    db.set_session(user_id, VS_DELIVERY, data)
    
    deliv_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("KIRIM SATU PER SATU", callback_data="vs_deliv_single", style="primary"),
            InlineKeyboardButton("KIRIM SEBAGAI ZIP", callback_data="vs_deliv_zip", style="primary")
        ],
        [InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]
    ])
    
    await query.edit_message_text(
        text=_get_breadcrumbs(data, 4) + "Pilih format pengiriman file VCF:",
        parse_mode="HTML",
        reply_markup=deliv_keyboard
    )

async def handle_vs_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih format pengiriman (VCF satu per satu atau ZIP)"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != VS_DELIVERY:
        return
        
    data = sess["data"]
    mode = "single" if query.data == "vs_deliv_single" else "zip"
    data["delivery_mode"] = mode
    
    db.set_session(user_id, VS_COLLECTING, data)
    await handle_vs_process(update, context)

async def handle_vs_delivery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hapus chat iseng dari user saat berada di state pemilihan pengiriman"""
    try:
        await update.message.delete()
    except Exception:
        pass

async def handle_vs_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses pembuatan berkas VCF"""
    user_id = update.effective_user.id
    from handlers.cancel_helper import register_active_task, unregister_active_task
    register_active_task(user_id, asyncio.current_task())

    sess = db.get_session(user_id)
    data = sess["data"]
    
    if data.get("is_processing_final"):
        return
    data["is_processing_final"] = True
    db.set_session(user_id, sess["state"], data)
    
    status_msg_id = data.get("status_msg_id")
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text="Memproses...",
        parse_mode="HTML"
    )

    user_dir = get_user_dir(user_id)
    vs_dir = os.path.join(user_dir, "vcfsimple")
    
    files = []
    if os.path.exists(vs_dir):
        files = [f for f in os.listdir(vs_dir) if f.endswith('.txt')]
        files.sort(key=lambda x: int(x.split('_')[0]))

    file_name    = data["file_name"] or "FEE"
    per_file     = data["per_file"]
    awalan       = data["awalan"]

    loop = asyncio.get_running_loop()

    def do_build():
        all_numbers = []
        for f in files:
            path = os.path.join(vs_dir, f)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as file_in:
                    for line in file_in:
                        num = line.strip()
                        if num:
                            all_numbers.append(add_plus(num))
            except Exception:
                pass

        results = []
        file_counter = awalan
        
        for i in range(0, len(all_numbers), per_file):
            chunk = all_numbers[i:i + per_file]
            vcf_lines = []
            for num in chunk:
                name = num.lstrip("+")
                vcf_lines.append(f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL;TYPE=CELL:{num}\nEND:VCARD")
            num_style = data.get("file_num_style", "back")
            if num_style == "front":
                label = f"{file_counter} {file_name}"
            else:
                label = f"{file_name} {file_counter}"
            content = ("\n".join(vcf_lines) + "\n").encode("utf-8")
            results.append((label, content))
            file_counter += 1
        return all_numbers, results

    all_numbers, results = await loop.run_in_executor(None, do_build)

    import io
    from telegram.error import RetryAfter
    import logging as _log
    _logger = _log.getLogger(__name__)

    try:
        total_input = len(files)
        per_file_val = data.get("per_file", 0)
        file_name_val = data.get("file_name", "")
        awalan_val = data.get("awalan", 1)

        if not results:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Gagal. Data tidak ditemukan.",
                parse_mode="HTML"
            )
            return

        total_files = len(results)
        delivery_mode = data.get("delivery_mode", "single")

        if delivery_mode == "zip":
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Mengompresi file ke ZIP...",
                parse_mode="HTML"
            )
            
            import zipfile
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for label, content in results:
                    zip_file.writestr(f"{label}.vcf", content)
            zip_buffer.seek(0)
            zip_filename = f"{file_name}.zip"

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text="Mengirim file ZIP...",
                parse_mode="HTML"
            )

            for attempt in range(SEND_MAX_RETRIES):
                try:
                    zip_buffer.seek(0)
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=zip_buffer,
                        filename=zip_filename,
                        read_timeout=FILE_READ_TIMEOUT,
                        write_timeout=FILE_WRITE_TIMEOUT,
                        connect_timeout=FILE_CONNECT_TIMEOUT,
                    )
                    break
                except Exception as ex:
                    _logger.error(f"[VCFSIMPLE] Gagal kirim ZIP attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        raise
                    else:
                        await asyncio.sleep(SEND_RETRY_DELAY)

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_vcfsimple_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            total_contacts_str = f"{len(all_numbers):,}"
            box_text = (
                f"<pre><b>"
                f"+----------------------------------------+\n"
                f"|             PROSES SELESAI             |\n"
                f"+----------------------------------------+\n"
                f"| Total Berkas   : {_fit(f'{total_input} TXT'):<22} |\n"
                f"| Berkas Output  : {_fit(f'{total_files} VCF (ZIP)'):<22} |\n"
                f"| Nama Kontak    : {_fit('Sesuai Nomor'):<22} |\n"
                f"| Jumlah / File  : {_fit(per_file_val):<22} |\n"
                f"| Nama File VCF  : {_fit(file_name_val):<22} |\n"
                f"| Urutan Mulai   : {_fit(f'{awalan_val} (AWAL)' if data.get('file_num_style') == 'front' else f'{awalan_val} (AKHIR)'):<22} |\n"
                f"| Total Kontak   : {_fit(total_contacts_str):<22} |\n"
                f"+----------------------------------------+"
                f"</b></pre>\n\n"
                f"<i>Silakan unduh file ZIP di atas.</i>"
            )
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

        else:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<b>Mengirim file VCF...</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            sent_count = 0
            from config import SEND_PROGRESS_INTERVAL, SEND_BATCH_SIZE

            for idx, (label, content) in enumerate(results):
                buf = io.BytesIO(content)
                buf.name = f"{label}.vcf"

                for attempt in range(SEND_MAX_RETRIES):
                    try:
                        buf.seek(0)
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=buf,
                            filename=f"{label}.vcf",
                            read_timeout=FILE_READ_TIMEOUT,
                            write_timeout=FILE_WRITE_TIMEOUT,
                            connect_timeout=FILE_CONNECT_TIMEOUT,
                        )
                        sent_count += 1
                        break
                    except RetryAfter as e:
                        wait_secs = max(int(e.retry_after), 2) + 1
                        _logger.warning(f"[VCFSIMPLE] Flood limit sekuensial, tunggu {wait_secs}s")
                        await asyncio.sleep(wait_secs)
                    except Exception as ex:
                        _logger.error(f"[VCFSIMPLE] Gagal kirim file VCF sekuensial {label} attempt {attempt+1}: {ex}")
                        if attempt == SEND_MAX_RETRIES - 1:
                            sent_count += 1
                        else:
                            await asyncio.sleep(SEND_RETRY_DELAY)

                if sent_count < total_files:
                    if sent_count % SEND_BATCH_SIZE == 0:
                        await asyncio.sleep(SEND_BATCH_DELAY)
                    else:
                        await asyncio.sleep(SEND_FILE_DELAY)

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_vcfsimple_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            total_contacts_str = f"{len(all_numbers):,}"
            box_text = (
                f"<pre><b>"
                f"+----------------------------------------+\n"
                f"|             PROSES SELESAI             |\n"
                f"+----------------------------------------+\n"
                f"| Total Berkas   : {_fit(f'{total_input} TXT'):<22} |\n"
                f"| Berkas Output  : {_fit(f'{total_files} VCF'):<22} |\n"
                f"| Nama Kontak    : {_fit('Sesuai Nomor'):<22} |\n"
                f"| Jumlah / File  : {_fit(per_file_val):<22} |\n"
                f"| Nama File VCF  : {_fit(file_name_val):<22} |\n"
                f"| Urutan Mulai   : {_fit(f'{awalan_val} (AWAL)' if data.get('file_num_style') == 'front' else f'{awalan_val} (AKHIR)'):<22} |\n"
                f"| Total Kontak   : {_fit(total_contacts_str):<22} |\n"
                f"+----------------------------------------+"
                f"</b></pre>\n\n"
                f"<i>Silakan unduh file VCF di atas.</i>"
            )
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

    finally:
        unregister_active_task(user_id)
        db.clear_session(user_id)
        _clear_buffers(user_id)

async def handle_show_vcfsimple_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    db.set_session(user_id, VS_WAIT_FILE, {"count": 0, "total_size": 0, "total_contacts": 0, "file_name": ""})

    text = _get_breadcrumbs({"count": 0}, 1) + "<b>Menunggu berkas...</b>\nKirim file <b>.TXT</b> sekarang."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    try:
        await query.message.edit_text(text=text, parse_mode="HTML", reply_markup=keyboard)
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, VS_WAIT_FILE, sess["data"])
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = msg.message_id
        db.set_session(user_id, VS_WAIT_FILE, sess["data"])
