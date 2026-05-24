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
        [InlineKeyboardButton("Lihat Paket VIP", callback_data="show_vip_menu")],
    ])
    await update.message.reply_text(
        "Fitur ini khusus member VIP.\n"
        "Mulai dari Rp 5.000, aktif otomatis via QRIS.",
        reply_markup=keyboard,
    )
    return False


async def require_admin(update, context) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Perintah ini hanya untuk admin.")
        return False
    return True
