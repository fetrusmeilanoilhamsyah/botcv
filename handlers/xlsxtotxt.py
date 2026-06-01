import os
import re
import csv
import logging
import asyncio
from io import BytesIO
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.session import get_user_dir

logger = logging.getLogger(__name__)

STATE = "XLSX2TXT_COLLECTING"

_master_locks: dict[int, asyncio.Lock] = {}
_status_msg: dict = {}
_debounce_tasks: dict[int, asyncio.Task] = {}

DEBOUNCE_SECONDS = 1.2

PHONE_REGEX = re.compile(r'\+?(?:\d[\s\-\(\)\.]*){8,16}')


def _get_master_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _master_locks:
        _master_locks[user_id] = asyncio.Lock()
    return _master_locks[user_id]


def cleanup_inactive_users(inactive_ids: list) -> int:
    for uid in inactive_ids:
        _master_locks.pop(uid, None)
        _status_msg.pop(uid, None)
        task = _debounce_tasks.pop(uid, None)
        if task:
            task.cancel()
    return len(inactive_ids)


def _extract_numbers_sync(filepath: str, ext: str) -> list:
    numbers = []
    seen = set()
    try:
        def process_cell(cell_value):
            if cell_value is None:
                return
            if isinstance(cell_value, float):
                if cell_value.is_integer():
                    cell_value = int(cell_value)
                else:
                    cell_value = f"{cell_value:.0f}"
            text = str(cell_value).strip()
            for m in PHONE_REGEX.findall(text):
                clean_num = re.sub(r'[^0-9]', '', m)
                if clean_num.startswith("08"):
                    clean_num = "62" + clean_num[1:]
                if 8 <= len(clean_num) <= 15 and clean_num not in seen:
                    seen.add(clean_num)
                    numbers.append(clean_num)

        if ext == ".csv":
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for row in csv.reader(f):
                    for cell in row:
                        process_cell(cell)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
            for sheet in wb.sheetnames:
                for row in wb[sheet].iter_rows(values_only=True):
                    for cell in row:
                        process_cell(cell)
            wb.close()
    except Exception as e:
        logger.error("Error ekstrak %s: %s", filepath, e)
    return numbers


def _status_text(total_file: int, total_kontak: int) -> str:
    return (
        f"<b>Sedang mengumpulkan...</b>\n\n"
        f"File diterima: <b>{total_file}</b>\n"
        f"Nomor unik: <b>{total_kontak:,}</b>\n\n"
        f"Ketik /done jika sudah selesai."
    )


def _schedule_move(chat_id: int, bot, user_id: int):
    old_task = _debounce_tasks.get(user_id)
    if old_task and not old_task.done():
        old_task.cancel()

    async def _wait_then_move():
        await asyncio.sleep(DEBOUNCE_SECONDS)
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

    task = asyncio.create_task(_wait_then_move())
    _debounce_tasks[user_id] = task


# ─────────────────────────────────────────────────────────────────────────────

async def cmd_xlsxtotxt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from middleware.auth import require_member
    if not await require_member(update, context):
        return

    user_id = update.effective_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    from handlers.start import transition_to_handler, get_start_keyboard
    import shutil
    xlsx_dir = os.path.join(get_user_dir(user_id), "xlsxtotxt")
    shutil.rmtree(xlsx_dir, ignore_errors=True)
    os.makedirs(xlsx_dir, exist_ok=True)

    master_txt = os.path.join(xlsx_dir, "extracted_numbers.txt")
    open(master_txt, 'w').close()

    db.set_session(user_id, STATE, {"total_kontak": 0, "total_file": 0})

    msg = await transition_to_handler(
        context.bot,
        user_id,
        update.effective_chat.id,
        "Kirim file <b>.xlsx</b> atau <b>.csv</b> sekarang. Ketik /done jika sudah selesai.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]]),
        update=update
    )

    _status_msg[user_id] = msg


