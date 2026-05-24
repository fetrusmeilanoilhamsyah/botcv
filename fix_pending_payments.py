"""
fix_pending_payments.py
Cek semua payment pending ke Pakasir API, aktifkan VIP kalau sudah completed.
Jalankan sekali: python fix_pending_payments.py
"""
import asyncio
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


async def fix_completed_payments():
    from core.pakasir import PakasirClient
    from database.db import set_member_vip
    from database.db_payments import update_payment_status, get_payment

    slug = os.getenv("PAKASIR_SLUG")
    key = os.getenv("PAKASIR_API_KEY")
    sandbox = os.getenv("PAKASIR_SANDBOX", "false").lower() == "true"
    client = PakasirClient(slug, key, sandbox)

    db_path = os.path.join("database", "bot.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT order_id, user_id, amount, package_days FROM payments WHERE status = 'pending'"
    ).fetchall()
    conn.close()

    print(f"Ada {len(rows)} payment pending. Mengecek satu per satu ke Pakasir...\n")

    for r in rows:
        order_id = r["order_id"]
        user_id = r["user_id"]
        amount = r["amount"]
        days = r["package_days"]

        print(f"Cek: {order_id}  user={user_id}  amount={amount}")
        status_data = await client.get_transaction_status(order_id, amount)

        if not status_data:
            print(f"  -> GAGAL cek (mungkin expired atau tidak ditemukan di Pakasir)\n")
            continue

        tx_status = status_data.get("status")
        print(f"  -> Status Pakasir: {tx_status}")

        if tx_status == "completed":
            set_member_vip(user_id=user_id, days=days)
            completed_at = status_data.get("completed_at") or datetime.now().isoformat()
            update_payment_status(order_id, "completed", completed_at)
            print(f"  -> VIP {days} hari AKTIF untuk user {user_id}! DB diupdate.\n")
        else:
            print(f"  -> Belum dibayar, skip.\n")

    print("Selesai!")


if __name__ == "__main__":
    asyncio.run(fix_completed_payments())
