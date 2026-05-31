"""
handlers/akun.py - Info akun: role, VIP expiry, referral progress
"""
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database.db_async import adb
from middleware.auth import is_admin

logger = logging.getLogger(__name__)


async def cmd_akun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    from handlers.start import transition_to_handler

    # Semua query paralel — tidak serial
    import asyncio
    row, member, expired_at, ref_points = await asyncio.gather(
        adb.get_user(user.id),
        adb.is_member(user.id),
        adb.get_vip_expiry(user.id),
        adb.get_referral_points(user.id),
    )

    admin = is_admin(user.id)

    # Role
    if admin:
        role_str = "Admin (Permanen)"
    elif member:
        if expired_at:
            try:
                # Tambahkan .replace(tzinfo=None) agar aman dari TypeError perbandingan naive/aware datetime
                exp = datetime.fromisoformat(expired_at).replace(tzinfo=None)
                sisa = (exp - datetime.now()).days
                tgl  = exp.strftime("%d/%m/%Y")
                role_str = f"VIP — berakhir {tgl} ({sisa} hari lagi)"
            except Exception:
                role_str = "VIP"
        else:
            role_str = "Member Permanen"
    else:
        role_str = "Belum berlangganan"

    # Usage & joined
    usage  = 0
    joined = "-"
    if row:
        r = dict(row)
        usage = r.get("usage_count", 0)
        try:
            joined = datetime.fromisoformat(r.get("joined_at") or r.get("last_active", "")).strftime("%d/%m/%Y")
        except Exception:
            pass

    # Referral points system (konsisten dengan referral.py)
    lines = [
        "<b>INFORMASI AKUN</b>",
        "━━━━━━━━━━━━━━━━━",
        f"• Nama     : {user.full_name or user.first_name or '-'}",
        f"• Username : @{user.username}" if user.username else "• Username : -",
        f"• ID User  : {user.id}",
        f"• Bergabung: {joined}",
        "━━━━━━━━━━━━━━━━━",
        "<b>STATUS & PEMAKAIAN</b>",
        f"• Status   : {role_str}",
        f"• Pemakaian: {usage}x konversi",
        "━━━━━━━━━━━━━━━━━",
    ]

    rows = []
    if not member:
        rows.append([InlineKeyboardButton("LANGGANAN VIP", callback_data="show_vip_menu", style="success")])
    rows.append([InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")])

    await transition_to_handler(
        context.bot,
        user.id,
        update.effective_chat.id,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        update=update
    )
