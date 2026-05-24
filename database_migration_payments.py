"""
Migration: Tambah tabel payments untuk track transaksi Pakasir
Run sekali: python database_migration_payments.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "database", "bot.db")

def migrate():
    """Add payments table to existing database"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    
    try:
        # Create payments table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                order_id        TEXT    NOT NULL UNIQUE,
                amount          INTEGER NOT NULL,
                package_days    INTEGER NOT NULL,
                payment_method  TEXT    DEFAULT 'qris',
                payment_number  TEXT    DEFAULT NULL,
                status          TEXT    DEFAULT 'pending',
                expired_at      TEXT    DEFAULT NULL,
                completed_at    TEXT    DEFAULT NULL,
                created_at      TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        
        # Indexes untuk performance
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at)")
        
        conn.commit()
        print("✅ Migration berhasil: tabel payments created")
        
        # Update schema version
        conn.execute("""
            INSERT INTO schema_version (version, description)
            VALUES (2, 'Add payments table for Pakasir integration')
        """)
        conn.commit()
        print("✅ Schema version updated to 2")
        
    except sqlite3.OperationalError as e:
        if "already exists" in str(e).lower():
            print("⚠️  Table payments sudah ada, skip migration")
        else:
            raise
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()