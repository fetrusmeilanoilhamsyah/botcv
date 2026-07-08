"""
merge.py — Mendukung VCF dan TXT.
- VCF input  → output 1 file VCF gabungan
- TXT input  → output 1 file TXT gabungan (deduplikasi nomor)
- Disk-based, sort by message_id, mencegah OOM.
"""
import os
import shutil
import asyncio
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf
from core.utils import sanitize_filename

STATE        = "MERGE_COLLECTING"
STATE_NAMING = "MERGE_NAMING"

from config import MAX_FILES_PER_SESSION as MAX_FILES, MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB
ALLOWED_EXT = {".vcf", ".txt"}

_user_locks: dict = {}
_user_timers: dict = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s


def _get_breadcrumbs(data: dict, step: int) -> str:
    count = data.get("count", 0)
    mode = (data.get("mode") or "").upper()
    file_name = data.get("file_name", "")
    
    parts = []
    # Step 1: Berkas
    if step == 1:
        if count:
            parts.append(f"<b>[UPLOAD BERKAS: {count} {mode}]</b>")
        else:
            parts.append("<b>[UPLOAD BERKAS]</b>")
    else:
        parts.append(f"Berkas: <code>{count} {mode}</code>" if count else "Berkas: ➖")
        
    # Step 2: Nama
    if step == 2:
        parts.append("<b>[NAMA FILE]</b>")
    elif step > 2 and file_name:
        parts.append(f"Nama: <code>{file_name}</code>")
    else:
        parts.append("Nama: ➖")
        
    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ MERGE VCF/TXT CONSOLE ]</b>\n"
                f"<blockquote>{breadcrumbs}</blockquote>\n"
        "\n"
    )


def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data, 1) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.vcf</code> atau <code>.txt</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"• Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"• Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file</blockquote>"
    )


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
            if sess and sess.get("state") == STATE:
                data = sess["data"]
                jumlah = data["count"]
                mode = data.get("mode", "vcf").upper()
                
                # Hapus welcome messages lama (hanya sekali saat file pertama masuk)
                from handlers.start import _welcome_messages
                welcome_ids = _welcome_messages.pop(user_id, [])
                for w_id in welcome_ids:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=w_id)
                    except Exception:
                        pass
                
                text = (
                    _get_breadcrumbs(data, 1) +
                    f"<blockquote><b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                    f"Berhasil mengunduh <code>{jumlah}</code> berkas {mode}.\n\n"
                    f"Silakan pilih tindakan di bawah:</blockquote>"
                )
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
                
                # Kirim pesan baru di paling bawah di bawah berkas yang dikirim
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                data["status_msg_id"] = msg.message_id
                db.set_session(user_id, STATE, data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Debounce notify error: %s", e)


def _reset_timer(user_id: int, context, chat_id: int):
    old = _user_timers.get(user_id)
    if old:
        old.cancel()
    _user_timers[user_id] = asyncio.ensure_future(
        _debounce_notify(user_id, context, chat_id)
    )


def _cancel_timer(user_id: int):
    old = _user_timers.pop(user_id, None)
    if old:
        old.cancel()


def _clear_buffers(user_id: int):
    user_dir = get_user_dir(user_id)
    merge_dir = os.path.join(user_dir, "merge")
    shutil.rmtree(merge_dir, ignore_errors=True)


async def cmd_merge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    from handlers.start import transition_to_handler
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    init_data = {"count": 0, "total_size": 0, "mode": None}
    db.set_session(user_id, STATE, init_data)
    
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
        db.set_session(user_id, STATE, sess["data"])


async def handle_merge_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    doc     = update.message.document
    msg_id  = update.message.message_id

    sess = db.get_session(user_id)
    if sess["state"] != STATE:
        return

    # Guard: jangan terima file saat proses sudah berjalan
    if sess["data"].get("is_processing"):
        return

    # Validasi eksensi file (User file tidak didelete)
    if not doc or not doc.file_name:
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        "<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        "Berkas tidak valid atau nama berkas kosong.</blockquote>"
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

    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in ALLOWED_EXT:
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"], 1) +
                        f"<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{doc.file_name}</code> bukan berkas <code>.vcf</code> atau <code>.txt</code>.</blockquote>"
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

    # Ambil mode dari file pertama yang diterima
    async with get_user_lock(user_id):
        sess = db.get_session(user_id)
        if sess["state"] != STATE:
            return
        data = sess["data"]

        if data.get("is_processing"):
            return

        current_mode = data.get("mode")
        file_mode    = "vcf" if ext == ".vcf" else "txt"

        # Tolak jika mencampur VCF dan TXT
        if current_mode and current_mode != file_mode:
            try:
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=(
                            _get_breadcrumbs(data, 1) +
                            f"<blockquote>⚠️ <b>[ TIPE BERBEDA ]</b>\n"
                            f"Tidak bisa mencampur file VCF dan TXT dalam satu sesi.\n\n"
                            f"Sesi ini dikunci untuk tipe <code>.{current_mode.upper()}</code>.</blockquote>"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
                    )
                    await asyncio.sleep(10)
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=_waiting_text(data),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
                    )
            except Exception:
                pass
            return

        if data["count"] >= MAX_FILES:
            try:
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=(
                            _get_breadcrumbs(data, 1) +
                            f"<blockquote>⚠️ <b>Batas sesi: <code>{MAX_FILES} file</code> telah tercapai.</b>\n"
                            f"Klik <b>PROSES SEKARANG</b> untuk melanjutkan.</blockquote>"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                            InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                        ]])
                    )
            except Exception:
                pass
            return

        if (data["total_size"] + doc.file_size) / (1024 * 1024) > MAX_SIZE_MB:
            try:
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=(
                            _get_breadcrumbs(data, 1) +
                            f"<blockquote>⚠️ <b>Batas ukuran: <code>{MAX_SIZE_MB} MB</code> telah tercapai.</b>\n"
                            f"Klik <b>PROSES SEKARANG</b> untuk melanjutkan.</blockquote>"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                            InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                        ]])
                    )
            except Exception:
                pass
            return

        # Set mode jika belum ada
        if not current_mode:
            data["mode"] = file_mode
            db.set_session(user_id, STATE, data)

    # Download ke disk setelah lock dilepas
    file_obj  = await context.bot.get_file(doc.file_id)
    user_dir  = get_user_dir(user_id)
    merge_dir = os.path.join(user_dir, "merge")
    os.makedirs(merge_dir, exist_ok=True)

    # Simpan dengan ekstensi asli agar filter di bawah benar
    out_path = os.path.join(merge_dir, f"{msg_id}{ext}")
    
    try:
        await file_obj.download_to_drive(out_path)
        
        # Re-check state SETELAH download selesai
        async with get_user_lock(user_id):
            sess = db.get_session(user_id)
            if sess["state"] != STATE:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return
            
            data = sess["data"]
            if data.get("is_processing"):
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception:
                    pass
                return

            if not data.get("mode"):
                data["mode"] = file_mode

            data["count"]      += 1
            data["total_size"] += doc.file_size
            db.set_session(user_id, STATE, data)

        _reset_timer(user_id, context, chat_id)
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Download failed: %s", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


