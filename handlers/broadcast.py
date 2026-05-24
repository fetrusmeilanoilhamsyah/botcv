"""
handlers/broadcast.py

FIX RACE CONDITION: handle_broadcast_msg sekarang yield ke event loop
setiap beberapa pesan via asyncio.sleep(), sehingga bot tetap bisa
handle command lain selama broadcast berlangsung.

Dengan 1000 user dan 0.05s sleep per user → broadcast ~50 detik total,
event loop tidak blokir karena await asyncio.sleep() melepas kontrol.
"""
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import RetryAfter
from database import db
from middleware.auth import require_admin

STATE = "BROADCAST_WAIT_MSG"


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id = update.effective_user.id
    db.set_session(user_id, STATE, {})
    await update.message.reply_text("Tulis pesan broadcast:")


async def handle_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess    = db.get_session(user_id)
    if sess["state"] != STATE:
        return

    # Guard: cegah double broadcast jika admin kirim pesan dua kali
    data = dict(sess.get("data", {}))
    if data.get("is_processing"):
        await update.message.reply_text("⏳ Broadcast masih berjalan, harap tunggu...")
        return
    data["is_processing"] = True
    db.set_session(user_id, STATE, data)

    message = update.message.text.strip()
    all_ids = db.get_all_user_ids()
    success = 0
    fail    = 0
    total   = len(all_ids)

    await update.message.reply_text(f"📤 Mengirim ke {total} user...")

    for i, uid in enumerate(all_ids):
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            success += 1
        except RetryAfter as e:
            # Flood limit Telegram — yield dulu, lalu coba kirim ulang
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.send_message(chat_id=uid, text=message)
                success += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1

        # FIX: yield ke event loop setiap pesan agar bot tidak freeze
        # 0.05s = 20 msg/detik, di bawah limit Telegram 30 msg/detik
        await asyncio.sleep(0.05)

        # Progress update tiap 100 user
        if (i + 1) % 100 == 0:
            try:
                await update.message.reply_text(
                    f"⏳ Progress: {i+1}/{total} (berhasil: {success}, gagal: {fail})"
                )
            except Exception:
                pass

    db.log_broadcast(user_id, message, success, fail)
    db.clear_session(user_id)

    await update.message.reply_text(
        f"✅ Broadcast selesai.\n"
        f"Berhasil: {success}\n"
        f"Gagal: {fail}"
    )
