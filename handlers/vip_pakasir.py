"""
handlers/vip_pakasir.py - Handler VIP dengan Pakasir QRIS
"""
import io
import logging
import os
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import db
from database.db_async import adb

logger = logging.getLogger(__name__)

# --- Dependency checks ---
try:
    from core.pakasir import PakasirClient
    _PAKASIR_LIB = True
except ImportError:
    logger.warning("[VIP] core.pakasir tidak tersedia, mode manual aktif")
    _PAKASIR_LIB = False

# FIX: db_payments hanya dipakai sebagai re-export, tapi
# di dalam async handler kita WAJIB pakai adb (async) agar tidak blokir event loop.
# Import berikut hanya dipakai untuk QRIS_ENABLED check.
try:
    from database.db_payments import (
        create_payment, get_payment, get_user_payments, update_payment_status,
        complete_payment_if_pending,
    )
    _PAYMENT_DB = True
except ImportError:
    logger.warning("[VIP] database.db_payments tidak tersedia, mode manual aktif")
    _PAYMENT_DB = False

try:
    import segno
    _SEGNO = True
except ImportError:
    _SEGNO = False

# --- Config ---
PAKASIR_ENABLED  = os.getenv("PAKASIR_ENABLED",  "false").lower() == "true"
PAKASIR_SLUG     = os.getenv("PAKASIR_SLUG",     "")
PAKASIR_API_KEY  = os.getenv("PAKASIR_API_KEY",  "")
PAKASIR_SANDBOX  = os.getenv("PAKASIR_SANDBOX",  "false").lower() == "true"
ADMIN_CONTACT    = os.getenv("ADMIN_CONTACT",    "@admin")

QRIS_ENABLED = (
    PAKASIR_ENABLED and _PAKASIR_LIB and _PAYMENT_DB
    and bool(PAKASIR_SLUG) and bool(PAKASIR_API_KEY)
)

# --- Paket ---
PAKET = [
    {"label": "2 Hari",    "days":  2, "price":  2_000},
    {"label": "1 Minggu",  "days":  7, "price":  5_000},
    {"label": "2 Minggu",  "days": 14, "price": 10_000},
    {"label": "3 Minggu",  "days": 21, "price": 15_000},
    {"label": "1 Bulan",   "days": 30, "price": 20_000},
]

_STATUS_EMOJI = {"completed": "✅", "pending": "⏳", "cancelled": "❌", "expired": "⏰"}


def _fmt_price(n: int) -> str:
    return f"Rp {n:,}".replace(",", ".")


def _pakasir() -> "PakasirClient":
    return PakasirClient(PAKASIR_SLUG, PAKASIR_API_KEY, PAKASIR_SANDBOX)


def _get_package(days: int):
    return next((p for p in PAKET if p["days"] == days), None)


