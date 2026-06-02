"""
vip.py — Tampilkan harga paket VIP dan arahkan user ke admin.
Pembayaran manual (QRIS pending approval Midtrans).
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database.db_async import adb
from datetime import datetime
from config import ADMIN_CONTACT


PAKET = [
    {"label": "2 Hari",    "days": 2,  "harga": "Rp 2.000"},
    {"label": "1 Minggu",  "days": 7,  "harga": "Rp 5.000"},
    {"label": "2 Minggu",  "days": 14, "harga": "Rp 10.000"},
    {"label": "3 Minggu",  "days": 21, "harga": "Rp 15.000"},
    {"label": "1 Bulan",   "days": 30, "harga": "Rp 20.000"},
    {"label": "Permanen",  "days": 365, "harga": "Rp 100.000"},
]


async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    from handlers.start import transition_to_handler
    
    status_line = ""
    if await adb.is_member(user.id):
        expired_at  = await adb.get_vip_expiry(user.id)
        if expired_at:
            exp  = datetime.fromisoformat(expired_at)
            if exp.tzinfo is not None:
                exp = exp.replace(tzinfo=None)
            sisa = max(0, (exp - datetime.now()).days)
            status_line = (
                f"Status: VIP Aktif\n"
                f"Limit : {exp.strftime('%d/%m/%Y')} ({sisa} hari lagi)\n"
            )
        else:
            status_line = "Status: Member Permanen\n"

    lines = []
    for p in PAKET:
        lines.append(f"• {p['label']:<9} : {p['harga']}")

    keyboard = [
        [InlineKeyboardButton("HUBUNGI ADMIN", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}")],
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
    ]

    header_text = (
        "<b>[ VIP MEMBER CV ]</b>\n"
        "────────────────────────────\n"
    )
    if status_line:
        header_text += f"<b>{status_line}</b>────────────────────────────\n\n"

    text = (
        f"{header_text}"
        f"<b>PAKET LAYANAN VIP</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) +
        f"\n━━━━━━━━━━━━━━━━━\n"
        f"<b>Metode:</b> Manual\n\n"
        f"Silakan hubungi {ADMIN_CONTACT} untuk aktivasi paket Anda."
    )

    await transition_to_handler(
        context.bot,
        user.id,
        update.effective_chat.id,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        update=update
    )
