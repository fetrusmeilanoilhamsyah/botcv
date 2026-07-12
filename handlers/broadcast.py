"""
handlers/broadcast.py

FIX RACE CONDITION: handle_broadcast_msg sekarang yield ke event loop
setiap beberapa pesan via asyncio.sleep(), sehingga bot tetap bisa
handle command lain selama broadcast berlangsung.

Dengan 1000 user dan 0.05s sleep per user → broadcast ~50 detik total,
event loop tidak blokir karena await asyncio.sleep() melepas kontrol.
"""
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    
    text = (
        "<b>[ BROADCAST TEKS ]</b>\n"
        "<blockquote>Silakan ketik pesan teks broadcast yang ingin Anda kirim ke seluruh pengguna:</blockquote>"
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

    # Hapus pesan input teks broadcast dari admin secara instan
    try:
        await update.message.delete()
    except Exception:
        pass

    from handlers.start import _welcome_messages, register_welcome_messages
    welcome_ids = _welcome_messages.get(user_id, [])
    welcome_msg_id = welcome_ids[0] if welcome_ids else None

    async def _run_broadcast():
        all_ids = await adb.get_all_user_ids()
        success = 0
        fail    = 0
        total   = len(all_ids)

        nonlocal welcome_msg_id
        if welcome_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=welcome_msg_id,
                    text=f"<blockquote><b>[ STATUS: BROADCAST TEKS ]</b>\nMengirim ke {total} user...</blockquote>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            try:
                status_msg = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"<blockquote><b>[ STATUS: BROADCAST TEKS ]</b>\nMengirim ke {total} user...</blockquote>",
                    parse_mode="HTML"
                )
                welcome_msg_id = status_msg.message_id
                register_welcome_messages(user_id, [welcome_msg_id])
            except Exception:
                pass

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

                # Progress update tiap 100 user (edit in-place)
                if (i + 1) % 100 == 0:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=update.effective_chat.id,
                            message_id=welcome_msg_id,
                            text=(
                                f"<blockquote><b>[ STATUS: BROADCAST TEKS ]</b>\n"
                                f"Progress: <code>{i+1}/{total}</code>\n"
                                f"• Berhasil : <code>{success}</code>\n"
                                f"• Gagal : <code>{fail}</code></blockquote>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            await adb.log_broadcast(user_id, message, success, fail)
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("KEMBALI KE PANEL", callback_data="admin_panel_menu", style="danger")]
            ])
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=welcome_msg_id,
                text=(
                    f"<b>[ BROADCAST TEKS SELESAI ]</b>\n"
                    f"<blockquote>• Berhasil : {success}\n"
                    f"• Gagal : {fail}</blockquote>"
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except asyncio.CancelledError:
            sent_so_far = success + fail
            not_sent = total - sent_so_far
            await adb.log_broadcast(user_id, f"[BATAL] {message}", success, fail)
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("KEMBALI KE PANEL", callback_data="admin_panel_menu", style="danger")]
            ])
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=welcome_msg_id,
                text=(
                    f"<b>[ BROADCAST TEKS DIBATALKAN ]</b>\n"
                    f"<blockquote>• Berhasil : {success}\n"
                    f"• Gagal : {fail}\n"
                    f"• Belum terkirim : {not_sent}</blockquote>"
                ),
                parse_mode="HTML",
                reply_markup=keyboard
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
    text = "<b>[ STATUS: BROADCAST BERHASIL DIHENTIKAN ]</b>" if cancelled else "Tidak ada broadcast aktif yang sedang berjalan."

    if update.callback_query:
        query = update.callback_query
        await query.answer(text, show_alert=True)
    else:
        await update.message.reply_text(text, parse_mode="HTML")

