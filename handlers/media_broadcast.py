"""
media_broadcast.py — Admin-only: kirim iklan berupa foto atau video ke semua user.
"""
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from database.db_async import adb
from middleware.auth import require_admin
from config import ADMIN_IDS
import asyncio

STATE = "WAIT_BROADCAST_MEDIA"

async def cmd_media_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    db.set_session(update.effective_user.id, STATE, {})
    await update.message.reply_text("Kirim Foto atau Video yang akan di-broadcast:")

active_broadcasts = {}


async def handle_broadcast_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess or sess.get("state") != STATE:
        return

    # Guard: cegah double broadcast
    active_mbc = context.bot_data.setdefault("active_media_broadcasts", {})
    if user_id in active_mbc:
        await update.message.reply_text("<b>Broadcast masih berjalan, harap tunggu atau gunakan /stopbroadcast...</b>", parse_mode="HTML")
        return

    # Deteksi jenis media
    media = None
    media_type = None
    
    if update.message.photo:
        media = update.message.photo[-1].file_id
        media_type = "photo"
    elif update.message.video:
        media = update.message.video.file_id
        media_type = "video"
    elif update.message.animation:
        media = update.message.animation.file_id
        media_type = "animation"
    else:
        await update.message.reply_text("Gagal. Kirim foto, video, atau GIF.")
        return

    caption = update.message.caption or ""
    db.clear_session(user_id)

    async def _run_media_broadcast():
        # Ambil semua user
        users = await adb.get_all_users_detail()
        total = len(users)
        await update.message.reply_text(f"Memulai broadcast media ke {total} user...")

        from telegram.error import RetryAfter

        async def send_one(uid):
            if media_type == "photo":
                await context.bot.send_photo(chat_id=uid, photo=media, caption=caption)
            elif media_type == "video":
                await context.bot.send_video(chat_id=uid, video=media, caption=caption)
            elif media_type == "animation":
                await context.bot.send_animation(chat_id=uid, animation=media, caption=caption)

        success = 0
        fail = 0
        try:
            for u in users:
                uid = u["id"]
                try:
                    await send_one(uid)
                    success += 1
                except RetryAfter as e:
                    # Telegram flood limit — tunggu lalu kirim ulang sekali
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        await send_one(uid)
                        success += 1
                    except Exception:
                        fail += 1
                except Exception:
                    fail += 1
                
                await asyncio.sleep(0.05)  # Anti rate-limit
                
            await adb.log_broadcast(user_id, f"[MEDIA:{media_type}] {caption[:100]}", success, fail)
            await update.message.reply_text(f"<b>Broadcast selesai.</b>\nBerhasil: <b>{success}</b>\nGagal: <b>{fail}</b>", parse_mode="HTML")
        except asyncio.CancelledError:
            await update.message.reply_text(
                f"🛑 <b>Broadcast media dibatalkan oleh admin.</b>\n"
                f"Berhasil terkirim: <b>{success}</b>\n"
                f"Gagal/Belum terkirim: <b>{total - success}</b>",
                parse_mode="HTML"
            )
            raise
        finally:
            active_mbc = context.bot_data.setdefault("active_media_broadcasts", {})
            active_mbc.pop(user_id, None)

    async def _run_with_timeout():
        try:
            await asyncio.wait_for(_run_media_broadcast(), timeout=600)
        except asyncio.TimeoutError:
            await update.message.reply_text("⏰ <b>Broadcast media otomatis dihentikan karena mencapai batas waktu 10 menit.</b>", parse_mode="HTML")
            active_mbc = context.bot_data.setdefault("active_media_broadcasts", {})
            active_mbc.pop(user_id, None)

    task = asyncio.create_task(_run_with_timeout())
    context.bot_data.setdefault("active_media_broadcasts", {})[user_id] = task

