"""
middleware/auth.py
Validasi membership — semua DB call async via adb agar tidak blokir event loop.
"""
import logging
import time
from config import ADMIN_IDS, ADMIN_CONTACT, FORCE_SUB_CHANNEL, FORCE_SUB_LINK

logger = logging.getLogger(__name__)

# RAM Cache for membership checks to prevent API rate-limiting/spamming
_membership_cache = {}
MEMBERSHIP_CACHE_TTL = 3600  # 1 jam — mengurangi beban API Telegram secara drastis


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


import asyncio

# Semaphore: max 5 concurrent get_chat_member calls ke Telegram API
# Mencegah flood setelah restart saat 188 user aktif bersamaan cek membership
_membership_api_sem: asyncio.Semaphore | None = None

def _get_membership_api_sem() -> asyncio.Semaphore:
    global _membership_api_sem
    if _membership_api_sem is None:
        _membership_api_sem = asyncio.Semaphore(5)
    return _membership_api_sem


async def check_channel_membership(bot, user_id: int) -> bool:
    """Checks if a user is a member of the configured Telegram channel, with RAM caching."""
    if not FORCE_SUB_CHANNEL:
        return True

    now = time.time()
    if user_id in _membership_cache:
        if now - _membership_cache[user_id] < MEMBERSHIP_CACHE_TTL:
            return True

    try:
        channel_id = FORCE_SUB_CHANNEL
        if str(channel_id).strip().replace("-", "").isdigit():
            channel_id = int(channel_id)

        # Rate-limit concurrent API calls: max 5 sekaligus agar tidak flood Telegram
        async with _get_membership_api_sem():
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)

        if member.status in ('creator', 'administrator', 'member', 'restricted'):
            _membership_cache[user_id] = now
            return True
    except Exception as e:
        logger.warning(f"Error checking channel membership for user {user_id}: {e}")
        # Fail open hanya jika error bukan user related (e.g. bot bukan admin channel)
        pass
    return False


async def require_channel_join(update, context) -> bool:
    """
    Checks if user is in required channel. If not, prompts to join and returns False.
    """
    if not FORCE_SUB_CHANNEL:
        return True

    user_id = update.effective_user.id

    # Admin always bypasses this check
    if is_admin(user_id):
        return True

    is_member = await check_channel_membership(context.bot, user_id)
    if is_member:
        return True

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    channel_display = FORCE_SUB_CHANNEL if str(FORCE_SUB_CHANNEL).startswith("@") else "@tutorialnotceve"
    text = (
        "<b>[ STATUS: AKSES DITOLAK ]</b>\n"
        f"Kamu belum bergabung ke channel resmi <b>{channel_display}</b>.\n\n"
        "Untuk menggunakan bot ini, ikuti langkah berikut:\n\n"
        "<b>1.</b> Klik tombol <b>MASUK CHANNEL</b> di bawah\n"
        "<b>2.</b> Bergabung ke channel kami\n"
        "<b>3.</b> Kembali ke sini, lalu klik <b>SUDAH BERGABUNG \u2713</b>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\ud83d\udce2 MASUK CHANNEL", url=FORCE_SUB_LINK, style="primary")],
        [InlineKeyboardButton("\u2705 SUDAH BERGABUNG", callback_data="check_channel_join", style="success")]
    ])

    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        try:
            msg = await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard)
            from handlers.start import register_welcome_messages
            register_welcome_messages(user_id, [msg.message_id])
            return False
        except Exception as e:
            logger.warning(f"Gagal mengedit pesan callback di require_channel_join: {e}")
            # Fallback: edit gagal, kirim pesan baru via transition_to_handler

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

    # Keyboard dengan tombol Lihat VIP, VIP Gratis, dan Kembali ke Menu
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("LIHAT PAKET", callback_data="show_vip_menu", style="primary"),
            InlineKeyboardButton("VIP GRATIS", callback_data="show_referral_menu", style="primary")
        ],
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
    ])
    
    text = (
        "<b>[ STATUS: VIP DIPERLUKAN ]</b>\n"
        "Masa coba gratis <b>7 hari</b> Anda telah berakhir.\n"
        "Fitur ini hanya tersedia untuk member <b>VIP</b>.\n\n"
        "Pilih salah satu opsi untuk melanjutkan:\n\n"
        "• <b>LIHAT PAKET</b> — Upgrade mulai dari <b>Rp 2.000</b>\n"
        "• <b>VIP GRATIS</b> — Undang teman &amp; tukarkan poin"
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
