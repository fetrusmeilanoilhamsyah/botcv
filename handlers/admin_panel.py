"""
handlers/admin_panel.py - Panel Kontrol Administrator (Admin Panel) tanpa emoji.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from middleware.auth import require_admin
from database import db

async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan Panel Kontrol Admin."""
    if not await require_admin(update, context):
        return
    
    user_id = update.effective_user.id
    
    # Hapus command dari obrolan agar chat bersih
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass

    from database.db import is_maintenance_mode
    mt_status = "AKTIF (LOCK)" if is_maintenance_mode() else "NON-AKTIF (OPEN)"
    text = (
        "<b>[ PANEL KONTROL ADMINISTRATOR ]</b>\n"
        f"<blockquote>Silakan pilih tindakan administrasi di bawah ini:\n\n"
        f"• Status Maintenance : <code>{mt_status}</code></blockquote>"
    )
    
    mt_btn_text = "OPEN SISTEM (MATIKAN MT)" if is_maintenance_mode() else "LOCK SISTEM (AKTIFKAN MT)"
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("STATISTIK", callback_data="admin_stat", style="primary"),
            InlineKeyboardButton("DAFTAR USER", callback_data="admin_daftar", style="primary")
        ],
        [
            InlineKeyboardButton("BACKUP DATABASE", callback_data="admin_backup", style="primary"),
            InlineKeyboardButton("RESET DATABASE", callback_data="admin_reset_confirm", style="danger")
        ],
        [
            InlineKeyboardButton("BC TEKS", callback_data="admin_bc_text", style="primary"),
            InlineKeyboardButton("BC MEDIA", callback_data="admin_bc_media", style="primary")
        ],
        [
            InlineKeyboardButton("STOP BROADCAST", callback_data="admin_bc_stop", style="danger"),
            InlineKeyboardButton("KELOLA VIP", callback_data="admin_vip_manage", style="primary")
        ],
        [
            InlineKeyboardButton(mt_btn_text, callback_data="admin_toggle_mt", style="danger" if not is_maintenance_mode() else "primary")
        ],
        [
            InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")
        ]
    ])

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        from handlers.start import transition_to_handler
        await transition_to_handler(
            context.bot,
            user_id,
            update.effective_chat.id,
            text,
            reply_markup=keyboard,
            update=update
        )


async def handle_admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router callback query khusus panel admin."""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Proteksi admin ganda
    from config import ADMIN_IDS
    if user_id not in ADMIN_IDS:
        await query.answer("Akses ditolak.", show_alert=True)
        return

    data = query.data

    if data == "admin_panel_menu":
        await cmd_admin_panel(update, context)

    elif data == "admin_stat":
        await query.answer()
        from handlers.stat import cmd_stat
        await cmd_stat(update, context)

    elif data == "admin_daftar":
        from handlers.daftar import cmd_daftar
        await cmd_daftar(update, context)

    elif data == "admin_backup":
        from handlers.backup import cmd_backup
        await cmd_backup(update, context)

    elif data == "admin_reset_confirm":
        await query.answer()
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("YA, HAPUS SEMUA", callback_data="admin_db_reset_final", style="danger"),
                InlineKeyboardButton("BATAL", callback_data="admin_db_reset_cancel", style="primary")
            ]
        ])
        await query.edit_message_text(
            text=(
                "<b>[ KONFIRMASI RESET ]</b>\n"
                "<blockquote>Tindakan ini akan menghapus riwayat broadcast, semua sesi aktif, dan cache file sementara secara permanen.\n\n"
                "Data pengguna dan status keanggotaan VIP tetap aman dan TIDAK akan dihapus. Apakah Anda yakin?</blockquote>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard
        )

    elif data == "admin_bc_text":
        from handlers.broadcast import cmd_broadcast
        await cmd_broadcast(update, context)

    elif data == "admin_bc_media":
        from handlers.media_broadcast import cmd_media_broadcast
        await cmd_media_broadcast(update, context)

    elif data == "admin_bc_stop":
        from handlers.broadcast import cmd_stop_broadcast
        await cmd_stop_broadcast(update, context)

    elif data == "admin_vip_manage":
        await query.answer()
        text = (
            "<b>[ KELOLA ANGGOTA VIP ]</b>\n"
            "<blockquote>Untuk mengelola status VIP user, gunakan perintah teks berikut langsung di obrolan chat:\n\n"
            "• Tambah VIP:\n"
            "<code>/addvip [USER_ID] [JUMLAH_HARI]</code>\n"
            "<i>Contoh: /addvip 123456789 30</i>\n\n"
            "• Hapus VIP:\n"
            "<code>/delvip [USER_ID]</code>\n"
            "<i>Contoh: /delvip 123456789</i></blockquote>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("KEMBALI", callback_data="admin_panel_menu", style="danger")]
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    elif data == "admin_toggle_mt":
        from database.db import is_maintenance_mode, set_maintenance_mode
        current = is_maintenance_mode()
        set_maintenance_mode(not current)
        new_status = not current
        status_str = "diaktifkan (Sistem di-LOCK)" if new_status else "dimatikan (Sistem di-OPEN)"
        await query.answer(f"Mode Maintenance berhasil {status_str}!", show_alert=True)
        await cmd_admin_panel(update, context)
