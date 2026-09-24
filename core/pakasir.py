"""
core/pakasir.py - Pakasir Payment Gateway Client (API v2)

Migrasi dari v1 ke v2:
- Endpoint baru: /api/v2/...
- Auth pindah dari body ke header X-Api-Key
- txn_id sebagai kunci utama untuk status check & cancel
- validate_webhook: X-Pakasir-Signature (HMAC) → X-Secret (plain compare)

Deadline v1 deprecated: 20 Oktober 2026.
"""
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

    def _headers(self) -> Dict[str, str]:
        """Auth header untuk semua request v2."""
        return {
            "X-Api-Key":    self.api_key,
            "Content-Type": "application/json",
        }

    async def create_transaction(
        self, order_id: str, amount: int, method: str = "qris"
    ) -> Optional[Dict[str, Any]]:
        """
        API v2: POST /api/v2/create-transaction/{slug}/{order_id}
        Body: {"method": "qris", "amount": 20000}
        Response langsung (tidak wrapped dalam "payment"):
            {txn_id, qr_string, amount, fee, total_payment, status, expired_at, ...}
        """
        url     = f"{BASE_URL}/v2/create-transaction/{self.project_slug}/{order_id}"
        payload = {"method": method, "amount": amount}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                logger.info("[Pakasir] Transaction created: %s | txn_id=%s", order_id, data.get("txn_id"))
                return data  # v2 tidak wrapped, langsung return
            logger.error("[Pakasir] Create failed %s: %s", resp.status_code, resp.text[:300])
        except Exception as exc:
            logger.error("[Pakasir] create_transaction error: %s", exc)
        return None

    async def get_transaction_status(
        self, txn_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        API v2: GET /api/v2/transaction-status/{slug}/{txn_id}
        Response: {txn_id, order_id, amount, status, completed_at, is_sandbox}
        """
        url = f"{BASE_URL}/v2/transaction-status/{self.project_slug}/{txn_id}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                return resp.json()  # v2 tidak wrapped
            logger.error("[Pakasir] Status check failed %s: %s", resp.status_code, resp.text[:300])
        except Exception as exc:
            logger.error("[Pakasir] get_transaction_status error: %s", exc)
        return None

    async def cancel_transaction(self, txn_id: str) -> bool:
        """
        API v2: POST /api/v2/cancel-transaction/{slug}/{txn_id}
        Tidak perlu body. Response: {"message": "Berhasil batalkan transaksi"}
        """
        url = f"{BASE_URL}/v2/cancel-transaction/{self.project_slug}/{txn_id}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers())
            if resp.status_code == 200:
                logger.info("[Pakasir] Cancelled txn_id: %s", txn_id)
                return True
            logger.error("[Pakasir] Cancel failed %s: %s", resp.status_code, resp.text[:300])
        except Exception as exc:
            logger.error("[Pakasir] cancel_transaction error: %s", exc)
        return False

    async def simulate_payment(self, order_id: str, amount: int) -> bool:
        """Hanya untuk sandbox mode."""
        if not self.sandbox:
            logger.warning("[Pakasir] simulate_payment only in sandbox mode")
            return False
        url     = f"{BASE_URL}/paymentsimulation"
        payload = {"project": self.project_slug, "api_key": self.api_key, "order_id": order_id, "amount": amount}
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
        secret_header: Optional[str] = None,
        raw_body: Optional[bytes] = None,  # kept for backwards compat, not used in v2
    ) -> bool:
        """
        Validasi webhook dari Pakasir v2.

        v2 menggunakan header X-Secret (plain string, bukan HMAC).
        Set PAKASIR_WEBHOOK_SECRET di .env — nilai diambil dari dashboard Pakasir
        di halaman detail proyek bagian bawah.

        Layer 1: Bandingkan X-Secret header dengan PAKASIR_WEBHOOK_SECRET env.
        Layer 2: Pastikan order_id dan status ada di payload.
        """
        try:
            webhook_secret = os.getenv("PAKASIR_WEBHOOK_SECRET", "").strip()

            # ── Layer 1: X-Secret plain compare ──────────────────────────────
            if webhook_secret:
                if secret_header:
                    if not hmac.compare_digest(webhook_secret, secret_header.strip()):
                        logger.error("[Pakasir] X-Secret mismatch untuk order: %s", expected_order_id)
                        return False
                    logger.debug("[Pakasir] X-Secret OK: %s", expected_order_id)
                else:
                    logger.error(
                        "[Pakasir] PAKASIR_WEBHOOK_SECRET diset tapi header X-Secret tidak ada. Tolak."
                    )
                    return False
            else:
                logger.warning(
                    "[Pakasir] PAKASIR_WEBHOOK_SECRET tidak diset! "
                    "Webhook hanya divalidasi via order_id+status. "
                    "Set secret di .env untuk keamanan penuh."
                )

            # ── Layer 2: field wajib ──────────────────────────────────────────
            recv_order  = payload.get("order_id")
            recv_status = payload.get("status")

            if not all([recv_order, recv_status]):
                logger.warning("[Pakasir] Webhook: field order_id/status tidak lengkap")
                return False
            if recv_order != expected_order_id:
                logger.warning(
                    "[Pakasir] Webhook: order_id mismatch (%s vs %s)",
                    recv_order, expected_order_id
                )
                return False

            return True

        except Exception as exc:
            logger.error("[Pakasir] validate_webhook error: %s", exc)
            return False

    def generate_order_id(self, user_id: int, package_days: int) -> str:
        date_str = datetime.now().strftime("%Y%m%d")
        suffix   = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        return f"VIP{date_str}-{user_id}-{suffix}"