async def handle_xlsxtotxt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        return

    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".xlsx", ".csv"):
        await update.message.reply_text("Format tidak didukung. Gunakan .xlsx atau .csv.")
        return

    xlsx_dir = os.path.join(get_user_dir(user_id), "xlsxtotxt")
    os.makedirs(xlsx_dir, exist_ok=True)
    file_path = os.path.join(xlsx_dir, f"{doc.file_id}{ext}")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(file_path)
    except Exception as e:
        logger.error("Download error user %s: %s", user_id, e)
        await update.message.reply_text("Gagal mengunduh file. Coba kirim ulang.")
        return

    loop = asyncio.get_running_loop()
    found_numbers = await loop.run_in_executor(None, _extract_numbers_sync, file_path, ext)

    try:
        os.remove(file_path)
    except Exception:
        pass

    master_txt = os.path.join(xlsx_dir, "extracted_numbers.txt")
    new_total = 0

    async with _get_master_lock(user_id):
        try:
            with open(master_txt, 'r', encoding='utf-8') as f:
                existing = f.read().splitlines()
        except FileNotFoundError:
            existing = []

        seen = set(existing)
        combined = list(existing)
        for num in found_numbers:
            if num not in seen:
                seen.add(num)
                combined.append(num)

        with open(master_txt, 'w', encoding='utf-8') as f:
            f.write("\n".join(combined))

        new_total = len(combined)

        fresh = db.get_session(user_id)
        if fresh and fresh.get("state") == STATE:
            data = fresh["data"]
            data["total_file"] += 1
            data["total_kontak"] = new_total
            db.set_session(user_id, STATE, data)

    # Pindahkan pesan status ke bawah (debounce)
    _schedule_move(update.effective_chat.id, context.bot, user_id)


async def handle_xlsxtotxt_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    task = _debounce_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    data = sess["data"]
    total = data.get("total_kontak", 0)

    old = _status_msg.pop(user_id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

    if total == 0:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_xlsxtotxt_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)
        await update.message.reply_text("Tidak ada nomor yang ditemukan dari file yang dikirim.", reply_markup=keyboard)
        db.clear_session(user_id)
        import shutil
        shutil.rmtree(os.path.join(get_user_dir(user_id), "xlsxtotxt"), ignore_errors=True)
        return

    xlsx_dir = os.path.join(get_user_dir(user_id), "xlsxtotxt")
    master_txt = os.path.join(xlsx_dir, "extracted_numbers.txt")

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_xlsxtotxt_help", style="success"),
                InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
            ]
        ])
        with open(master_txt, 'rb') as f:
            buffer = BytesIO(f.read())
            buffer.name = "Hasil_Ekstrak.txt"

        total_file = data.get("total_file", 0)
        await update.message.reply_document(
            document=buffer,
            caption=(
                f"Ekstraksi selesai!\n\n"
                f"File diproses: <b>{total_file}</b>\n"
                f"Nomor unik: <b>{total:,}</b>"
            ),
            parse_mode="HTML",
        )
        from handlers.start import clear_welcome_messages
        clear_welcome_messages(user_id)
        await update.message.reply_text(
            "Proses selesai. Silakan unduh file hasil ekstraksi di atas.",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error("Error kirim hasil xlsx: %s", e)
        await update.message.reply_text("Gagal mengirim hasil. Coba ulangi.")
    finally:
        db.clear_session(user_id)
        import shutil
        shutil.rmtree(xlsx_dir, ignore_errors=True)


async def handle_show_xlsxtotxt_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Excel/CSV to TXT)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))

    import shutil
    xlsx_dir = os.path.join(get_user_dir(user_id), "xlsxtotxt")
    shutil.rmtree(xlsx_dir, ignore_errors=True)
    os.makedirs(xlsx_dir, exist_ok=True)

    master_txt = os.path.join(xlsx_dir, "extracted_numbers.txt")
    open(master_txt, 'w').close()

    db.set_session(user_id, STATE, {"total_kontak": 0, "total_file": 0})
    from handlers.start import get_start_keyboard

    # Edit the message in-place instead of deleting it to provide a smooth morphing transition
    try:
        await query.message.edit_text(
            text="Kirim file <b>.xlsx</b> atau <b>.csv</b> sekarang. Ketik /done jika sudah selesai.",
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
            text="Kirim file <b>.xlsx</b> atau <b>.csv</b> sekarang. Ketik /done jika sudah selesai.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )