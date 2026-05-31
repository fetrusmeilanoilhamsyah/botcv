import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.session import clear_user_dir, clear_all_sessions
from handlers.cancel_helper import cancel_all


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1. Jalankan pembersihan sesi RAM & disk di background
    async def cleanup_bg():
        cancel_all(user_id)
        db.clear_session(user_id)
        clear_user_dir(user_id)
    
    asyncio.create_task(cleanup_bg())

    # Hapus pesan perintah /reset dari user agar obrolan bersih
    try:
        await update.message.delete()
    except Exception:
        pass

    # 2. Kirim menu utama yang fresh dan bersih
    from handlers.start import send_fresh_start_menu
    first_name = update.effective_user.first_name or "Kawan"
    await send_fresh_start_menu(context.bot, user_id, chat_id, first_name)

    # 4. Tampilkan menu tambahan Admin secara bersih jika user adalah Admin
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("RESET TOTAL DATABASE", callback_data="admin_db_reset_confirm", style="danger")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>MENU ADMINISTRATOR</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                "Penghapusan total seluruh database."
            ),
            parse_mode="HTML",
            reply_markup=reply_markup
        )


async def handle_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle konfirmasi reset dari admin"""
    query = update.callback_query

    # Verifikasi admin untuk callback query
    from config import ADMIN_IDS
    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("Akses ditolak.", show_alert=True)
        return

    await query.answer()

    data = query.data

    if data == "admin_db_reset_confirm":
        keyboard = [
            [
                InlineKeyboardButton("YA, HAPUS SEMUA", callback_data="admin_db_reset_final", style="danger"),
                InlineKeyboardButton("BATAL", callback_data="admin_db_reset_cancel", style="primary")
            ]
        ]
        await query.edit_message_text(
            text=(
                "<b>KONFIRMASI HAPUS</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                "Tindakan ini akan menghapus seluruh data pengguna, data VIP, dan riwayat secara permanen. Apakah Anda yakin?"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "admin_db_reset_final":
        # Eksekusi Reset Total Database
        await adb.clear_all_db()
        clear_all_sessions()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
        ])
        await query.edit_message_text(
            text=(
                "<b>PROSES BERHASIL</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                "Database berhasil direset secara total."
            ),
            parse_mode="HTML",
            reply_markup=keyboard
        )

    elif data == "admin_db_reset_cancel":
        try:
            await query.message.delete()
        except Exception:
            pass
        from handlers.start import send_fresh_start_menu
        await send_fresh_start_menu(context.bot, update.effective_user.id, query.message.chat_id, update.effective_user.first_name or "Kawan")