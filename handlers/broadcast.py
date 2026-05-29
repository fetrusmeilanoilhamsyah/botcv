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
from database.db_async import adb
from middleware.auth import require_admin

STATE = "BROADCAST_WAIT_MSG"


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id = update.effective_user.id
    db.set_session(user_id, STATE, {})
    await update.message.reply_text("Tulis pesan broadcast:")


active_broadcasts = {}


async def handle_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sess    = db.get_session(user_id)
    if sess["state"] != STATE:
        return

    # Guard: cegah double broadcast jika admin kirim pesan dua kali
    active_bc = context.bot_data.setdefault("active_broadcasts", {})
    if user_id in active_bc:
        await update.message.reply_text("<b>Broadcast masih berjalan, harap tunggu atau gunakan /stopbroadcast...</b>", parse_mode="HTML")
        return

    message = update.message.text.strip()
    db.clear_session(user_id)

    async def _run_broadcast():
        all_ids = await adb.get_all_user_ids()
        success = 0
        fail    = 0
        total   = len(all_ids)

        status_msg = await update.message.reply_text(f"Mengirim ke {total} user...")

        try:
            for i, uid in enumerate(all_ids):
                try:
                    await context.bot.send_message(chat_id=uid, text=message)
                    success += 1
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        await context.bot.send_message(chat_id=uid, text=message)
                        success += 1
                    except Exception:
                        fail += 1
                except Exception:
                    fail += 1

                # yield ke event loop setiap pesan agar bot tidak freeze
                await asyncio.sleep(0.05)

                # Progress update tiap 100 user
                if (i + 1) % 100 == 0:
                    try:
                        await update.message.reply_text(
                            f"Progress: {i+1}/{total} (berhasil: <b>{success}</b>, gagal: <b>{fail}</b>)",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            await adb.log_broadcast(user_id, message, success, fail)
            await update.message.reply_text(
                f"<b>Broadcast selesai.</b>\n"
                f"Berhasil: <b>{success}</b>\n"
                f"Gagal: <b>{fail}</b>",
                parse_mode="HTML"
            )
        except asyncio.CancelledError:
            await adb.log_broadcast(user_id, f"[BATAL] {message}", success, fail)
            await update.message.reply_text(
                f"🛑 <b>Broadcast dibatalkan oleh admin.</b>\n"
                f"Berhasil terkirim: <b>{success}</b>\n"
                f"Gagal/Belum terkirim: <b>{total - success}</b>",
                parse_mode="HTML"
            )
            raise
        finally:
            active_bc = context.bot_data.setdefault("active_broadcasts", {})
            active_bc.pop(user_id, None)

    async def _run_with_timeout():
        try:
            await asyncio.wait_for(_run_broadcast(), timeout=600)
        except asyncio.TimeoutError:
            await update.message.reply_text("⏰ <b>Broadcast otomatis dihentikan karena mencapai batas waktu 10 menit.</b>", parse_mode="HTML")
            active_bc = context.bot_data.setdefault("active_broadcasts", {})
            active_bc.pop(user_id, None)

    task = asyncio.create_task(_run_with_timeout())
    context.bot_data.setdefault("active_broadcasts", {})[user_id] = task


async def cmd_stop_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hentikan paksa broadcast yang sedang berjalan"""
    if not await require_admin(update, context):
        return
    user_id = update.effective_user.id

    cancelled = False

    # Hentikan broadcast teks dari context
    active_bc = context.bot_data.setdefault("active_broadcasts", {})
    task1 = active_bc.pop(user_id, None)
    if task1 and not task1.done():
        task1.cancel()
        cancelled = True

    # Hentikan broadcast media dari context
    active_mbc = context.bot_data.setdefault("active_media_broadcasts", {})
    task2 = active_mbc.pop(user_id, None)
    if task2 and not task2.done():
        task2.cancel()
        cancelled = True

    db.clear_session(user_id)
    if cancelled:
        await update.message.reply_text("🛑 <b>Broadcast sedang aktif berhasil dihentikan paksa oleh admin.</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("Tidak ada broadcast aktif yang sedang berjalan.")

