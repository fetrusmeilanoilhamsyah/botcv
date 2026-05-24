"""
admin_navy.py — In-memory approach, tidak ada disk sama sekali.
"""
import io
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from core.vcf_parser import add_plus, contacts_to_vcf
from core.utils import sanitize_filename
import asyncio

_user_locks: dict = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


def cleanup_inactive_users(inactive_ids: list) -> int:
    cleaned = 0
    for uid in inactive_ids:
        _user_locks.pop(uid, None)
        cleaned += 1
    return cleaned


STATES = {
    "WAIT_ADMIN_NUMBERS": "AN_STEP1",
    "WAIT_NAVY_NUMBERS":  "AN_STEP2",
    "WAIT_ADMIN_NAME":    "AN_STEP3",
    "WAIT_NAVY_NAME":     "AN_STEP4",
    "WAIT_FILE_NAME":     "AN_STEP5",
}


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    
    from handlers.start import delete_welcome_messages
    await delete_welcome_messages(context.bot, user_id, update.effective_chat.id)

    db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], {})
    from handlers.start import get_start_keyboard
    await update.message.reply_text(
        "Nomor ADMIN (satu per baris):",
        reply_markup=get_start_keyboard()
    )


async def handle_admin_navy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    async with get_user_lock(user_id):
        sess = db.get_session(user_id)
        state = sess["state"]
        data = sess["data"]
        
        if state == STATES["WAIT_ADMIN_NUMBERS"]:
            numbers = [n.strip() for n in text.splitlines() if n.strip()]
            if not numbers:
                await update.message.reply_text("Nomor tidak boleh kosong. Kirim minimal 1 nomor ADMIN:")
                return
            data["admin_numbers"] = numbers
            db.set_session(user_id, STATES["WAIT_NAVY_NUMBERS"], data)
            from handlers.start import get_start_keyboard
            await update.message.reply_text("Nomor NAVY (satu per baris):", reply_markup=get_start_keyboard())

        elif state == STATES["WAIT_NAVY_NUMBERS"]:
            numbers = [n.strip() for n in text.splitlines() if n.strip()]
            if not numbers:
                await update.message.reply_text("Nomor tidak boleh kosong. Kirim minimal 1 nomor NAVY:")
                return
            data["navy_numbers"] = numbers
            db.set_session(user_id, STATES["WAIT_ADMIN_NAME"], data)
            from handlers.start import get_start_keyboard
            await update.message.reply_text("Label ADMIN:", reply_markup=get_start_keyboard())

        elif state == STATES["WAIT_ADMIN_NAME"]:
            data["admin_name"] = text
            db.set_session(user_id, STATES["WAIT_NAVY_NAME"], data)
            from handlers.start import get_start_keyboard
            await update.message.reply_text("Label NAVY:", reply_markup=get_start_keyboard())

        elif state == STATES["WAIT_NAVY_NAME"]:
            data["navy_name"] = text
            db.set_session(user_id, STATES["WAIT_FILE_NAME"], data)
            from handlers.start import get_start_keyboard
            await update.message.reply_text("Nama file:", reply_markup=get_start_keyboard())

        elif state == STATES["WAIT_FILE_NAME"]:
            data["file_name"] = sanitize_filename(text)

            # Build VCF di RAM langsung, tidak perlu disk sama sekali
            contacts = []
            admin_nums = data["admin_numbers"]
            navy_nums = data["navy_numbers"]
            admin_name = data["admin_name"]
            navy_name = data["navy_name"]

            for i, num in enumerate(admin_nums, start=1):
                contacts.append({
                    "name": f"{admin_name}{i}" if len(admin_nums) > 1 else admin_name,
                    "tel": add_plus(num.strip())
                })

            for i, num in enumerate(navy_nums, start=1):
                contacts.append({
                    "name": f"{navy_name}{i}" if len(navy_nums) > 1 else navy_name,
                    "tel": add_plus(num.strip())
                })

            vcf_bytes = contacts_to_vcf(contacts).encode("utf-8")
            db.clear_session(user_id)

            await update.message.reply_document(
                document=io.BytesIO(vcf_bytes),
                filename=f"{data['file_name']}.vcf"
            )

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_admin_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")
                ]
            ])
            await update.message.reply_text(
                "Proses pembuatan Admin VCF selesai.",
                reply_markup=keyboard
            )


async def handle_show_admin_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Admin VCF)"""
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], {})
    from handlers.start import get_start_keyboard
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Nomor ADMIN (satu per baris):",
        reply_markup=get_start_keyboard()
    )