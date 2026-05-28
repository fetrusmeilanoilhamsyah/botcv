"""
webhook_pakasir.py - Webhook handler Pakasir Payment Gateway

FIX SECURITY: Sekarang baca raw body dulu untuk verifikasi HMAC signature
sebelum proses apapun. Header: X-Pakasir-Signature

Setup:
1. Set PAKASIR_WEBHOOK_SECRET di .env (wajib untuk produksi)
2. Set WEBHOOK_PORT di .env (default: 8080)
3. Buka port di firewall VPS: ufw allow 8080
4. Set URL di dashboard Pakasir: http://IP_VPS:8080/webhook/pakasir
   atau gunakan domain + reverse proxy nginx
"""
import asyncio
import json
import logging
import time
import os
from collections import defaultdict
from datetime import datetime
from functools import partial
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

try:
    from core.pakasir import PakasirClient
    from database import db
    from database.db_payments import get_payment, update_payment_status, complete_payment_if_pending
    _DEPS_OK = True
except ImportError:
    logger.error("[Webhook] Dependency tidak ditemukan")
    _DEPS_OK = False

# Rate limiting: {ip: [timestamp, ...]}
_rate_cache: dict = defaultdict(list)
MAX_REQ_PER_MIN = 10


def _rate_ok(ip: str) -> bool:
    now    = time.time()
    window = [t for t in _rate_cache[ip] if now - t < 60]
    _rate_cache[ip] = window
    if len(window) >= MAX_REQ_PER_MIN:
        return False
    _rate_cache[ip].append(now)
    return True


