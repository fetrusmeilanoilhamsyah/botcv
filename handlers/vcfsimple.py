"""
vcfsimple.py - Disk-based, terima paralel, sort by message_id.
Single-Message Morphing Wizard, bebas emoji, super simpel.
Flow: Upload TXT -> PROSES -> VCF langsung dikirim.
- Nama kontak : nomor telepon tanpa tanda +
- Nama file   : mengikuti nama file TXT yang diunggah
- Tidak ada pertanyaan jumlah per file, urutan, atau format pengiriman.
"""
import os
import shutil
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import add_plus
from core.utils import sanitize_filename

VS_WAIT_FILE  = "VS_WAIT_FILE"
VS_COLLECTING = "VS_COLLECTING"

from config import (
    MAX_FILES_PER_SESSION as MAX_FILES,
    MAX_UPLOAD_SIZE_MB as MAX_SIZE_MB,
    SEND_MAX_RETRIES,
    SEND_RETRY_DELAY,
    FILE_READ_TIMEOUT,
    FILE_WRITE_TIMEOUT,
    FILE_CONNECT_TIMEOUT,
)

def _fit(val, max_len=22) -> str:
    s = str(val)
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s

def _get_breadcrumbs(data: dict) -> str:
    count = data.get("count", 0)
    file_name = data.get("file_name", "")
    total_contacts = data.get("total_contacts", 0)

    parts = []

    # Berkas
    if count:
        parts.append(f"Berkas: <code>{count}</code>")
    else:
        parts.append("Berkas: ➖")

    # Nama kontak selalu sesuai nomor
    parts.append("Nama: <code>Sesuai Nomor</code>")

    # File
    if file_name:
        parts.append(f"File: <code>{file_name}</code>")
    else:
        parts.append("File: ➖")

    # Total kontak
    if total_contacts:
        parts.append(f"Kontak: <code>{total_contacts:,}</code>")

    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ TXT ➔ VCF SIMPLE ]</b>\n"
                f"<blockquote>{breadcrumbs}</blockquote>\n"
        "\n"
    )

def _waiting_text(data: dict) -> str:
    return (
        _get_breadcrumbs(data) +
        f"<blockquote><b>[ STATUS: WAITING FOR UPLOAD ]</b>\n"
        f"Silakan kirim satu atau beberapa file <code>.txt</code> sekarang.\n\n"
        f"<b>Batas Sesi:</b>\n"
        f"• Maksimum upload: <code>{MAX_FILES} file</code>\n"
        f"• Maksimum ukuran: <code>{MAX_SIZE_MB} MB</code> per file</blockquote>"
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
            if sess and sess.get("state") == VS_WAIT_FILE:
                data = sess["data"]
                jumlah = data["count"]
                text = (
                    _get_breadcrumbs(data) +
                    f"<blockquote><b>[ STATUS: BERKAS DITERIMA ]</b>\n"
                    f"Berhasil mengunduh <code>{jumlah}</code> berkas TXT.\n"
                    f"Total kontak terdeteksi: <code>{data.get('total_contacts', 0)}</code> baris.\n\n"
                    f"Silakan pilih tindakan di bawah:</blockquote>"
                )
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("PROSES SEKARANG", callback_data="done", style="success"),
                        InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                    ]
                ])

                # Hapus status message lama di atas
                status_msg_id = data.get("status_msg_id")
                if status_msg_id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                        return
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
                from handlers.start import register_welcome_messages
                register_welcome_messages(user_id, [msg.message_id])
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

    init_data = {"count": 0, "total_size": 0, "total_contacts": 0, "file_name": ""}
    db.set_session(user_id, VS_WAIT_FILE, init_data)

    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        _waiting_text(init_data),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
        ]]),
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
        # Jangan hapus file user — cukup beri peringatan lewat edit status message
        try:
            status_msg_id = sess["data"].get("status_msg_id")
            if status_msg_id:
                sent_name = doc.file_name if doc and doc.file_name else "file tersebut"
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=(
                        _get_breadcrumbs(sess["data"]) +
                        f"<blockquote>⚠️ <b>[ FORMAT SALAH ]</b>\n"
                        f"<code>{sent_name}</code> bukan berkas <code>.txt</code>.\n\n"
                        f"Kirim ulang berkas dengan format <code>.txt</code>.</blockquote>"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                    ]])
                )
                await asyncio.sleep(10)
                # Kembalikan ke tampilan waiting semula
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=_waiting_text(sess["data"]),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
                    ]])
                )
        except Exception:
            pass
        return

    msg_id = update.message.message_id

    # File .txt valid — JANGAN dihapus, biarkan tetap di chat
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

            # Kunci nama file dari file TXT pertama yang diunggah
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
    """User memicu selesai kirim file -> langsung proses"""
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

    if sess["state"] != VS_WAIT_FILE:
        return

    if data["count"] == 0:
        return
    if data.get("is_processing"):
        return

    data["is_processing"] = True
    db.set_session(user_id, VS_COLLECTING, data)
    await handle_vs_process(update, context)