# ──────────────────────────────────────────────────────────────────────────────
# /vip
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await adb.upsert_user(user.id, user.username or "", user.full_name or "")

    from handlers.start import transition_to_handler

    # Status VIP
    status_line = ""
    if await adb.is_member(user.id):
        expired_at = await adb.get_vip_expiry(user.id)
        if expired_at:
            exp = datetime.fromisoformat(expired_at)
            sisa = (exp - datetime.now()).days
            status_line = (
                f"Status: VIP Aktif\n"
                f"Limit : {exp.strftime('%d/%m/%Y')} ({sisa} hari lagi)\n\n"
            )
        else:
            status_line = "Status: Member Permanen\n\n"

    # Paket
    paket_lines = "PAKET VIP\n" + ("─" * 24) + "\n"
    for p in PAKET:
        paket_lines += f"{p['label']:<8} : {_fmt_price(p['price'])}\n"
    paket_lines += "─" * 24 + "\n"

    # Info pembayaran
    if QRIS_ENABLED:
        mode = "SANDBOX" if PAKASIR_SANDBOX else "QRIS Otomatis"
        info = f"Metode: {mode}\nPilih paket di bawah untuk mengaktifkan VIP."
    else:
        info = f"Metode: Manual\nHubungi {ADMIN_CONTACT} untuk aktivasi."

    # Keyboard
    if QRIS_ENABLED:
        rows = []
        for p in PAKET:
            rows.append([
                InlineKeyboardButton(
                    f"{p['label'].upper()} — {_fmt_price(p['price'])}",
                    callback_data=f"buy_vip_{p['days']}",
                    style="primary"
                )
            ])
        rows.append([InlineKeyboardButton("RIWAYAT PEMBAYARAN", callback_data="vip_history", style="success")])
        rows.append([InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")])
    else:
        rows = [
            [InlineKeyboardButton("HUBUNGI ADMIN", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}")],
            [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
        ]

    text = f"{status_line}{paket_lines}{info}"
    await transition_to_handler(
        context.bot,
        user.id,
        update.effective_chat.id,
        text,
        reply_markup=InlineKeyboardMarkup(rows),
        update=update
    )



# ──────────────────────────────────────────────────────────────────────────────
# Callback: buy_vip_{days}
# ──────────────────────────────────────────────────────────────────────────────

async def handle_buy_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    try:
        days = int(query.data.split("_")[-1])
    except ValueError:
        await query.edit_message_text("Paket tidak valid.")
        return

    package = _get_package(days)
    if not package:
        await query.edit_message_text("Paket tidak ditemukan.")
        return

    if not QRIS_ENABLED:
        await query.edit_message_text(
            f"Pembayaran otomatis tidak tersedia.\n"
            f"Hubungi {ADMIN_CONTACT} untuk aktivasi manual."
        )
        return

    # ── Guard: cek apakah user sudah punya payment pending ───────────────────────
    # FIX: gunakan adb.get_user_payments (async) bukan get_user_payments sync
    existing = await adb.get_user_payments(user.id, limit=5)
    pending = next((p for p in existing if p["status"] == "pending"), None)
    if pending:
        pkg_pending = _get_package(pending["package_days"])
        label_pending = pkg_pending["label"] if pkg_pending else f"{pending['package_days']} hari"
        kb_existing = InlineKeyboardMarkup([
            [InlineKeyboardButton("CEK STATUS PEMBAYARAN", callback_data=f"check_payment_{pending['order_id']}", style="success")],
            [InlineKeyboardButton("BATALKAN & BUAT BARU", callback_data=f"cancel_payment_{pending['order_id']}", style="danger")],
        ])
        await query.edit_message_text(
            f"Kamu masih punya pembayaran pending.\n\n"
            f"Paket   : {label_pending}\n"
            f"Total   : {_fmt_price(pending['amount'])}\n"
            f"Order ID: {pending['order_id']}\n\n"
            f"Selesaikan atau batalkan dulu sebelum buat yang baru.",
            reply_markup=kb_existing,
        )
        return

    client = _pakasir()
    order_id = client.generate_order_id(user.id, days)

    # Notif loading
    msg = await query.edit_message_text(
        f"Membuat QRIS untuk {package['label']}..."
    )

    payment = await client.create_transaction(
        order_id=order_id, amount=package["price"], method="qris"
    )

    if not payment:
        await msg.edit_text(
            f"Gagal membuat pembayaran. Coba lagi atau hubungi {ADMIN_CONTACT}."
        )
        return

    original_amount = package["price"]
    total_payment   = payment.get("total_payment", original_amount)

    # FIX: gunakan adb.create_payment (async) bukan create_payment sync
    saved = await adb.create_payment(
        user_id=user.id,
        order_id=order_id,
        amount=original_amount,
        package_days=days,
        payment_number=payment.get("payment_number"),
        expired_at=payment.get("expired_at"),
    )

    if not saved:
        logger.error("[VIP] Gagal simpan ke DB: %s", order_id)
        await client.cancel_transaction(order_id, original_amount)
        await msg.edit_text("Error menyimpan data. Silakan coba lagi.")
        return

    qr_string = payment.get("payment_number", "")

    # Tampilkan waktu expired (5 menit dari sekarang secara lokal WIB)
    try:
        from datetime import timezone, timedelta
        # Konversi ke Asia/Jakarta (WIB, UTC+7)
        jakarta_tz = timezone(timedelta(hours=7))
        exp_dt_jakarta = datetime.now(jakarta_tz) + timedelta(minutes=5)
        exp_text = exp_dt_jakarta.strftime("%d/%m/%Y %H:%M") + " WIB (5 Menit)"
    except Exception:
        exp_text = "5 menit"

    fee = payment.get("fee", 0)
    caption = (
        f"QRIS PEMBAYARAN\n"
        f"{'─'*24}\n"
        f"{'Paket':<5} : {package['label']}\n"
        f"{'Harga':<5} : {_fmt_price(original_amount)}\n"
        f"{'Fee':<5} : {_fmt_price(fee)}\n"
        f"{'Total':<5} : {_fmt_price(total_payment)}\n"
        f"{'Limit':<5} : {exp_text}\n"
        f"{'Order':<5} : {order_id}\n"
        f"{'─'*24}\n"
        f"Scan QR di atas untuk mengaktifkan VIP otomatis."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("CEK STATUS PEMBAYARAN", callback_data=f"check_payment_{order_id}", style="success")],
        [InlineKeyboardButton("BATALKAN PEMBAYARAN", callback_data=f"cancel_payment_{order_id}", style="danger")],
    ])

    qr_chat_id    = None
    qr_message_id = None

    if _SEGNO and qr_string:
        try:
            qr = segno.make(qr_string)
            buf = io.BytesIO()
            qr.save(buf, kind="png", scale=8, border=2)
            buf.seek(0)
            # FIX: kirim foto DULU, delete loading message SETELAH berhasil
            # (urutan lama: delete dulu → kalau send_photo crash → msg sudah hilang, fallback crash)
            sent = await context.bot.send_photo(
                chat_id=user.id, photo=buf, caption=caption, reply_markup=kb
            )
            try:
                await msg.delete()  # hapus loading message setelah foto berhasil terkirim
            except Exception as e:
                logger.debug("[VIP] Gagal hapus loading message: %s", e)
            qr_chat_id    = sent.chat_id
            qr_message_id = sent.message_id
        except Exception as exc:
            logger.warning("[VIP] Gagal render QR image: %s", exc)

    if qr_message_id is None:
        # Fallback: msg loading masih ada, edit jadi teks QRIS
        try:
            sent = await msg.edit_text(
                f"{caption}\n\nQRIS String:\n`{qr_string}`",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception as exc:
            # msg mungkin sudah hilang karena exception saat send_photo, kirim baru
            logger.warning("[VIP] Fallback edit_text gagal, kirim pesan baru: %s", exc)
            sent = await context.bot.send_message(
                chat_id=user.id,
                text=f"{caption}\n\nQRIS String:\n`{qr_string}`",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        qr_chat_id    = sent.chat_id
        qr_message_id = sent.message_id

    # Simpan message_id QR ke DB agar bisa dihapus otomatis setelah bayar
    # FIX: jalankan di thread executor agar tidak blokir event loop
    try:
        import asyncio
        from database.db import get_connection

        def _save_qr_ids():
            with get_connection() as conn:
                conn.execute(
                    "UPDATE payments SET qr_chat_id=?, qr_message_id=? WHERE order_id=?",
                    (qr_chat_id, qr_message_id, order_id)
                )
                conn.commit()

        await asyncio.get_event_loop().run_in_executor(None, _save_qr_ids)
    except Exception as exc:
        logger.warning("[VIP] Gagal simpan qr_message_id: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Callback: check_payment_{order_id}
# ──────────────────────────────────────────────────────────────────────────────

async def handle_check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Mengecek status...")

    order_id = query.data.removeprefix("check_payment_")
    chat_id  = query.message.chat_id

    async def reply(text: str):
        """Kirim pesan baru dengan tombol KEMBALI KE MENU"""
        back_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
        ])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=back_markup)

    try:
        # FIX: gunakan adb.get_payment (async) bukan get_payment sync
        payment = await adb.get_payment(order_id)
        if not payment:
            await reply("Pembayaran tidak ditemukan.")
            return

        if payment["status"] == "expired":
            await reply(
                f"Pembayaran ini sudah kedaluwarsa (batas waktu 5 menit habis).\n"
                f"Silakan buat QRIS baru via /vip."
            )
            return

        if payment["status"] == "completed":
            await reply(
                f"Pembayaran sudah selesai.\n\n"
                f"VIP aktif: {payment['package_days']} hari\n"
                f"Order ID : {order_id}"
            )
            return

        txn = await _pakasir().get_transaction_status(
            order_id=order_id, amount=payment["amount"]
        )

        if txn and txn.get("status") == "completed":
            # ATOMIC: hanya aktivasi VIP jika kita yang pertama update ke 'completed'.
            # Mencegah double-activation jika webhook dan manual cek terjadi bersamaan.
            # FIX: gunakan adb.complete_payment_if_pending (async)
            was_updated = await adb.complete_payment_if_pending(order_id, datetime.now().isoformat())

            if was_updated:
                await adb.set_member_vip(user_id=payment["user_id"], days=payment["package_days"])
                # Hapus pesan QR lama agar tidak menumpuk
                await _delete_qr_message(context.bot, payment)
                await reply(
                    f"Pembayaran berhasil!\n\n"
                    f"Paket VIP {payment['package_days']} hari sudah aktif.\n"
                    f"Nikmati semua fitur premium sekarang."
                )
            else:
                # Sudah diproses oleh webhook atau cek manual sebelumnya
                await reply(
                    f"Pembayaran sudah dikonfirmasi.\n\n"
                    f"VIP aktif: {payment['package_days']} hari\n"
                    f"Order ID : {order_id}"
                )
        else:
            await reply(
                f"Pembayaran belum diterima.\n\n"
                f"Scan QRIS lagi atau tunggu beberapa saat.\n"
                f"Order ID: {order_id}"
            )

    except Exception as exc:
        logger.error("[VIP] check_payment error: %s", exc, exc_info=True)
        await reply("Gagal mengecek status. Coba lagi.")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: cancel_payment_{order_id}
# ──────────────────────────────────────────────────────────────────────────────

async def handle_cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Membatalkan...")

    order_id = query.data.removeprefix("cancel_payment_")
    chat_id  = query.message.chat_id

    async def reply(text: str):
        """Kirim pesan baru dengan tombol KEMBALI KE MENU"""
        back_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
        ])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=back_markup)

    try:
        # FIX: gunakan adb.get_payment (async) bukan get_payment sync
        payment = await adb.get_payment(order_id)
        if not payment:
            await reply("Pembayaran tidak ditemukan.")
            return

        if payment["status"] == "expired":
            await reply("Pembayaran ini sudah kedaluwarsa. Kamu bisa membuat QRIS baru via /vip.")
            return

        if payment["status"] != "pending":
            await reply(f"Pembayaran sudah berstatus '{payment['status']}', tidak bisa dibatalkan.")
            return

        ok = await _pakasir().cancel_transaction(order_id, payment["amount"])
        if ok:
            # FIX: gunakan adb.update_payment_status (async)
            await adb.update_payment_status(order_id, "cancelled")
            # Hapus pesan QR lama — tombol di QR sudah tidak valid setelah cancel
            await _delete_qr_message(context.bot, payment)
            try:
                await query.message.delete()
            except Exception:
                pass
            await reply(f"Pembayaran dibatalkan.\nOrder ID: {order_id}\n\nKamu bisa buat QRIS baru kapan saja via /vip.")
        else:
            await reply(f"Gagal membatalkan. Hubungi {ADMIN_CONTACT} jika diperlukan.")

    except Exception as exc:
        logger.error("[VIP] cancel_payment error: %s", exc, exc_info=True)
        await reply("Terjadi error. Silakan coba lagi.")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: vip_history
