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

    # Kirim status awal
    status_msg = await update.effective_message.reply_text(
        "Sedang mempersiapkan backup database..."
    )

    try:
        # Nama file backup unik dengan timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_database_{timestamp}.db"
        
        # Buat temporary backup path di direktori yang sama dengan bot.db
        temp_dir = os.path.dirname(DB_PATH)
        backup_path = os.path.join(temp_dir, backup_filename)
        
        # Lakukan penyalinan file untuk menghindari file locking
        shutil.copy2(DB_PATH, backup_path)
        
        # Kirim dokumen database ke ID chat admin
        with open(backup_path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=backup_filename,
                caption=f"Backup database berhasil dibuat.\nTanggal: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            )
            
        # Hapus file temporary backup
        if os.path.exists(backup_path):
            os.remove(backup_path)
            
        # Hapus status pesan awal
        await status_msg.delete()

    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Gagal melakukan backup database: %s", e)
        await status_msg.edit_text(
            f"Gagal melakukan backup database: {str(e)}"
        )
