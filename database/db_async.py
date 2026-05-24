"""
database/db_async.py
Thin async wrapper — semua DB call dijalankan di thread pool executor
agar tidak blokir asyncio event loop.

Cara pakai di handler:
    from database.db_async import adb
    member = await adb.is_member(user_id)
    await adb.upsert_user(user_id, username, full_name)
"""
import asyncio
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from database import db

# Thread pool khusus DB — 8 worker cukup untuk 1000 user concurrent
# karena SQLite sendiri serialized per koneksi
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-worker")


def _run(func, *args, **kwargs):
    """Jalankan fungsi DB sinkron di thread pool, return coroutine."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_executor, partial(func, *args, **kwargs))


class AsyncDB:
    """Async wrapper untuk semua fungsi db.py"""

    # ── Users ──────────────────────────────────────────────────────────────────
    async def get_user(self, user_id: int):
        return await _run(db.get_user, user_id)

    async def upsert_user(self, user_id: int, username: str, full_name: str):
        return await _run(db.upsert_user, user_id, username, full_name)

    async def increment_usage(self, user_id: int):
        return await _run(db.increment_usage, user_id)

    async def is_member(self, user_id: int) -> bool:
        return await _run(db.is_member, user_id)

    async def get_vip_expiry(self, user_id: int):
        return await _run(db.get_vip_expiry, user_id)

    async def set_member(self, user_id: int, full_name: str = ""):
        return await _run(db.set_member, user_id, full_name)

    async def set_member_vip(self, user_id: int, days: int, full_name: str = ""):
        return await _run(db.set_member_vip, user_id, days, full_name)

    async def remove_member(self, user_id: int):
        return await _run(db.remove_member, user_id)

    async def expire_vip_members(self) -> int:
        return await _run(db.expire_vip_members)

    async def get_all_member_ids(self):
        return await _run(db.get_all_member_ids)

    async def get_all_user_ids(self):
        return await _run(db.get_all_user_ids)

    async def get_all_users_detail(self):
        return await _run(db.get_all_users_detail)

    async def get_top_users(self, limit: int = 5):
        return await _run(db.get_top_users, limit)

    async def get_users_for_expiry_notif(self):
        return await _run(db.get_users_for_expiry_notif)

    async def mark_expiry_notified(self, user_id: int):
        return await _run(db.mark_expiry_notified, user_id)

    async def set_referrer(self, user_id: int, referrer_id: int):
        return await _run(db.set_referrer, user_id, referrer_id)

    async def get_referral_count(self, referrer_id: int) -> int:
        return await _run(db.get_referral_count, referrer_id)

    async def get_referrer(self, user_id: int):
        return await _run(db.get_referrer, user_id)

    # ── Sessions (in-memory — tidak perlu executor, sudah non-blocking) ────────
    def get_session(self, user_id: int) -> dict:
        return db.get_session(user_id)

    def set_session(self, user_id: int, state: str, data: dict):
        return db.set_session(user_id, state, data)

    def clear_session(self, user_id: int):
        return db.clear_session(user_id)

    def clear_user_ram(self, user_id: int):
        return db.clear_user_ram(user_id)

    # ── Payments ───────────────────────────────────────────────────────────────
    async def create_payment(self, **kwargs) -> bool:
        return await _run(db.create_payment, **kwargs)

    async def get_payment(self, order_id: str):
        return await _run(db.get_payment, order_id)

    async def get_user_payments(self, user_id: int, limit: int = 10):
        return await _run(db.get_user_payments, user_id, limit)

    async def update_payment_status(self, order_id: str, status: str, completed_at: str = None):
        return await _run(db.update_payment_status, order_id, status, completed_at)

    async def get_payment_stats(self):
        return await _run(db.get_payment_stats)

    async def expire_old_pending_payments(self, minutes: int = 20) -> int:
        """FIX: Method ini sebelumnya HILANG dari AsyncDB — job di main.py selalu error."""
        return await _run(db.expire_old_pending_payments, minutes)

    async def get_and_expire_old_pending_payments(self, minutes: int = 5) -> list:
        """Mendapatkan dan meng-expire pembayaran pending secara asinkron."""
        return await _run(db.get_and_expire_old_pending_payments, minutes)

    async def complete_payment_if_pending(self, order_id: str, completed_at: str = None) -> bool:
        """Atomic: set completed HANYA jika masih pending. Cegah double-activation."""
        return await _run(db.complete_payment_if_pending, order_id, completed_at)

    # ── Stats & cleanup ────────────────────────────────────────────────────────
    async def get_db_stats(self):
        return await _run(db.get_db_stats)

    async def cleanup_stale_sessions(self, max_age_hours: int = 24) -> int:
        return await _run(db.cleanup_stale_sessions, max_age_hours)

    async def clear_all_db(self):
        return await _run(db.clear_all_db)

    async def batch_update_users(self, users: list):
        return await _run(db.batch_update_users, users)

    async def log_broadcast(self, admin_id: int, message: str, success: int, fail: int):
        return await _run(db.log_broadcast, admin_id, message, success, fail)


# Singleton — import ini di mana saja
adb = AsyncDB()
