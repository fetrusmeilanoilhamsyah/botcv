"""
media_broadcast.py — Admin-only: kirim iklan berupa foto atau video ke semua user.
Mendukung pengiriman album/media group (multiple photos/videos).
"""
from telegram import Update, InputMediaPhoto, InputMediaVideo, InlineKeyboardButton, InlineKeyboardMarkup
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
    user_id = update.effective_user.id
    db.set_session(user_id, STATE, {})
    
    text = (
        "<b>[ BROADCAST MEDIA ]</b>\n"
        "<blockquote>Silakan kirim foto atau video (bisa berupa album/media group) yang ingin Anda broadcast:</blockquote>"
    )
    
    if update.callback_query:
        query = update.callback_query
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("BATAL", callback_data="admin_panel_menu", style="danger")]
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML")

active_broadcasts = {}
media_group_buffers = {}


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
    item = {}
    if update.message.photo:
        item["type"] = "photo"
        item["file_id"] = update.message.photo[-1].file_id
    elif update.message.video:
        item["type"] = "video"
        item["file_id"] = update.message.video.file_id
    elif update.message.animation:
        item["type"] = "animation"
        item["file_id"] = update.message.animation.file_id
    else:
        await update.message.reply_text("Gagal. Kirim foto, video, atau GIF.")
        return

    item["caption"] = update.message.caption or ""

    # Jika ini bagian dari media group (album)
    mg_id = update.message.media_group_id
    if mg_id:
        if mg_id not in media_group_buffers:
            media_group_buffers[mg_id] = {
                "user_id": user_id,
                "items": [],
                "task": None
            }
        
        media_group_buffers[mg_id]["items"].append(item)
        
        # Debounce: batalkan task sebelumnya dan jadwalkan ulang
        if media_group_buffers[mg_id]["task"]:
            media_group_buffers[mg_id]["task"].cancel()
            
        async def wait_and_broadcast(mg_id_to_process):
            try:
                # Beri waktu 1.5 detik untuk mengumpulkan semua media dalam grup
                await asyncio.sleep(1.5)
                items = media_group_buffers[mg_id_to_process]["items"]
                await run_media_group_broadcast(update, context, items)
            except asyncio.CancelledError:
                pass
            finally:
                media_group_buffers.pop(mg_id_to_process, None)
                
        media_group_buffers[mg_id]["task"] = asyncio.create_task(wait_and_broadcast(mg_id))
    else:
        # Kiriman media tunggal
        await run_media_group_broadcast(update, context, [item])


async def run_media_group_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, items: list):
    user_id = update.effective_user.id
    db.clear_session(user_id)
    
    if not items:
        return

    # Filter media list untuk send_media_group (hanya mendukung photo & video)
    # Jika ada tipe animation, kirim sebagai single saja (jika cuma 1 item)
    has_animation = any(x["type"] == "animation" for x in items)
    if has_animation and len(items) > 1:
        # Telegram send_media_group tidak mendukung GIF/animation bersama foto/video,
        # jadi kita filter hanya foto & video jika ada lebih dari 1 item.
        items = [x for x in items if x["type"] in ("photo", "video")]

    if not items:
        await update.message.reply_text("Gagal. Album hanya boleh berisi foto atau video.")
        return

    async def _run_media_broadcast():
        users = await adb.get_all_users_detail()
        total = len(users)
        await update.message.reply_text(f"Memulai broadcast media ke {total} user...")

        from telegram.error import RetryAfter

        async def send_one(uid):
            if len(items) == 1:
                item = items[0]
                if item["type"] == "photo":
                    await context.bot.send_photo(chat_id=uid, photo=item["file_id"], caption=item["caption"])
                elif item["type"] == "video":
                    await context.bot.send_video(chat_id=uid, video=item["file_id"], caption=item["caption"])
                elif item["type"] == "animation":
                    await context.bot.send_animation(chat_id=uid, animation=item["file_id"], caption=item["caption"])
            else:
                media_list = []
                for item in items:
                    if item["type"] == "photo":
                        media_list.append(InputMediaPhoto(media=item["file_id"], caption=item["caption"]))
                    elif item["type"] == "video":
                        media_list.append(InputMediaVideo(media=item["file_id"], caption=item["caption"]))
                await context.bot.send_media_group(chat_id=uid, media=media_list)

        success = 0
        fail = 0
        try:
            for u in users:
                uid = u["id"]
                try:
                    await send_one(uid)
                    success += 1
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        await send_one(uid)
                        success += 1
                    except Exception:
                        fail += 1
                except Exception:
                    fail += 1
                
                await asyncio.sleep(0.05)  # Anti rate-limit
                
            media_desc = f"{len(items)} media" if len(items) > 1 else items[0]["type"]
            caption_sample = items[0]["caption"][:100] if items[0]["caption"] else ""
            await adb.log_broadcast(user_id, f"[MEDIA:{media_desc}] {caption_sample}", success, fail)
            await update.message.reply_text(f"<b>Broadcast selesai.</b>\nBerhasil: <b>{success}</b>\nGagal: <b>{fail}</b>", parse_mode="HTML")
        except asyncio.CancelledError:
            sent_so_far = success + fail
            not_sent = total - sent_so_far
            media_desc = f"{len(items)} media" if len(items) > 1 else items[0]["type"]
            caption_sample = items[0]["caption"][:100] if items[0]["caption"] else ""
            await adb.log_broadcast(user_id, f"[BATAL][MEDIA:{media_desc}] {caption_sample}", success, fail)
            await update.message.reply_text(
                f"🛑 <b>Broadcast media dibatalkan oleh admin.</b>\n"
                f"Berhasil: <b>{success}</b>\n"
                f"Gagal: <b>{fail}</b>\n"
                f"Belum terkirim: <b>{not_sent}</b>",
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