async def handle_merge_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _cancel_timer(user_id)

    sess = db.get_session(user_id)
    if not sess:
        return
    data = sess["data"]
    
    if update.message and update.message.text in ("done", "selesai", "/done"):
        try:
            await update.message.delete()
        except Exception:
            pass

    if data["count"] == 0:
        return

    mode = data.get("mode", "vcf")
    db.set_session(user_id, STATE_NAMING, data)

    status_msg_id = data.get("status_msg_id")
    naming_text = (
        _get_breadcrumbs(data, 2) +
        f"<blockquote><b>[ LANGKAH 2: NAMA FILE GABUNGAN ]</b>\n"
        f"Terdeteksi: <code>{data['count']}</code> file {mode.upper()}.\n\n"
        f"Ketik nama file hasil gabungan (contoh: <code>FEE</code>):</blockquote>"
    )
    naming_markup = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

    # Edit pesan yang sudah ada — jangan delete+send baru
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text=naming_text,
                parse_mode="HTML",
                reply_markup=naming_markup
            )
            data["status_msg_id"] = update.callback_query.message.message_id
        except Exception:
            pass
    elif status_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=naming_text,
                parse_mode="HTML",
                reply_markup=naming_markup
            )
        except Exception:
            pass

    db.set_session(user_id, STATE_NAMING, data)


