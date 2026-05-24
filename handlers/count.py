import os
import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.session import get_user_dir

logger = logging.getLogger(__name__)

STATE = "COUNT_COLLECTING"

_user_locks: dict[int, asyncio.Lock] = {}
_status_msg: dict = {}
_debounce_tasks: dict[int, asyncio.Task] = {}

DEBOUNCE_SECONDS = 1.2  # tunggu 1.2 detik setelah file terakhir baru pindahkan pesan


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


def cleanup_inactive_users(inactive_ids: list) -> int:
    for uid in inactive_ids:
        _user_locks.pop(uid, None)
        _status_msg.pop(uid, None)
        task = _debounce_tasks.pop(uid, None)
        if task:
            task.cancel()
    return len(inactive_ids)


def _count_contacts_sync(filepath: str, ext: str) -> int:
    count = 0
    try:
        if ext == ".txt":
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip():
                        count += 1
        elif ext == ".vcf":
            with open(filepath, "rb") as f:
                count = f.read().count(b"BEGIN:VCARD")
    except Exception as e:
        logger.error("Error menghitung %s: %s", filepath, e)
    return count


def _status_text(total_file: int, total_kontak: int) -> str:
    return (
        f"📂 <b>Sedang mengumpulkan...</b>\n\n"
        f"├ File diterima : <b>{total_file}</b>\n"
        f"└ Total kontak  : <b>{total_kontak:,}</b>\n\n"
        f"<i>Ketik /done jika sudah selesai.</i>"
    )


async def _do_move_status(chat_id: int, bot, user_id: int):
    """Setelah debounce selesai: hapus pesan lama, kirim baru di bawah."""
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    data = sess["data"]
    text = _status_text(data["total_file"], data["total_kontak"])

    old = _status_msg.get(user_id)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

    try:
        new_msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        _status_msg[user_id] = new_msg
    except Exception:
        pass


def _schedule_move(chat_id: int, bot, user_id: int):
    """Batalkan debounce sebelumnya, jadwalkan yang baru."""
    old_task = _debounce_tasks.get(user_id)
    if old_task and not old_task.done():
        old_task.cancel()

    async def _wait_then_move():
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await _do_move_status(chat_id, bot, user_id)

    task = asyncio.get_event_loop().create_task(_wait_then_move())
    _debounce_tasks[user_id] = task


# ─────────────────────────────────────────────────────────────────────────────

async def cmd_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from middleware.auth import require_member
    if not await require_member(update, context):
        return

    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    from handlers.start import delete_welcome_messages
    await delete_welcome_messages(context.bot, user_id, update.effective_chat.id)

    import shutil
    count_dir = os.path.join(get_user_dir(user_id), "count")
    shutil.rmtree(count_dir, ignore_errors=True)
    os.makedirs(count_dir, exist_ok=True)

    db.set_session(user_id, STATE, {"total_kontak": 0, "total_file": 0})

    msg = await update.message.reply_text(
        "📂 <b>Kirim file TXT atau VCF</b>\n\n"
        "Bisa kirim banyak sekaligus.\n"
        "Ketik /done jika sudah selesai.",
        parse_mode="HTML",
    )
    _status_msg[user_id] = msg


async def handle_count_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        return

    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".txt", ".vcf"):
        return

    count_dir = os.path.join(get_user_dir(user_id), "count")
    os.makedirs(count_dir, exist_ok=True)
    file_path = os.path.join(count_dir, f"{doc.file_id}{ext}")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(file_path)
    except Exception as e:
        logger.error("Download error user %s: %s", user_id, e)
        return

    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(None, _count_contacts_sync, file_path, ext)

    try:
        os.remove(file_path)
    except Exception:
        pass

    async with _get_lock(user_id):
        fresh = db.get_session(user_id)
        if not fresh or fresh.get("state") != STATE:
            return
        data = fresh["data"]
        data["total_kontak"] += count
        data["total_file"] += 1
        db.set_session(user_id, STATE, data)

    # Jadwalkan pindah pesan ke bawah — debounce reset tiap ada file baru
    _schedule_move(update.effective_chat.id, context.bot, user_id)


async def handle_count_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Batalkan debounce yang mungkin masih pending
    task = _debounce_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    data = sess["data"]
    total_file = data.get("total_file", 0)
    total_kontak = data.get("total_kontak", 0)

    if total_file == 0:
        await update.message.reply_text("Belum ada file yang dikirim.")
        return

    # Hapus pesan status
    old = _status_msg.pop(user_id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

    avg = total_kontak // total_file if total_file else 0

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")],
        [InlineKeyboardButton("HITUNG FILE LAIN", callback_data="show_count_help", style="success")]
    ])

    await update.message.reply_text(
        f"LAPORAN HITUNGAN\n"
        f"{'─' * 20}\n"
        f"Total File : {total_file}\n"
        f"Total Kontak : {total_kontak:,}\n"
        f"Rata-rata/File : {avg:,}\n"
        f"{'─' * 20}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    db.clear_session(user_id)
    import shutil
    shutil.rmtree(os.path.join(get_user_dir(user_id), "count"), ignore_errors=True)


async def handle_show_count_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol HITUNG FILE LAIN"""
    query = update.callback_query
    await query.answer()

    # Hapus pesan laporan lama agar tidak menumpuk di chat
    try:
        await query.message.delete()
    except Exception:
        pass

    user_id = query.from_user.id
    import shutil
    count_dir = os.path.join(get_user_dir(user_id), "count")
    shutil.rmtree(count_dir, ignore_errors=True)
    os.makedirs(count_dir, exist_ok=True)

    db.set_session(user_id, STATE, {"total_kontak": 0, "total_file": 0})

    msg = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "📂 <b>Kirim file TXT atau VCF</b>\n\n"
            "Bisa kirim banyak sekaligus.\n"
            "Ketik /done jika sudah selesai."
        ),
        parse_mode="HTML"
    )
    _status_msg[user_id] = msg

