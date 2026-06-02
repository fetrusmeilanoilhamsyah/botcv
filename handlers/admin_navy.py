"""
admin_navy.py — In-memory approach, tidak ada disk sama sekali.
"""
import io
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_member
from core.vcf_parser import add_plus, contacts_to_vcf
from core.utils import sanitize_filename
import asyncio

_user_locks: dict = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


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


def _get_breadcrumbs(data: dict, step: int) -> str:
    admin_count = len(data.get("admin_numbers", []))
    navy_count = len(data.get("navy_numbers", []))
    admin_name = data.get("admin_name", "")
    navy_name = data.get("navy_name", "")
    file_name = data.get("file_name", "")

    parts = []
    
    # Step 1: Admin numbers
    if step == 1:
        parts.append(f"<b>» ADMIN: {admin_count} «</b>" if admin_count else "<b>» ADMIN «</b>")
    else:
        parts.append(f"Admin: {admin_count}" if admin_count else "Admin ○")
        
    # Step 2: Navy numbers
    if step == 2:
        parts.append(f"<b>» NAVY: {navy_count} «</b>" if navy_count else "<b>» NAVY «</b>")
    elif step > 2:
        parts.append(f"Navy: {navy_count}")
    else:
        parts.append("Navy ○")
        
    # Step 3: Admin name
    if step == 3:
        parts.append(f"<b>» LBL ADMIN: {admin_name.upper()} «</b>" if admin_name else "<b>» LBL ADMIN «</b>")
    elif step > 3:
        parts.append(f"Lbl Admin: {admin_name}")
    else:
        parts.append("Lbl Admin ○")
        
    # Step 4: Navy name
    if step == 4:
        parts.append(f"<b>» LBL NAVY: {navy_name.upper()} «</b>" if navy_name else "<b>» LBL NAVY «</b>")
    elif step > 4:
        parts.append(f"Lbl Navy: {navy_name}")
    else:
        parts.append("Lbl Navy ○")

    # Step 5: File name
    if step == 5:
        parts.append(f"<b>» FILE: {file_name.upper()} «</b>" if file_name else "<b>» FILE «</b>")
    else:
        parts.append("File ○")

    breadcrumbs = " ➔ ".join(parts)
    return (
        "<b>[ ADMIN VCF CV ]</b>\n"
        "────────────────────────────\n"
        f"{breadcrumbs}\n"
        "────────────────────────────\n\n"
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_member(update, context):
        return
    user_id = update.effective_user.id
    
    db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], {"admin_numbers": [], "navy_numbers": []})
    from handlers.start import transition_to_handler
    
    text = _get_breadcrumbs({"admin_numbers": [], "navy_numbers": []}, 1) + "<b>[ ➔ ] Menunggu nomor ADMIN...</b>\nKirim nomor <b>ADMIN</b> sekarang (satu per baris):"
    
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
        db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], sess["data"])


async def handle_admin_navy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    async with get_user_lock(user_id):
        sess = db.get_session(user_id)
        if not sess or sess["state"] not in STATES.values():
            return
        state = sess["state"]
        data = sess["data"]
        status_msg_id = data.get("status_msg_id")
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])

        if state == STATES["WAIT_ADMIN_NUMBERS"]:
            numbers = [n.strip() for n in text.splitlines() if n.strip()]
            if not numbers:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=status_msg_id,
                        text=_get_breadcrumbs(data, 1) + "Kirim minimal 1 nomor <b>ADMIN</b>:",
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                except Exception:
                    pass
                return
            data["admin_numbers"] = numbers
            db.set_session(user_id, STATES["WAIT_NAVY_NUMBERS"], data)
            
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=_get_breadcrumbs(data, 2) + "Kirim nomor <b>NAVY</b> sekarang (satu per baris):",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass

        elif state == STATES["WAIT_NAVY_NUMBERS"]:
            numbers = [n.strip() for n in text.splitlines() if n.strip()]
            if not numbers:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=status_msg_id,
                        text=_get_breadcrumbs(data, 2) + "Kirim minimal 1 nomor <b>NAVY</b>:",
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                except Exception:
                    pass
                return
            data["navy_numbers"] = numbers
            db.set_session(user_id, STATES["WAIT_ADMIN_NAME"], data)
            
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=_get_breadcrumbs(data, 3) + "Masukkan Label <b>ADMIN</b>:",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass

        elif state == STATES["WAIT_ADMIN_NAME"]:
            data["admin_name"] = text
            db.set_session(user_id, STATES["WAIT_NAVY_NAME"], data)
            
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=_get_breadcrumbs(data, 4) + "Masukkan Label <b>NAVY</b>:",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass

        elif state == STATES["WAIT_NAVY_NAME"]:
            data["navy_name"] = text
            db.set_session(user_id, STATES["WAIT_FILE_NAME"], data)
            
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text=_get_breadcrumbs(data, 5) + "Masukkan nama file hasil? Contoh: <b>ADMIN NAVY</b>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass

        elif state == STATES["WAIT_FILE_NAME"]:
            data["file_name"] = sanitize_filename(text)

            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg_id,
                    text="<b>Memproses...</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            # Build VCF di RAM langsung
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

            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=io.BytesIO(vcf_bytes),
                filename=f"{data['file_name']}.vcf"
            )

            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg_id)
            except Exception:
                pass

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("PROSES FILE LAIN", callback_data="show_admin_help", style="success"),
                    InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
                ]
            ])
            
            def _fit(val, max_len=22) -> str:
                s = str(val)
                if len(s) > max_len:
                    return s[:max_len-3] + "..."
                return s

            from handlers.start import clear_welcome_messages, register_welcome_messages
            clear_welcome_messages(user_id)
            
            box_text = (
                f"<pre><b>"
                f"┌────────────────────────────────────────┐\n"
                f"│             PROSES SELESAI             │\n"
                f"├────────────────────────────────────────┤\n"
                f"│ Total Admin    : {_fit(f'{len(admin_nums)}'):<22} │\n"
                f"│ Total Navy     : {_fit(f'{len(navy_nums)}'):<22} │\n"
                f"│ Total Kontak   : {_fit(f'{len(contacts)}'):<22} │\n"
                f"│ Nama File      : {_fit(data['file_name'] + '.vcf'):<22} │\n"
                f"└────────────────────────────────────────┘"
                f"</b></pre>\n\n"
                f"<i>Pembuatan Admin VCF selesai! Silakan unduh file di atas.</i>"
            )
            
            final_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=box_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            register_welcome_messages(user_id, [final_msg.message_id])


async def handle_show_admin_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback untuk tombol PROSES FILE LAIN (Admin VCF)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    asyncio.create_task(adb.increment_usage(user_id))
    
    db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], {"admin_numbers": [], "navy_numbers": []})

    text = _get_breadcrumbs({"admin_numbers": [], "navy_numbers": []}, 1) + "<b>[ ➔ ] Menunggu nomor ADMIN...</b>\nKirim nomor <b>ADMIN</b> sekarang (satu per baris):"
    
    try:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BATAL & KEMBALI", callback_data="back_to_start", style="danger")]])
        )
        sess = db.get_session(user_id)
        sess["data"]["status_msg_id"] = query.message.message_id
        db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], sess["data"])
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
        db.set_session(user_id, STATES["WAIT_ADMIN_NUMBERS"], sess["data"])