"""
handlers/redeem.py - Sistem Kupon / Redeem Code untuk aktivasi paket VIP.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_admin

REDEEM_STATE = "REDEEM_WAIT_CODE"


async def handle_redeem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan halaman input kode promo setelah menekan tombol di menu VIP."""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Set status sesi menunggu kode promo
    db.set_session(user_id, REDEEM_STATE, {})
    
    await query.answer()
    
    text = (
        "<b>[ TUKAR KODE PROMO ]</b>\n\n"
        "<blockquote>Silakan ketik dan kirimkan kode promo Anda langsung di obrolan chat ini.</blockquote>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("BATAL", callback_data="show_vip_menu", style="danger")]
    ])
    
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_redeem_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memproses kode promo yang diketikkan oleh user."""
    user_id = update.effective_user.id
    
    # Hapus pesan teks kode promo milik user agar chat bersih
    try:
        await update.message.delete()
    except Exception:
        pass
        
    code = (update.message.text or "").strip().upper()
    if not code:
        return
        
    # Panggil fungsi DB untuk redeem kode secara aman & atomic
    res = await adb.redeem_promo_code(user_id, code)
    
    # Reset sesi kembali bersih setelah memproses
    db.clear_session(user_id)
    
    if res.startswith("success:"):
        parts = res.split(":")
        days = parts[1]
        expiry = parts[2]
        
        # Konversi format tanggal UTC ke tampilan Indonesia yang ramah
        try:
            from datetime import datetime
            dt = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
            bulan_indo = [
                "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                "Juli", "Agustus", "September", "Oktober", "November", "Desember"
            ]
            expiry_display = f"{dt.day} {bulan_indo[dt.month - 1]} {dt.year}"
        except Exception:
            expiry_display = expiry
            
        text = (
            "<b>[ PENUKARAN BERHASIL ]</b>\n\n"
            f"<blockquote>Selamat! Kode promo <code>{code}</code> berhasil ditukarkan.\n\n"
            f"• Akses VIP ditambahkan : <b>{days} Hari</b>\n"
            f"• Masa aktif VIP s.d : <b>{expiry_display}</b></blockquote>"
        )
    elif res == "already_claimed":
        text = (
            "<b>[ PENUKARAN GAGAL ]</b>\n\n"
            f"<blockquote>Kamu sudah pernah mengklaim kode promo <code>{code}</code> sebelumnya.</blockquote>"
        )
    elif res == "limit_reached":
        text = (
            "<b>[ PENUKARAN GAGAL ]</b>\n\n"
            f"<blockquote>Batas penggunaan kuota kode promo <code>{code}</code> sudah habis.</blockquote>"
        )
    elif res == "invalid":
        text = (
            "<b>[ PENUKARAN GAGAL ]</b>\n\n"
            f"<blockquote>Kode promo <code>{code}</code> tidak valid atau sudah kedaluwarsa.</blockquote>"
        )
    else:
        text = (
            "<b>[ PENUKARAN GAGAL ]</b>\n\n"
            "<blockquote>Terjadi kesalahan tak terduga saat memproses kode promo. Silakan coba lagi nanti.</blockquote>"
        )
        
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="primary")]
    ])
    
    # Coba cari welcome message aktif untuk diedit agar chat tetap bersih
    from handlers.start import _welcome_messages, register_welcome_messages
    msg_ids = _welcome_messages.get(user_id, [])
    edited = False
    if msg_ids:
        for mid in msg_ids:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=mid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                edited = True
                break
            except Exception:
                pass
                
    if not edited:
        new_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        register_welcome_messages(user_id, [new_msg.message_id])


# ── ADMIN COMMANDS ───────────────────────────────────────────────────────────

async def cmd_addpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin membuat kode promo baru."""
    if not await require_admin(update, context):
        return
        
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Format salah.\n"
            "Gunakan: <code>/addpromo [KODE] [HARI] [BATAS_PAKAI]</code>\n"
            "Contoh: <code>/addpromo MIL200USER 2 50</code>",
            parse_mode="HTML"
        )
        return
        
    code = args[0].upper().strip()
    try:
        days = int(args[1])
        max_uses = int(args[2]) if len(args) > 2 else 0
    except ValueError:
        await update.message.reply_text("Parameter hari dan batas pakai harus berupa angka.")
        return
        
    success = await adb.add_promo_code(code, days, max_uses)
    if success:
        limit_str = f"kuota {max_uses} user" if max_uses > 0 else "unlimited"
        await update.message.reply_text(
            f"<b>[ KODE PROMO DIBUAT ]</b>\n\n"
            f"• Kode Promo : <code>{code}</code>\n"
            f"• Durasi VIP : <b>{days} Hari</b>\n"
            f"• Batas Kuota : <b>{limit_str}</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("Gagal membuat kode promo.")


async def cmd_delpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin menghapus kode promo."""
    if not await require_admin(update, context):
        return
        
    args = context.args
    if not args:
        await update.message.reply_text("Gunakan: <code>/delpromo [KODE]</code>", parse_mode="HTML")
        return
        
    code = args[0].upper().strip()
    success = await adb.delete_promo_code(code)
    if success:
        await update.message.reply_text(f"Kode promo <code>{code}</code> berhasil dihapus.", parse_mode="HTML")
    else:
        await update.message.reply_text("Gagal menghapus kode promo.")


async def cmd_listpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin melihat daftar kode promo yang aktif beserta statistiknya."""
    if not await require_admin(update, context):
        return
        
    promos = await adb.get_active_promos()
    if not promos:
        await update.message.reply_text("Tidak ada kode promo aktif saat ini.")
        return
        
    text = "<b>[ DAFTAR KODE PROMO AKTIF ]</b>\n\n"
    for p in promos:
        limit_str = f"{p['uses_count']}/{p['max_uses']}" if p['max_uses'] > 0 else f"{p['uses_count']}/unlimited"
        text += (
            f"• Kode : <code>{p['code']}</code>\n"
            f"  Durasi : <b>{p['package_days']} Hari</b> | Terpakai : <b>{limit_str}</b>\n\n"
        )
    await update.message.reply_text(text, parse_mode="HTML")
