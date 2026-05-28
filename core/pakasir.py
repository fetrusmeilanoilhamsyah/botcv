"""
core/pakasir.py - Pakasir Payment Gateway Client

FIX SECURITY: validate_webhook sekarang verifikasi HMAC-SHA256 signature
dari header X-Pakasir-Signature (jika Pakasir mendukung), PLUS validasi
order_id + amount seperti sebelumnya.

Jika Pakasir belum support HMAC header, set PAKASIR_WEBHOOK_SECRET kosong
di .env — validasi akan fallback ke order_id+amount saja sambil log warning.
"""
import hashlib
import hmac
import httpx
import logging
import os
import random
import string
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

BASE_URL = "https://app.pakasir.com/api"
TIMEOUT  = 30.0


class PakasirClient:
    def __init__(self, project_slug: str, api_key: str, sandbox: bool = False):
        self.project_slug = project_slug
        self.api_key      = api_key
        self.sandbox      = sandbox

    def _base_payload(self) -> Dict[str, Any]:
        return {"project": self.project_slug, "api_key": self.api_key}

    async def create_transaction(
        self, order_id: str, amount: int, method: str = "qris"
    ) -> Optional[Dict[str, Any]]:
        url     = f"{BASE_URL}/transactioncreate/{method}"
        payload = {**self._base_payload(), "order_id": order_id, "amount": amount}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                logger.info("[Pakasir] Transaction created: %s", order_id)
                return data.get("payment")
            logger.error("[Pakasir] Create failed %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("[Pakasir] create_transaction error: %s", exc)
        return None

    async def get_transaction_status(
        self, order_id: str, amount: int
    ) -> Optional[Dict[str, Any]]:
        url    = f"{BASE_URL}/transactiondetail"
        params = {**self._base_payload(), "order_id": order_id, "amount": amount}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json().get("transaction")
            logger.error("[Pakasir] Status check failed %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("[Pakasir] get_transaction_status error: %s", exc)
        return None

    async def cancel_transaction(self, order_id: str, amount: int) -> bool:
        url     = f"{BASE_URL}/transactioncancel"
        payload = {**self._base_payload(), "order_id": order_id, "amount": amount}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("[Pakasir] Cancelled: %s", order_id)
                return True
            logger.error("[Pakasir] Cancel failed %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("[Pakasir] cancel_transaction error: %s", exc)
        return False

    async def simulate_payment(self, order_id: str, amount: int) -> bool:
        if not self.sandbox:
            logger.warning("[Pakasir] simulate_payment only in sandbox mode")
            return False
        url     = f"{BASE_URL}/paymentsimulation"
        payload = {**self._base_payload(), "order_id": order_id, "amount": amount}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception as exc:
            logger.error("[Pakasir] simulate_payment error: %s", exc)
        return False

    @staticmethod
    def validate_webhook(
        payload: Dict[str, Any],
        expected_order_id: str,
        expected_amount: int,
        signature_header: Optional[str] = None,
        raw_body: Optional[bytes] = None,
    ) -> bool:
        """
        Validasi webhook dari Pakasir.

        Layer 1 (HMAC) — dijalankan jika PAKASIR_WEBHOOK_SECRET di-set DAN
        Pakasir mengirim header X-Pakasir-Signature:
            expected = HMAC-SHA256(secret, raw_body).hexdigest()
            harus cocok dengan signature_header

        Layer 2 (order_id + amount) — selalu dijalankan.

        NOTE: Status check (completed/pending/etc) TIDAK dilakukan di sini.
        Handler webhook yang bertanggung jawab atas logika per-status.
        Jika secret belum di-set, Layer 1 dilewati + warning di log.
        """
        try:
            # ── Layer 1: HMAC signature ──────────────────────────────────────
            webhook_secret = os.getenv("PAKASIR_WEBHOOK_SECRET", "").strip()
            if webhook_secret:
                if signature_header and raw_body:
                    # FIX: gunakan hmac.new() dengan keyword arg digestmod agar eksplisit
                    expected_sig = hmac.new(
                        webhook_secret.encode(),
                        raw_body,
                        hashlib.sha256
                    ).hexdigest()
                    if not hmac.compare_digest(expected_sig, signature_header.lower()):
                        logger.error("[Pakasir] HMAC signature mismatch: %s", expected_order_id)
                        return False
                    logger.debug("[Pakasir] HMAC OK: %s", expected_order_id)
                else:
                    # Secret ada tapi Pakasir tidak kirim header — tolak
                    logger.error(
                        "[Pakasir] PAKASIR_WEBHOOK_SECRET diset tapi header "
                        "X-Pakasir-Signature tidak ada. Tolak request."
                    )
                    return False
            else:
                logger.warning(
                    "[Pakasir] PAKASIR_WEBHOOK_SECRET tidak diset! "
                    "Webhook hanya divalidasi via order_id+amount. "
                    "Set secret di .env untuk keamanan penuh."
                )

            # ── Layer 2: field wajib + order_id + amount ─────────────────────
            recv_order  = payload.get("order_id")
            recv_amount = payload.get("amount")
            recv_status = payload.get("status")

            if not all([recv_order, recv_amount, recv_status]):
                logger.warning("[Pakasir] Webhook: field tidak lengkap (order_id/amount/status)")
                return False
            if recv_order != expected_order_id:
                logger.warning("[Pakasir] Webhook: order_id mismatch (%s vs %s)", recv_order, expected_order_id)
                return False
            if int(recv_amount) != int(expected_amount):
                logger.warning("[Pakasir] Webhook: amount mismatch (%s vs %s)", recv_amount, expected_amount)
                return False

            # Status apapun (completed/expired/cancelled) dianggap valid —
            # handler yang akan memutuskan tindakan berdasarkan status.
            return True

        except Exception as exc:
            logger.error("[Pakasir] validate_webhook error: %s", exc)
            return False

    def generate_order_id(self, user_id: int, package_days: int) -> str:
        date_str = datetime.now().strftime("%Y%m%d")
        suffix   = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        return f"VIP{date_str}-{user_id}-{suffix}"
