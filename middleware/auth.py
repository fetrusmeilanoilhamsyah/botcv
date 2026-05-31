"""
middleware/auth.py
Validasi membership — semua DB call async via adb agar tidak blokir event loop.
"""
import logging
from config import ADMIN_IDS, ADMIN_CONTACT

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def require_member(update, context) -> bool:
    """
    Async cek membership. DB call di thread pool — tidak blokir event loop.
    Return False + kirim pesan jika bukan member.
    """
    from database.db_async import adb
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    user_id = update.effective_user.id

    # Admin selalu lolos tanpa DB query
    if is_admin(user_id):
        return True

    member = await adb.is_member(user_id)
    if member:
        return True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("LIHAT PAKET VIP", callback_data="show_vip_menu", style="success")],
    ])
    
    # Cegah crash jika dipanggil dari callback query (di mana update.message adalah None)
    message = update.effective_message
    if update.callback_query:
        try:
            await update.callback_query.answer("Fitur khusus member VIP.", show_alert=True)
        except Exception as e:
            logger.warning(f"Gagal menjawab callback query di require_member: {e}")

    text = (
        "<b>AKSES TERBATAS</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "Fitur ini khusus untuk member VIP.\n"
        "Mulai dari Rp 2.000, silakan upgrade untuk menikmati semua akses fitur."
    )

    if message:
        await message.reply_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        chat_id = update.effective_chat.id if update.effective_chat else user_id
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    return False


async def require_admin(update, context) -> bool:
    if not is_admin(update.effective_user.id):
        message = update.effective_message
        if update.callback_query:
            try:
                await update.callback_query.answer("Perintah ini hanya untuk admin.", show_alert=True)
            except Exception as e:
                logger.warning(f"Gagal menjawab callback query di require_admin: {e}")

        if message:
            await message.reply_text("Perintah ini hanya untuk admin.")
        else:
            chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
            await context.bot.send_message(
                chat_id=chat_id,
                text="Perintah ini hanya untuk admin."
            )
        return False
    return True
