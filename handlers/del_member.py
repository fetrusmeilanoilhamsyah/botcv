from telegram import Update
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_admin

STATE = "DELMEMBER_WAIT_ID"

async def cmd_delmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id = update.effective_user.id
    db.set_session(user_id, STATE, {})
    await update.message.reply_text("ID target:")

async def handle_delmember_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return
        
    text = update.message.text.strip()
    if not text.lstrip('-').isdigit() or text == '-':
        await update.message.reply_text("ID harus angka.")
        return
        
    target_id = int(text)
    
    # Check if user exists
    user = await adb.get_user(target_id)
    if not user:
        await update.message.reply_text(f"User {target_id} tidak ditemukan.")
        db.clear_session(user_id)
        return

    try:
        await adb.remove_member(target_id)
        db.clear_session(user_id)
        await update.message.reply_text(f"✅ Akses {target_id} berhasil dicabut.")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal mencabut akses {target_id}: {e}")