async def handle_vs_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses pembuatan berkas VCF - semua kontak dalam satu file"""
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
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg_id,
            text="<blockquote><b>[ SYSTEM: PROCESSING DATA ]</b>\nSedang memproses dan menyusun data VCF...</blockquote>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    user_dir = get_user_dir(user_id)
    vs_dir = os.path.join(user_dir, "vcfsimple")

    files = []
    if os.path.exists(vs_dir):
        files = [f for f in os.listdir(vs_dir) if f.endswith('.txt')]
        files.sort(key=lambda x: int(x.split('_')[0]))

    loop = asyncio.get_running_loop()

    def do_build():
        results = []  # list of (vcf_basename, vcf_bytes, contact_count)
        for f in files:
            # Ambil nama asli TXT: hapus prefix msg_id_ di depan
            original_name = f.split('_', 1)[1] if '_' in f else f
            vcf_base = os.path.splitext(original_name)[0]  # tanpa .txt

            path = os.path.join(vs_dir, f)
            numbers = []
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as file_in:
                    for line in file_in:
                        num = line.strip()
                        if num:
                            numbers.append(add_plus(num))
            except Exception:
                pass

            vcf_lines = []
            for num in numbers:
                name = num.lstrip("+")
                vcf_lines.append(
                    f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL;TYPE=CELL:{num}\nEND:VCARD"
                )
            content = ("\n".join(vcf_lines) + "\n").encode("utf-8")
            results.append((vcf_base, content, len(numbers)))
        return results

    results = await loop.run_in_executor(None, do_build)

    import io
    import logging as _log
    _logger = _log.getLogger(__name__)

    try:
        total_input = len(files)
        total_contacts = sum(r[2] for r in results)

        if not results or total_contacts == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<blockquote>⚠️ <b>Gagal. Data tidak ditemukan atau berkas kosong.</b></blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # --- Kirim semua VCF satu per satu ---
        total_files = len(results)
        sent = 0
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id,
                text=f"<blockquote><b>[ SYSTEM: SENDING VCF ]</b>\nMengirim berkas VCF: <code>0</code> / <code>{total_files}</code> file terkirim.</blockquote>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        for vcf_base, vcf_bytes, _ in results:
            buf = io.BytesIO(vcf_bytes)
            vcf_filename = f"{vcf_base}.vcf"
            for attempt in range(SEND_MAX_RETRIES):
                try:
                    buf.seek(0)
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=buf,
                        filename=vcf_filename,
                        read_timeout=FILE_READ_TIMEOUT,
                        write_timeout=FILE_WRITE_TIMEOUT,
                        connect_timeout=FILE_CONNECT_TIMEOUT,
                    )
                    sent += 1
                    try:
                        await context.bot.edit_message_text(
                            chat_id=update.effective_chat.id,
                            message_id=status_msg_id,
                            text=f"<blockquote><b>[ SYSTEM: SENDING VCF ]</b>\nMengirim berkas VCF: <code>{sent}</code> / <code>{total_files}</code> file terkirim.</blockquote>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    break
                except Exception as ex:
                    _logger.error(f"[VCFSIMPLE] Gagal kirim {vcf_filename} attempt {attempt+1}: {ex}")
                    if attempt == SEND_MAX_RETRIES - 1:
                        sent += 1
                    else:
                        await asyncio.sleep(SEND_RETRY_DELAY)

        output_label = f"{sent} VCF"

        # --- Laporan selesai ---
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=status_msg_id
            )
        except Exception:
            pass

        from handlers.start import clear_welcome_messages, register_welcome_messages
        clear_welcome_messages(user_id)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_vcfsimple_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        box_text = (
            f"<b>[ PROSES SELESAI ]</b>\n"
            f"<blockquote>"
            f"• Total Berkas : {total_input} TXT\n"
            f"• Output : {output_label}\n"
            f"• Nama Kontak : Sesuai Nomor\n"
            f"• Total Kontak : {total_contacts:,}</blockquote>\n\n"
            f"<i>Silakan unduh file di atas.</i>"
        )
        final_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=box_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
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

    init_data = {"count": 0, "total_size": 0, "total_contacts": 0, "file_name": ""}
    db.set_session(user_id, VS_WAIT_FILE, init_data)

    text = _waiting_text(init_data)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")
    ]])

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
