"""
Fix one-time: koreksi amount di tabel payments yang tersimpan salah
(total_payment=5345 harusnya original_price=5000)
"""
import sqlite3
import os

db_path = os.path.join("database", "bot.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Original price per package_days (sesuai PAKET di vip_pakasir.py)
price_map = {7: 5000, 14: 10000, 21: 15000, 30: 20000}

rows = conn.execute(
    "SELECT order_id, package_days, amount FROM payments WHERE status = 'pending'"
).fetchall()

print("=== Cek dan fix payment amounts ===")
fixed = 0
for r in rows:
    correct_amount = price_map.get(r["package_days"], r["amount"])
    if r["amount"] != correct_amount:
        conn.execute(
            "UPDATE payments SET amount = ? WHERE order_id = ?",
            (correct_amount, r["order_id"])
        )
        print(f"  FIXED: {r['order_id']}  {r['amount']} -> {correct_amount}")
        fixed += 1
    else:
        print(f"  OK: {r['order_id']}  amount={r['amount']} (sudah benar)")

conn.commit()
conn.close()
print(f"\nSelesai! {fixed} payment difix.")
