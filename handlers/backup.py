"""
handlers/backup.py - Command handler untuk backup database secara manual bagi Admin.
"""
import os
import shutil
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from middleware.auth import require_admin
from database.db import DB_PATH

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengirimkan salinan file database.db kepada admin secara privat."""
    if not await require_admin(update, context):
        return

    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer("Mempersiapkan backup database...")
        except Exception:
            pass

    # Kirim status awal
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Sedang mempersiapkan backup database..."
    )

    try:
        # Nama file backup unik dengan timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_database_{timestamp}.db"
        
        # Buat temporary backup path di direktori yang sama dengan bot.db
        temp_dir = os.path.dirname(DB_PATH)
        backup_path = os.path.join(temp_dir, backup_filename)
        
        # Lakukan penyalinan file untuk menghindari file locking secara async
        import asyncio
        await asyncio.to_thread(shutil.copy2, DB_PATH, backup_path)
        
        # Kirim dokumen database ke ID chat admin secara aman
        with open(backup_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=backup_filename,
                caption=(
                    f"<b>[ BACKUP DATABASE ]</b>\n"
                    f"<blockquote>Berhasil membuat backup database.\n"
                    f"Waktu: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} WIB</blockquote>"
                ),
                parse_mode="HTML"
            )
            
        # Hapus file temporary backup secara async
        if os.path.exists(backup_path):
            import asyncio
            await asyncio.to_thread(os.remove, backup_path)
            
        # Hapus status pesan awal
        await status_msg.delete()

    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Gagal melakukan backup database: %s", e)
        await status_msg.edit_text(
            f"Gagal melakukan backup database: {str(e)}"
        )