async def handle_merge_naming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import logging
    logger = logging.getLogger(__name__)

    user_id = update.effective_user.id
    sess    = db.get_session(user_id)
    if sess["state"] != STATE_NAMING:
        return
    
    # Hapus input teks user
    try:
        await update.message.delete()
    except Exception:
        pass

    data = dict(sess["data"])
    if data.get("is_processing"):
        return
    data["is_processing"] = True
    db.set_session(user_id, STATE_NAMING, data)

    file_name   = sanitize_filename(update.message.text.strip())
    total_files = data["count"]
    mode        = data.get("mode", "vcf")

    status_msg_id = data.get("status_msg_id")
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg_id,
        text="<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang menggabungkan berkas...</blockquote>",
        parse_mode="HTML"
    )

    user_dir  = get_user_dir(user_id)
    merge_dir = os.path.join(user_dir, "merge")

    ext_filter = f".{mode}"
    files = []
    if os.path.exists(merge_dir):
        files = sorted(
            [f for f in os.listdir(merge_dir) if f.endswith(ext_filter)],
            key=lambda x: int(os.path.splitext(x)[0])
        )

    loop = asyncio.get_running_loop()

    async def update_progress(pct: int):
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=f"<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang menggabungkan <code>{total_files}</code> file {mode.upper()}... <code>{pct}%</code></blockquote>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    out_path = None
    success  = False
    try:
        if mode == "vcf":
            def parse_one(fname):
                path = os.path.join(merge_dir, fname)
                try:
                    return parse_vcf_file(path)
                except Exception as e:
                    logger.error("Merge VCF parse error %s: %s", fname, e)
                    return []

            def do_merge_vcf():
                results = {}
                for idx, fname in enumerate(files):
                    results[idx] = parse_one(fname)

                seen_tel = set()
                all_contacts = []
                for i in range(len(files)):
                    for contact in results.get(i, []):
                        tel = (contact.get("tel") or "").strip()
                        if tel and tel in seen_tel:
                            continue
                        if tel:
                            seen_tel.add(tel)
                        all_contacts.append(contact)
                return all_contacts

            if total_files > 10:
                await update_progress(10)

            all_contacts = await loop.run_in_executor(None, do_merge_vcf)
            await update_progress(90)

            if not all_contacts:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Gagal. Kontak tidak ditemukan di file VCF.</b></blockquote>",
                    parse_mode="HTML"
                )
                return

            out_path = os.path.join(user_dir, f"{file_name}.vcf")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(contacts_to_vcf(all_contacts))

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            with open(out_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f, filename=f"{file_name}.vcf",
                    read_timeout=120, write_timeout=120, connect_timeout=60
                )

            _clear_buffers(user_id)
            db.clear_session(user_id)
            success = True

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_merge_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            
            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {total_files} VCF\n"
                f"• Berkas Output : 1 VCF\n"
                f"• Nama File Hasil : {file_name}\n"
                f"• Total Kontak : {len(all_contacts):,}</blockquote>\n\n"
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

        else:
            def do_merge_txt():
                seen    = set()
                numbers = []
                for fname in files:
                    path = os.path.join(merge_dir, fname)
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                num = line.strip()
                                if num and num not in seen:
                                    seen.add(num)
                                    numbers.append(num)
                    except Exception as e:
                          logger.error("Merge TXT read error %s: %s", fname, e)
                return numbers

            if total_files > 10:
                await update_progress(10)

            numbers = await loop.run_in_executor(None, do_merge_txt)
            await update_progress(90)

            if not numbers:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Gagal. Nomor tidak ditemukan di file TXT.</b></blockquote>",
                    parse_mode="HTML"
                )
                return

            out_path = os.path.join(user_dir, f"{file_name}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(numbers))

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            with open(out_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f, filename=f"{file_name}.txt",
                    read_timeout=120, write_timeout=120, connect_timeout=60
                )

            _clear_buffers(user_id)
            db.clear_session(user_id)
            success = True

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_merge_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            from handlers.start import clear_welcome_messages
            clear_welcome_messages(user_id)
            
            box_text = (
                f"<b>[ PROSES SELESAI ]</b>\n"
                f"<blockquote>"
                f"• Total Berkas : {total_files} TXT\n"
                f"• Berkas Output : 1 TXT\n"
                f"• Nama File Hasil : {file_name}\n"
                f"• Total Nomor : {len(numbers):,}</blockquote>\n\n"
                f"<i>Silakan unduh file TXT di atas.</i>"
            )
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [final_msg.message_id])

    except Exception as e:
        logger.error("Merge error for user %s: %s", user_id, e)
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"❌ Gagal menggabungkan file: {e}\n"
                "File mentah Anda masih tersimpan aman. Silakan ketik nama file baru untuk mencoba kembali."
            )
        )
        sess = db.get_session(user_id)
        if sess and sess["state"] == STATE_NAMING:
            sess["data"]["is_processing"] = False
            db.set_session(user_id, STATE_NAMING, sess["data"])

    finally:
        if out_path:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                pass


async def handle_show_merge_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Merge File)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    _cancel_timer(user_id)
    _clear_buffers(user_id)
    
    init_data = {"count": 0, "total_size": 0, "mode": None}
    db.set_session(user_id, STATE, init_data)
    text = _waiting_text(init_data)
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )