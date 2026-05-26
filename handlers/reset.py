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
    current_msg_id = update.message.message_id

    # 1. Jalankan pembersihan sesi RAM & disk di background
    async def cleanup_bg():
        cancel_all(user_id)
        db.clear_session(user_id)
        clear_user_dir(user_id)
    
    asyncio.create_task(cleanup_bg())

    # 2. Hapus seluruh riwayat chat (100 pesan terakhir) secara paralel untuk membersihkan layar secara total
    async def delete_msg(msg_id):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    # Hapus 100 pesan ke belakang dari pesan /reset saat ini
    await asyncio.gather(*(delete_msg(mid) for mid in range(current_msg_id, current_msg_id - 100, -1)))

    # 3. Kirim menu utama yang fresh dan bersih sebagai satu-satunya pesan tersisa
    from handlers.start import send_fresh_start_menu
    first_name = update.effective_user.first_name or "Kawan"
    await send_fresh_start_menu(context.bot, user_id, chat_id, first_name)

    # 4. Tampilkan menu tambahan Admin secara bersih jika user adalah Admin
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("⚠️ RESET TOTAL DATABASE", callback_data="admin_db_reset_confirm")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>ADMIN MENU</b>\n"
                "Reset total database."
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
                InlineKeyboardButton("✅ YA, HAPUS SEMUA", callback_data="admin_db_reset_final"),
                InlineKeyboardButton("❌ BATAL", callback_data="admin_db_reset_cancel")
            ]
        ]
        await query.edit_message_text(
            "KONFIRMASI\n"
            "Hapus semua data permanent?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "admin_db_reset_final":
        # Eksekusi Reset Total Database
        db.clear_all_db()
        clear_all_sessions()
        await query.edit_message_text(
            "Database berhasil direset."
        )

    elif data == "admin_db_reset_cancel":
        await query.edit_message_text("Reset dibatalkan.")