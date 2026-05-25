"""
handlers/duplikat.py — Pembersih nomor kontak duplikat untuk file VCF dan TXT.
"""
import os
import io
import asyncio
import logging
from telegram import ReplyKeyboardRemove, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from middleware.session import get_user_dir
from core.vcf_parser import parse_vcf_file, contacts_to_vcf, add_plus

logger = logging.getLogger(__name__)

STATE = "DUPLICAT_WAIT_FILE"
_user_locks: dict[int, asyncio.Lock] = {}
_button_timers: dict[int, asyncio.Task] = {}
_active_requests: dict[int, int] = {}


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


def cleanup_inactive_users(inactive_ids: list) -> int:
    for uid in inactive_ids:
        _user_locks.pop(uid, None)
        _active_requests.pop(uid, None)
    return len(inactive_ids)


async def cmd_duplikat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler untuk /duplikat"""
    if not await require_member(update, context):
        return
        
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    # Set session state
    db.set_session(user_id, STATE, {})
    
    from handlers.start import transition_to_handler, get_start_keyboard
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "KIRIM FILE KONTAK (VCF ATAU TXT)\n\nSilakan kirim file VCF atau TXT berupa dokumen yang ingin Anda bersihkan dari nomor kontak duplikat.",
        reply_markup=ReplyKeyboardRemove(),
        update=update
    )



async def handle_duplikat_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """File handler untuk /duplikat"""
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        await update.message.reply_text("Kirim file dokumen VCF atau TXT.")
        return

    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".txt", ".vcf"):
        await update.message.reply_text("Ekstensi file tidak didukung. Kirim file dengan ekstensi .txt atau .vcf.")
        return

    # Increment active requests for debouncing
    _active_requests[user_id] = _active_requests.get(user_id, 0) + 1

    user_dir = get_user_dir(user_id)
    tmp_path = os.path.join(user_dir, f"duplikat_{doc.file_id}{ext}")
    proc_msg = None

    try:
        async with _get_lock(user_id):
            # Double check state
            fresh = db.get_session(user_id)
            if not fresh or fresh.get("state") != STATE:
                return

            # Kirim status memproses
            proc_msg = await update.message.reply_text("MEMPROSES FILE...")

            # Download file
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(tmp_path)

            total_awal = 0
            total_unik = 0
            total_duplikat = 0

            loop = asyncio.get_running_loop()

            if ext == ".vcf":
                # ── PROSES VCF ──
                def process_vcf():
                    contacts = parse_vcf_file(tmp_path)
                    t_awal = len(contacts)
                    
                    seen_numbers = set()
                    unique_contacts = []
                    
                    for c in contacts:
                        num = c["tel"]
                        if num not in seen_numbers:
                            seen_numbers.add(num)
                            unique_contacts.append(c)
                            
                    t_unik = len(unique_contacts)
                    t_dup = t_awal - t_unik
                    content = contacts_to_vcf(unique_contacts)
                    return t_awal, t_unik, t_dup, content

                total_awal, total_unik, total_duplikat, clean_content = await loop.run_in_executor(None, process_vcf)
                
            else:
                # ── PROSES TXT ──
                def process_txt():
                    with open(tmp_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                        lines = [line.strip() for line in f if line.strip()]
                    
                    t_awal = len(lines)
                    seen_numbers = set()
                    unique_numbers = []
                    
                    for line in lines:
                        cleaned = add_plus(line)
                        if cleaned and cleaned not in seen_numbers:
                            seen_numbers.add(cleaned)
                            unique_numbers.append(cleaned)
                            
                    t_unik = len(unique_numbers)
                    t_dup = t_awal - t_unik
                    content = "\n".join(unique_numbers) + "\n"
                    return t_awal, t_unik, t_dup, content

                total_awal, total_unik, total_duplikat, clean_content = await loop.run_in_executor(None, process_txt)

            # Buat file buffer untuk dikirim kembali
            buf = io.BytesIO(clean_content.encode("utf-8"))
            clean_filename = f"CLEAN_{doc.file_name}"
            buf.name = clean_filename

            # Sediakan stats di session untuk debouncing
            fresh = db.get_session(user_id)
            s_data = fresh.get("data", {}) if fresh else {}
            s_data["processed_count"] = s_data.get("processed_count", 0) + 1
            s_data["total_awal"] = s_data.get("total_awal", 0) + total_awal
            s_data["total_duplikat"] = s_data.get("total_duplikat", 0) + total_duplikat
            s_data["total_unik"] = s_data.get("total_unik", 0) + total_unik
            db.set_session(user_id, STATE, s_data)

            # Hapus pesan "MEMPROSES FILE..."
            try:
                await proc_msg.delete()
                proc_msg = None
            except Exception:
                pass

            # Kirim file hasil pembersihan
            await update.message.reply_document(
                document=buf,
                filename=clean_filename,
                caption=(
                    f"PEMBERSIHAN DUPLIKAT SELESAI\n\n"
                    f"File       : {doc.file_name}\n"
                    f"Total Awal : {total_awal} kontak\n"
                    f"Dihapus    : {total_duplikat} kontak duplikat\n"
                    f"Total Unik : {total_unik} kontak tersisa"
                )
            )

    except Exception as e:
        logger.error("Deduplication error for user %s: %s", user_id, e)
        if proc_msg:
            try:
                await proc_msg.delete()
            except Exception:
                pass
        await update.message.reply_text("GAGAL MEMPROSES FILE. SILAKAN COBA LAGI.")

    finally:
        # Hapus file sementara dari disk
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

        # Decrement active requests
        _active_requests[user_id] = max(0, _active_requests.get(user_id, 0) - 1)

        # Trigger debounced button/summary message only when ALL active files are finished
        if _active_requests[user_id] == 0:
            old_timer = _button_timers.pop(user_id, None)
            if old_timer and not old_timer.done():
                old_timer.cancel()

            async def _send_duplikat_buttons_debounced(uid, chat_id, bot):
                await asyncio.sleep(1.5)
                # Check active requests again to make sure no new file came in during sleep
                if _active_requests.get(uid, 0) > 0:
                    return
                sess = db.get_session(uid)
                if not sess or sess.get("state") != STATE:
                    return
                d_data = sess["data"]
                count = d_data.get("processed_count", 0)
                t_awal = d_data.get("total_awal", 0)
                t_dup = d_data.get("total_duplikat", 0)
                t_unik = d_data.get("total_unik", 0)

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_duplikat_help", style="success"),
                        InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")
                    ]
                ])

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Pembersihan selesai!</b>\n"
                        f"{'─' * 20}\n"
                        f"📁 Total file  : <b>{count} file</b>\n"
                        f"👥 Total awal  : <b>{t_awal:,} kontak</b>\n"
                        f"🗑️ Total hapus : <b>{t_dup:,} duplikat</b>\n"
                        f"👥 Total unik  : <b>{t_unik:,} tersisa</b>\n"
                        f"{'─' * 20}"
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                db.clear_session(uid)

            task = asyncio.get_event_loop().create_task(_send_duplikat_buttons_debounced(user_id, update.effective_chat.id, context.bot))
            _button_timers[user_id] = task


async def handle_show_duplikat_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol BERSIHKAN FILE LAIN"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db.set_session(user_id, STATE, {})

    from handlers.start import get_start_keyboard

    # Edit message in-place instead of deleting it to provide a smooth morphing transition
    try:
        await query.message.edit_text(
            text=(
                "KIRIM FILE KONTAK (VCF ATAU TXT)\n\n"
                "Silakan kirim file VCF atau TXT berupa dokumen yang ingin Anda bersihkan dari nomor kontak duplikat."
            ),
            parse_mode="HTML"
        )
    except Exception:
        # Fallback if editing fails
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "KIRIM FILE KONTAK (VCF ATAU TXT)\n\n"
                "Silakan kirim file VCF atau TXT berupa dokumen yang ingin Anda bersihkan dari nomor kontak duplikat."
            ),
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )