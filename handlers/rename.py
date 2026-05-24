"""
rename.py — Ubah nama kontak di dalam file VCF.
Nama file tetap sama. Kontak diubah jadi: <NamaBaru> 1, <NamaBaru> 2, dst.
Counter berlanjut antar file sehingga urutan tidak acak.
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
from core.vcf_parser import parse_vcf_file, contacts_to_vcf

logger = logging.getLogger(__name__)

STATE_NAME = "RENAME_WAIT_NAME"
STATE_FILE = "RENAME_WAIT_FILE"

_user_locks: dict[int, asyncio.Lock] = {}
_user_timers: dict = {}  # kept for cancel_helper.py compatibility
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


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    from handlers.start import transition_to_handler, get_start_keyboard
    db.set_session(user_id, STATE_NAME, {})
    await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Ketik nama kontak baru.\nContoh: FEE",
        reply_markup=ReplyKeyboardRemove()
    )


async def handle_rename_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_NAME:
        return

    base_name = update.message.text.strip()
    if not base_name:
        await update.message.reply_text("Nama tidak boleh kosong.")
        return

    db.set_session(user_id, STATE_FILE, {"base_name": base_name, "counter": 0})
    from handlers.start import get_start_keyboard
    await update.message.reply_text(
        f"Nama kontak diset: {base_name}\n"
        f"Kirim file VCF (bisa banyak FILE sekaligus).",
        reply_markup=ReplyKeyboardRemove()
    )


async def handle_rename_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess["state"] != STATE_FILE:
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".vcf"):
        await update.message.reply_text("Kirim file dengan ekstensi .vcf.")
        return

    # Increment active requests for debouncing
    _active_requests[user_id] = _active_requests.get(user_id, 0) + 1

    user_dir = get_user_dir(user_id)
    tmp_path = os.path.join(user_dir, f"rename_{doc.file_id}.vcf")

    try:
        # Seluruh proses dalam lock: counter berlanjut dan berurutan antar file
        async with _get_lock(user_id):
            fresh = db.get_session(user_id)
            if not fresh or fresh["state"] != STATE_FILE:
                return
            data = fresh["data"]
            base_name = data["base_name"]
            start_counter = data.get("counter", 0)

            file_obj = await context.bot.get_file(doc.file_id)
            await file_obj.download_to_drive(tmp_path)

            # Parse kontak, ubah semua nama secara berurutan
            loop = asyncio.get_running_loop()
            contacts = await loop.run_in_executor(None, parse_vcf_file, tmp_path)

            renamed = []
            for i, c in enumerate(contacts, start=start_counter + 1):
                renamed.append({"name": f"{base_name} {i}", "tel": c["tel"]})

            # Update counter and batch stats
            end_counter = start_counter + len(contacts)
            data["counter"] = end_counter
            data["processed_count"] = data.get("processed_count", 0) + 1
            data["total_renamed"] = data.get("total_renamed", 0) + len(contacts)
            db.set_session(user_id, STATE_FILE, data)

            # Tulis hasil ke buffer, nama file tetap sama
            vcf_content = contacts_to_vcf(renamed)
            buf = io.BytesIO(vcf_content.encode("utf-8"))
            buf.name = doc.file_name

            await update.message.reply_document(
                document=buf,
                filename=doc.file_name,
                caption=(
                    f"{doc.file_name}\n"
                    f"{len(contacts)} kontak diubah: "
                    f"{base_name} {start_counter + 1} - {base_name} {end_counter}"
                ),
            )

    except Exception as e:
        logger.error("Rename error user %s: %s", user_id, e)
        await update.message.reply_text(f"Gagal memproses {doc.file_name}. Coba kirim ulang.")
    finally:
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

            async def _send_rename_buttons_debounced(uid, chat_id, bot, base):
                await asyncio.sleep(1.5)
                # Check active requests again to make sure no new file came in during sleep
                if _active_requests.get(uid, 0) > 0:
                    return
                sess = db.get_session(uid)
                if not sess or sess.get("state") != STATE_FILE:
                    return
                s_data = sess["data"]
                count = s_data.get("processed_count", 0)
                t_renamed = s_data.get("total_renamed", 0)
                end_counter = s_data.get("counter", 0)
                start_cnt = end_counter - t_renamed

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_rename_help", style="success"),
                        InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")
                    ]
                ])

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Rename selesai!</b>\n"
                        f"{'─' * 20}\n"
                        f"📁 Total file  : <b>{count} file</b>\n"
                        f"✏️ Total kontak : <b>{t_renamed:,} nama diubah</b>\n"
                        f"🏷️ Label baru   : <b>{base} {start_cnt + 1} - {base} {end_counter}</b>\n"
                        f"{'─' * 20}"
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            task = asyncio.get_event_loop().create_task(_send_rename_buttons_debounced(user_id, update.effective_chat.id, context.bot, base_name))
            _button_timers[user_id] = task


async def handle_show_rename_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Rename VCF)"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    db.set_session(user_id, STATE_NAME, {})
    from handlers.start import get_start_keyboard

    # Edit message in-place instead of deleting it to provide a smooth morphing transition
    try:
        await query.message.edit_text(
            text=(
                "Ketik nama kontak baru.\n"
                "Contoh: FEE"
            )
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
                "Ketik nama kontak baru.\n"
                "Contoh: FEE"
            ),
            reply_markup=ReplyKeyboardRemove()
        )