async def handle_pakasir_webhook(request: web.Request) -> web.Response:
    """
    POST /webhook/pakasir

    FIX: Baca raw body terlebih dahulu untuk HMAC verification.
    Payload Pakasir:
    {
        "order_id": "VIP20260522-123456-A7B3",
        "amount": 20000,
        "status": "completed",
        "payment_method": "qris",
        "project": "your-slug",
        "completed_at": "2026-05-22T08:07:02.819+07:00"
    }
    """
    try:
        ip = request.remote or "unknown"

        if not _rate_ok(ip):
            logger.warning("[Webhook] Rate limit: %s", ip)
            return web.Response(status=429, text="Too Many Requests")

        # FIX: Baca raw body dulu untuk HMAC signature verification
        try:
            raw_body = await request.read()
        except Exception:
            return web.Response(status=400, text="Cannot read body")

        # Ambil HMAC signature dari header
        signature_header = request.headers.get("X-Pakasir-Signature", "").strip() or None

        # Parse JSON dari raw body (bukan request.json() agar raw body tetap tersedia)
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, Exception):
            return web.Response(status=400, text="Invalid JSON")

        order_id     = payload.get("order_id")
        amount       = payload.get("amount")
        status       = payload.get("status")
        completed_at = payload.get("completed_at")

        logger.info("[Webhook] Received: order=%s status=%s ip=%s", order_id, status, ip)

        if not all([order_id, amount, status]):
            return web.Response(status=400, text="Missing required fields")

        # FIX: Semua DB call dijalankan via run_in_executor agar tidak blokir aiohttp event loop.
        loop = asyncio.get_running_loop()

        # Ambil data dari DB dulu (untuk validasi)
        payment = await loop.run_in_executor(None, get_payment, order_id)
        if not payment:
            logger.warning("[Webhook] Order tidak ditemukan: %s", order_id)
            return web.Response(status=200, text="Order not found")

        # FIX SECURITY: Validasi HMAC + order_id + amount
        if not PakasirClient.validate_webhook(
            payload,
            expected_order_id=payment["order_id"],
            expected_amount=payment["amount"],
            signature_header=signature_header,
            raw_body=raw_body,
        ):
            logger.error(
                "[Webhook] Validasi gagal untuk order=%s. SignatureHeader=%s SecretDiset=%s Payload=%s",
                order_id, signature_header, bool(os.getenv("PAKASIR_WEBHOOK_SECRET", "").strip()), payload
            )
            return web.Response(status=400, text="Validation failed")

        # Idempoten — sudah diproses sebelumnya
        if payment["status"] == "completed":
            return web.Response(status=200, text="Already processed")

        # Proses berdasarkan status
        if status == "completed":
            # ATOMIC: hanya proses jika status masih 'pending'.
            # Mencegah race condition double-activation antara webhook dan manual cek.
            # FIX: jalankan via executor agar tidak blokir event loop
            was_updated = await loop.run_in_executor(
                None,
                partial(
                    complete_payment_if_pending,
                    order_id=order_id,
                    completed_at=completed_at or datetime.now().isoformat(),
                )
            )

            if not was_updated:
                # Race condition: sudah diproses oleh thread lain (manual cek / webhook duplikat)
                logger.info("[Webhook] Double-process terdeteksi untuk %s, skip aktivasi VIP", order_id)
                return web.Response(status=200, text="Already processed")

            # FIX: set_member_vip juga via executor
            await loop.run_in_executor(
                None,
                partial(db.set_member_vip, user_id=payment["user_id"], days=payment["package_days"])
            )

            logger.info(
                "[Webhook] VIP aktif: user=%s days=%s order=%s",
                payment["user_id"], payment["package_days"], order_id
            )

            bot = request.app.get("bot")
            main_loop = request.app.get("main_loop")
            if bot:
                # Hapus pesan QR lama
                qr_chat_id    = payment.get("qr_chat_id")
                qr_message_id = payment.get("qr_message_id")
                if qr_chat_id and qr_message_id:
                    try:
                        if main_loop and main_loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                bot.delete_message(chat_id=qr_chat_id, message_id=qr_message_id),
                                main_loop
                            )
                        else:
                            await bot.delete_message(chat_id=qr_chat_id, message_id=qr_message_id)
                    except Exception as exc:
                        logger.debug("[Webhook] Gagal hapus QR message: %s", exc)

                # Kirim notifikasi sukses ke user
                try:
                    text_msg = (
                        f"Pembayaran berhasil!\n\n"
                        f"Paket VIP {payment['package_days']} hari sudah aktif.\n"
                        f"Nikmati semua fitur premium sekarang."
                    )
                    if main_loop and main_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(chat_id=payment["user_id"], text=text_msg),
                            main_loop
                        )
                    else:
                        await bot.send_message(chat_id=payment["user_id"], text=text_msg)
                except Exception as exc:
                    logger.warning("[Webhook] Gagal kirim notif user: %s", exc)

        else:
            # Status lain: expired, cancelled, dsb — update saja di DB tanpa aktivasi VIP.
            # FIX: update_payment_status via executor
            await loop.run_in_executor(None, update_payment_status, order_id, status)
            logger.info("[Webhook] Status diperbarui: %s -> %s", order_id, status)

        return web.Response(status=200, text="OK")

    except Exception as e:
        logger.exception("[Webhook] Terjadi error tidak terduga di handler webhook: %s", e)
        return web.Response(status=500, text="Internal Server Error")


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "pakasir-webhook"})


def create_webhook_app(bot=None, main_loop=None) -> web.Application:
    app = web.Application()
    if bot:
        app["bot"] = bot
    if main_loop:
        app["main_loop"] = main_loop
    app.router.add_post("/webhook/pakasir", handle_pakasir_webhook)
    app.router.add_get("/health", handle_health)
    return app


async def run_webhook_server(port: int = 8080, bot=None, main_loop=None):
    if not _DEPS_OK:
        logger.error("[Webhook] Tidak bisa start: dependency hilang")
        return

    app    = create_webhook_app(bot, main_loop)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("[Webhook] Server berjalan di port %s", port)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await runner.cleanup()


def start_webhook_server_thread(port: int = 8080, bot=None):
    """Jalankan webhook di thread terpisah (kompatibel dengan bot polling)."""
    import threading

    # Capture the main event loop
    try:
        main_loop = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        main_loop = None

    def _run():
        asyncio.run(run_webhook_server(port, bot, main_loop))

    t = threading.Thread(target=_run, daemon=True, name="webhook-pakasir")
    t.start()
    logger.info("[Webhook] Thread dimulai di port %s", port)
    return t


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.getenv("WEBHOOK_PORT", 8080))
    
    # Explicitly initialize SQLite database and connection pool
    from database.db import init_db
    init_db()
    
    asyncio.run(run_webhook_server(port))
