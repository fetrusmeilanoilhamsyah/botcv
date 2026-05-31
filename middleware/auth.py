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

    # Keyboard dengan tombol Lihat VIP dan Kembali ke Menu
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("LIHAT PAKET VIP", callback_data="show_vip_menu", style="success")],
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
    ])
    
    text = (
        "<b>AKSES TERBATAS</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "Fitur ini khusus untuk member VIP.\n"
        "Mulai dari Rp 2.000, silakan upgrade untuk menikmati semua akses fitur."
    )

    # 1. Jika dipanggil dari Callback Query (user klik tombol inline di menu)
    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception as e:
            logger.warning(f"Gagal menjawab callback query di require_member: {e}")
            
        try:
            # Edit langsung pesan callback in-place agar transisi mulus dan TIDAK menumpuk!
            await query.edit_message_text(
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return False
        except Exception as e:
            logger.warning(f"Gagal mengedit pesan callback di require_member: {e}")

    # 2. Jika dipanggil dari Command Teks (user ketik /fitur)
    from handlers.start import transition_to_handler
    chat_id = update.effective_chat.id if update.effective_chat else user_id
    
    await transition_to_handler(
        context.bot,
        user_id,
        chat_id,
        text,
        reply_markup=keyboard,
        update=update
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
