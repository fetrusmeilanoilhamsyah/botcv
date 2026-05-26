"""
handlers/referral.py — Referral points system & VIP Redemption Shop logic.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb

async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_username = context.bot.username or "Bot"
    
    from handlers.start import transition_to_handler
    
    # Ambil poin & total akumulasi referral
    pts = await adb.get_referral_points(user.id)
    count = await adb.get_referral_count(user.id)
    
    link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    
    text = (
        "<b>PROGRAM REFERRAL (SISTEM POIN)</b>\n\n"
        "Undang teman baru dan dapatkan poin koin untuk ditukarkan dengan paket VIP gratis secara instan!\n\n"
        "<b>Aturan Program:</b>\n"
        "• 1 Teman diundang = <b>1 Poin</b>.\n"
        "• Maksimal perolehan seumur hidup = <b>50 Poin</b> (anti-spam).\n\n"
    )
    
    if pts["total_referral_points_earned"] < 50:
        text += (
            "<b>Link Referral Kamu:</b>\n"
            f"<code>{link}</code>\n"
            "<i>(Klik link di atas untuk menyalin)</i>\n\n"
        )
    else:
        text += (
            "<b>Link Referral Kamu:</b>\n"
            "<i>Link referral dinonaktifkan karena telah mencapai batas maksimal 50 Poin.</i>\n\n"
        )
        
    text += (
        "<b>Statistik Kamu:</b>\n"
        f"• Saldo Poin: <b>{pts['referral_points']} Poin</b>\n"
        f"• Total Akumulasi: <b>{pts['total_referral_points_earned']} / 50 Poin</b>\n"
        f"• Total Teman Diundang: <b>{count} orang</b>\n\n"
        "<b>Toko Penukaran VIP:</b>\n"
        "Pilih paket VIP di bawah ini untuk menukarkan poin kamu secara langsung."
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10 POIN — 7 HARI VIP", callback_data="redeem_ref_10", style="primary"),
            InlineKeyboardButton("20 POIN — 14 HARI VIP", callback_data="redeem_ref_20", style="primary")
        ],
        [
            InlineKeyboardButton("30 POIN — 21 HARI VIP", callback_data="redeem_ref_30", style="primary"),
            InlineKeyboardButton("50 POIN — 30 HARI VIP", callback_data="redeem_ref_50", style="primary")
        ],
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
    ])
    
    await transition_to_handler(
        context.bot,
        user.id,
        update.effective_chat.id,
        text,
        reply_markup=keyboard,
        update=update
    )


async def handle_redeem_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    
    try:
        points_to_redeem = int(data.replace("redeem_ref_", ""))
    except ValueError:
        await query.answer("Paket penukaran tidak valid.", show_alert=True)
        return
        
    days_map = {
        10: 7,
        20: 14,
        30: 21,
        50: 30
    }
    
    if points_to_redeem not in days_map:
        await query.answer("Paket penukaran tidak ditemukan.", show_alert=True)
        return
        
    days = days_map[points_to_redeem]
    
    # Kurangi poin
    success = await adb.deduct_referral_points(user.id, points_to_redeem)
    if not success:
        await query.answer("Saldo koin/poin kamu tidak mencukupi untuk penukaran ini.", show_alert=True)
        return
        
    # Tambah masa VIP
    await adb.set_member_vip(user.id, days, "Referral Redemption")
    
    # Alert sukses
    await query.answer(f"Penukaran sukses! {points_to_redeem} Poin ditukarkan dengan {days} Hari VIP gratis.", show_alert=True)
    
    # Refresh halaman referral secara mulus
    await cmd_referral(update, context)
