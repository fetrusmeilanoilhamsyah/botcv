"""
daftar.py — Tampilkan daftar semua pengguna bot.
Hanya bisa diakses oleh admin.
"""
import io
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_admin

async def cmd_daftar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    users = await adb.get_all_users_detail()

    if not users:
        await update.message.reply_text("Belum ada pengguna.")
        return

    total       = len(users)
    total_member = sum(1 for u in users if u["is_member"])
    total_non   = total - total_member

    # Dapatkan waktu local WIB (UTC+7)
    try:
        jakarta_tz = timezone(timedelta(hours=7))
        now_dt = datetime.now(jakarta_tz)
    except Exception:
        now_dt = datetime.now()
    now_str = now_dt.strftime("%d/%m/%Y %H:%M")

    # Format isi file .txt
    lines = [
        "========================================",
        "             DAFTAR PENGGUNA            ",
        "========================================",
        f"Tanggal    : {now_str} WIB",
        f"Total User : {total}",
        f"VIP Member : {total_member}",
        f"Regular    : {total_non}",
        "========================================\n"
    ]

    for idx, u in enumerate(users, 1):
        status   = "VIP Member" if u["is_member"] else "Regular"
        username = f"@{u['username']}" if u["username"] else "-"
        name     = u["full_name"] or "-"
        uid      = u["id"]
        
        usage    = u.get("usage_count", 0)
        
        lines.append(f"{idx:02d}. Nama     : {name}")
        lines.append(f"    Username : {username}")
        lines.append(f"    ID       : {uid}")
        lines.append(f"    Status   : {status}")
        lines.append(f"    Penggunaan: {usage}x")
        lines.append("") # Baris kosong pemisah

    file_content = "\n".join(lines)
    
    # Bungkus ke memory byte stream
    file_bytes = io.BytesIO(file_content.encode('utf-8'))
    file_bytes.name = f"daftar_pengguna_{now_dt.strftime('%Y%m%d_%H%M%S')}.txt"

    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer("Mengirim berkas daftar pengguna...")
        except Exception:
            pass

    # Kirim dokumen (.txt) ke admin secara aman
    await context.bot.send_document(
        chat_id=chat_id,
        document=file_bytes,
        filename=file_bytes.name,
        caption=(
            f"<b>[ DAFTAR PENGGUNA BOT ]</b>\n"
            f"<blockquote>• Total : {total} USER\n"
            f"• VIP   : {total_member}\n"
            f"• Reg   : {total_non}</blockquote>"
        ),
        parse_mode="HTML"
    )