# ──────────────────────────────────────────────────────────────────────────────

async def handle_vip_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user    = query.from_user
    chat_id = query.message.chat_id

    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("KEMBALI KE MENU", callback_data="back_to_start", style="danger")]
    ])

    try:
        # FIX: gunakan adb.get_user_payments (async) bukan get_user_payments sync
        payments = await adb.get_user_payments(user.id, limit=10)
        if not payments:
            await query.edit_message_text(
                "Belum ada riwayat pembayaran.\nBeli paket VIP untuk mulai.",
                reply_markup=back_markup
            )
            return

        lines = ["RIWAYAT PEMBAYARAN\n"]
        for p in payments:
            emoji = _STATUS_EMOJI.get(p["status"], "?")
            created = datetime.fromisoformat(p["created_at"]).strftime("%d/%m/%Y %H:%M")
            lines.append(
                f"{emoji} {p['package_days']} hari — {_fmt_price(p['amount'])}\n"
                f"   {created}\n"
                f"   {p['order_id']}\n"
            )

        await query.edit_message_text(
            "\n".join(lines)[:4000],
            reply_markup=back_markup
        )

    except Exception as exc:
        logger.error("[VIP] vip_history error: %s", exc, exc_info=True)
        try:
            await query.edit_message_text(
                "Gagal memuat riwayat. Coba lagi.",
                reply_markup=back_markup
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Gagal memuat riwayat. Coba lagi.",
                reply_markup=back_markup
            )


# ──────────────────────────────────────────────────────────────────────────────
# Helper: hapus pesan QR lama setelah pembayaran selesai
# ──────────────────────────────────────────────────────────────────────────────

async def _delete_qr_message(bot, payment: dict):
    """
    Hapus pesan QR lama dari chat user.
    Dipanggil setelah pembayaran confirmed — baik via webhook maupun cek manual.
    """
    chat_id    = payment.get("qr_chat_id")
    message_id = payment.get("qr_message_id")
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug("[VIP] Gagal hapus QR message: %s", exc)
