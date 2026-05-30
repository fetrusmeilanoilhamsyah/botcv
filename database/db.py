"""
database/db.py - OPTIMIZED VERSION with Connection Pool

Key improvements:
1. Connection pool (10 connections) untuk concurrent operations
2. Timeout 30 seconds
3. Better error handling
4. Database indexes for faster queries
"""
import sqlite3
import os
from datetime import datetime, timedelta
import queue
from contextlib import contextmanager
import threading
import copy
import logging

# Setup logger
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")

# ─── CONNECTION POOL ──────────────────────────────────────────────────────────
_conn_pool = queue.Queue(maxsize=32)
_pool_initialized = False
_pool_lock = threading.Lock()
_pool_put_lock = threading.Lock()  # NOTE: Queue.put sudah thread-safe, lock ini redundant tapi dibiarkan untuk kompatibilitas


def _init_connection():
    """Create a new database connection with optimized settings"""
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,  # 30 seconds timeout (was default 5s)
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")  # Explicitly set busy timeout to 30 seconds
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")   # Use memory for temp tables
    return conn


def init_connection_pool():
    """Initialize connection pool with 32 connections"""
    global _pool_initialized
    
    with _pool_lock:
        if _pool_initialized:
            return
            
        for _ in range(32):
            conn = _init_connection()
            _conn_pool.put(conn)
        
        _pool_initialized = True
        print(f"✅ Database connection pool initialized (32 connections)")


@contextmanager
def get_connection():
    """
    Get connection from pool using context manager.
    Timeout 30s — mencegah event loop blokir selamanya jika pool habis.
    
    Usage:
        with get_connection() as conn:
            conn.execute("SELECT * FROM users")
    """
    try:
        conn = _conn_pool.get(timeout=30)
    except queue.Empty:
        raise RuntimeError("Database connection pool exhausted (timeout 30s). Bot overloaded.")
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        # FIX: Queue.put() sudah thread-safe, tidak perlu lock tambahan
        _conn_pool.put(conn)


# ─── DATABASE INITIALIZATION ──────────────────────────────────────────────────

def init_db():
    """Initialize database tables and indexes"""
    init_connection_pool()
    
    with get_connection() as conn:
        # Create tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY,
                username    TEXT    DEFAULT '',
                full_name   TEXT    DEFAULT '',
                is_member   INTEGER DEFAULT 0,
                joined_at   TEXT    DEFAULT (datetime('now')),
                last_active TEXT    DEFAULT (datetime('now')),
                expired_at  TEXT    DEFAULT NULL,
                referred_by INTEGER DEFAULT NULL,
                usage_count INTEGER DEFAULT 0,
                expiry_notified INTEGER DEFAULT 0
            )
        """)
        
        # Safe migration helper
        def safe_add_column(table: str, col_def: str):
            col_name = col_def.split()[0]
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
                conn.commit()
                print(f"✅ Migrated: added {col_name} column to {table}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    logging.getLogger(__name__).error("Migration of %s failed: %s", col_name, e)
                    raise
            except Exception as e:
                logging.getLogger(__name__).error("Unexpected migration error for %s: %s", col_name, e)
                raise

        # Perform migrations for users table columns individually
        safe_add_column("users", "expired_at TEXT DEFAULT NULL")
        safe_add_column("users", "referred_by INTEGER DEFAULT NULL")
        safe_add_column("users", "usage_count INTEGER DEFAULT 0")
        safe_add_column("users", "expiry_notified INTEGER DEFAULT 0")
        safe_add_column("users", "referral_points INTEGER DEFAULT 0")
        safe_add_column("users", "total_referral_points_earned INTEGER DEFAULT 0")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id      INTEGER,
                message       TEXT,
                success_count INTEGER DEFAULT 0,
                fail_count    INTEGER DEFAULT 0,
                sent_at       TEXT    DEFAULT (datetime('now'))
            )
        """)

        # ── PAKASIR PAYMENTS TABLE ──────────────────────────────────────
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
                qr_chat_id      INTEGER DEFAULT NULL,
                qr_message_id   INTEGER DEFAULT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        # Schema version tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TEXT DEFAULT (datetime('now'))
            )
        """)
        
        # Check current version
        current_version = conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()["v"]
        
        if current_version is None:
            # First time setup
            conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema')"
            )
            print("✅ Database schema version: 1 (initial)")
        else:
            print(f"✅ Database schema version: {current_version}")
        
        conn.commit()
        
        # Create indexes for better performance
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_member ON users(is_member)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_expiry ON users(expired_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referred_by)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_usage ON users(usage_count)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_admin ON broadcast_log(admin_id)")
        # Payment indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at)")

        # Migrasi: tambah kolom qr_chat_id dan qr_message_id jika belum ada
        for col in ['qr_chat_id INTEGER DEFAULT NULL', 'qr_message_id INTEGER DEFAULT NULL']:
            col_name = col.split()[0]
            try:
                conn.execute(f'ALTER TABLE payments ADD COLUMN {col}')
            except Exception:
                pass  # kolom sudah ada

        conn.commit()
        print(f"✅ Database tables and indexes initialized")

    # Auto-register ADMIN_IDS from config as members (INSIDE init_db)
    from config import ADMIN_IDS
    for admin_id in ADMIN_IDS:
        if admin_id > 0:
            set_member(admin_id, "System Admin")
            print(f"👑 Admin {admin_id} auto-registered as member")



# ─── IN-MEMORY SESSION CACHE ──────────────────────────────────────────────────
_session_cache: dict = {}
_all_buffers: dict = {}
_vip_cache: dict = {}
_vip_cache_lock = threading.Lock()
_session_cache_lock = threading.Lock()


# ─── USERS ────────────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str, full_name: str):
    """Insert or update user information"""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (id, username, full_name, last_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username    = excluded.username,
                full_name   = excluded.full_name,
                last_active = excluded.last_active
        """, (user_id, username, full_name, datetime.now().isoformat()))
        conn.commit()

def increment_usage(user_id: int):
    """Increment user activity counter"""
    with get_connection() as conn:
        conn.execute("UPDATE users SET usage_count = usage_count + 1, last_active = ? WHERE id = ?", 
                    (datetime.now().isoformat(), user_id))
        conn.commit()

def set_referrer(user_id: int, referrer_id: int):
    """Set who referred this user (only once)"""
    if user_id == referrer_id: return None
    with get_connection() as conn:
        # Cek apakah user sudah ada sebelumnya untuk mencegah spam referral
        # (Idealnya dicek di level handler, tapi ini pengaman database)
        conn.execute("UPDATE users SET referred_by = ? WHERE id = ? AND referred_by IS NULL", (referrer_id, user_id))
        conn.commit()
    return referrer_id

def get_referral_count(referrer_id: int) -> int:
    """Count how many users were referred by this ID"""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) as count FROM users WHERE referred_by = ?", (referrer_id,)).fetchone()
        return row["count"] if row else 0

def get_referrer(user_id: int):
    """Get the ID of the person who referred this user"""
    with get_connection() as conn:
        row = conn.execute("SELECT referred_by FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["referred_by"] if row else None

def get_referral_points(user_id: int) -> dict:
    """Get spendable referral points and total accumulated points."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT referral_points, total_referral_points_earned FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        return {"referral_points": 0, "total_referral_points_earned": 0}

def add_referral_points(user_id: int, points: int) -> bool:
    """Add referral points up to the limit of 50 total earned points."""
    if points <= 0:
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE users 
               SET referral_points = referral_points + min(?, 50 - total_referral_points_earned), 
                   total_referral_points_earned = total_referral_points_earned + min(?, 50 - total_referral_points_earned) 
               WHERE id = ? AND total_referral_points_earned < 50""", 
            (points, points, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0

def deduct_referral_points(user_id: int, points: int) -> bool:
    """Deduct spendable referral points."""
    if points <= 0:
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE users SET referral_points = referral_points - ? WHERE id = ? AND referral_points >= ?", 
            (points, user_id, points)
        )
        conn.commit()
        return cursor.rowcount > 0

def get_top_users(limit=5):
    """Get top active users for leaderboard"""
    with get_connection() as conn:
        rows = conn.execute("SELECT id, full_name, username, usage_count FROM users ORDER BY usage_count DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_users_for_expiry_notif():
    """Get users who expire in ~24 hours and haven't been notified"""
    # Mencari yang expired_at antara 23 jam sampai 25 jam ke depan
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, expired_at FROM users 
            WHERE is_member = 1 
              AND expiry_notified = 0 
              AND expired_at IS NOT NULL
              AND expired_at BETWEEN datetime('now', '+23 hours') AND datetime('now', '+25 hours')
        """).fetchall()
        return [dict(r) for r in rows]

def mark_expiry_notified(user_id: int):
    """Mark that user has been notified about expiry"""
    with get_connection() as conn:
        conn.execute("UPDATE users SET expiry_notified = 1 WHERE id = ?", (user_id,))
        conn.commit()


def get_user(user_id: int):
    """Get user by ID - FIXED: include expired_at and last_active for VIP check & cleanup"""
    with get_connection() as conn:
        return conn.execute(
            "SELECT id, username, full_name, is_member, expired_at, last_active, usage_count, joined_at FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()


def is_member(user_id: int) -> bool:
    """Check if user is an active member.
    FIX: Gunakan RAM Cache (VIP_CACHE_TTL) dengan Lock untuk performa ekstrem & aman konkurensi.
    """
    import time
    from config import VIP_CACHE_TTL
    
    # Cek cache RAM
    now = time.time()
    with _vip_cache_lock:
        cached = _vip_cache.get(user_id)
        if cached and (now - cached["cached_at"] < VIP_CACHE_TTL):
            return cached["is_member"]

    with get_connection() as conn:
        # Single atomic query: update expired rows sekaligus
        now_iso = datetime.now().isoformat()
        # Revoke VIP yang sudah expired dalam satu query
        conn.execute(
            """UPDATE users
               SET is_member = 0, expired_at = NULL
               WHERE id = ? AND is_member = 1
                 AND expired_at IS NOT NULL
                 AND expired_at < ?""",
            (user_id, now_iso)
        )
        conn.commit()
        # Sekarang baca status terkini
        row = conn.execute(
            "SELECT is_member FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        
        status = False
        if row is not None:
            status = bool(row["is_member"])
            
        with _vip_cache_lock:
            # Simpan ke cache RAM
            _vip_cache[user_id] = {
                "is_member": status,
                "cached_at": now
            }
        return status


def remove_member(user_id: int):
    """Remove user from membership status"""
    with _vip_cache_lock:
        _vip_cache.pop(user_id, None)  # Invalidate RAM cache
    with get_connection() as conn:
        conn.execute("UPDATE users SET is_member = 0, expired_at = NULL WHERE id = ?", (user_id,))
        conn.commit()


def set_member(user_id: int, full_name: str = ""):
    """Set user as permanent member (no expiry — for admins / manual grant)"""
    with _vip_cache_lock:
        _vip_cache.pop(user_id, None)  # Invalidate RAM cache
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (id, username, full_name, is_member, expired_at)
            VALUES (?, '', ?, 1, NULL)
            ON CONFLICT(id) DO UPDATE SET is_member = 1, expired_at = NULL
        """, (user_id, full_name))
        conn.commit()


def set_member_vip(user_id: int, days: int, full_name: str = ""):
    """Set user as VIP member with expiry date. Admins are forced to NULL (permanent)."""
    with _vip_cache_lock:
        _vip_cache.pop(user_id, None)  # Invalidate RAM cache
    from datetime import datetime, timedelta
    from config import ADMIN_IDS
    
    if user_id in ADMIN_IDS:
        set_member(user_id, full_name)
        return None

    expired_at = (datetime.now() + timedelta(days=days)).isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (id, username, full_name, is_member, expired_at)
            VALUES (?, '', ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                is_member  = 1,
                expired_at = CASE
                    WHEN expired_at IS NOT NULL AND expired_at > datetime('now')
                    THEN datetime(expired_at, '+' || ? || ' days')
                    ELSE ?
                END
        """, (user_id, full_name, expired_at, days, expired_at))
        conn.commit()
    return expired_at


def get_vip_expiry(user_id: int):
    """Returns ISO expiry string or None if permanent/not found"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT expired_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row)["expired_at"] if row else None


def expire_vip_members():
    """Batch expire all VIP members whose time has passed. Called on startup/periodic."""
    with get_connection() as conn:
        result = conn.execute("""
            UPDATE users SET is_member = 0, expired_at = NULL
            WHERE is_member = 1
              AND expired_at IS NOT NULL
              AND expired_at < datetime('now')
        """)
        conn.commit()
        return result.rowcount


def get_all_member_ids():
    """Get all member IDs"""
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM users WHERE is_member = 1").fetchall()
        return [r["id"] for r in rows]


def get_all_user_ids():
    """Get all user IDs"""
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM users").fetchall()
        return [r["id"] for r in rows]


def get_all_users_detail():
    """Get all users with full details for /daftar command"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, full_name, is_member, joined_at, expired_at, usage_count FROM users ORDER BY joined_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def clear_all_db():
    """Smart Reset: Clear logs and sessions but preserve users and premium status"""
    with get_connection() as conn:
        # Clear broadcast history
        conn.execute("DELETE FROM broadcast_log")
        conn.commit()
    
    # Clear in-memory buffers
    with _session_cache_lock:
        _session_cache.clear()
        _all_buffers.clear()
    
    # Clear on-disk temporary files via session middleware (if possible)
    try:
        from middleware.session import clear_all_sessions
        clear_all_sessions()
    except ImportError:
        pass
        
    print("⚠️ SMART RESET: Logs and sessions cleared. Users and Premium status preserved.")


# ─── BATCH OPERATIONS (NEW) ───────────────────────────────────────────────────

def batch_update_users(users: list):
    """
    Update multiple users in a single transaction
    
    Args:
        users: List of (user_id, username, full_name) tuples
    """
    with get_connection() as conn:
        for user_id, username, full_name in users:
            conn.execute("""
                INSERT INTO users (id, username, full_name, last_active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    last_active = excluded.last_active
            """, (user_id, username, full_name, datetime.now().isoformat()))
        conn.commit()


# ─── SESSIONS (IN-MEMORY) ─────────────────────────────────────────────────────

def get_session(user_id: int) -> dict:
    """Get user session (in-memory) with deep copy to prevent mutation"""
    with _session_cache_lock:
        cached = _session_cache.get(user_id, {"state": None, "data": {}})
        return {
            "state": cached["state"],
            "data": copy.deepcopy(cached["data"])
        }


def set_session(user_id: int, state: str, data: dict):
    """Set user session (in-memory) with deep copy to prevent mutation.
    FIX MEMORY LEAK: Evict entri terlama jika cache melebihi SESSION_CACHE_MAX_SIZE.
    """
    try:
        from config import SESSION_CACHE_MAX_SIZE
        max_size = SESSION_CACHE_MAX_SIZE
    except ImportError:
        max_size = 2000

    with _session_cache_lock:
        # Evict 10% entri terlama jika cache penuh
        if len(_session_cache) >= max_size and user_id not in _session_cache:
            evict_count = max(1, max_size // 10)
            for old_uid in list(_session_cache.keys())[:evict_count]:
                _session_cache.pop(old_uid, None)
                _all_buffers.pop(old_uid, None)
            logger.warning("[db] Session cache penuh, evict %d entri terlama", evict_count)

        _session_cache[user_id] = {
            "state": state,
            "data": copy.deepcopy(data)
        }


def clear_session(user_id: int):
    """Clear user session"""
    with _session_cache_lock:
        _session_cache.pop(user_id, None)


def clear_user_ram(user_id: int):
    """Clear user RAM data (session and buffers)"""
    with _session_cache_lock:
        _session_cache.pop(user_id, None)
        _all_buffers.pop(user_id, None)


# ─── BROADCAST LOG ────────────────────────────────────────────────────────────

def log_broadcast(admin_id: int, message: str, success: int, fail: int):
    """Log broadcast message"""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO broadcast_log (admin_id, message, success_count, fail_count)
            VALUES (?, ?, ?, ?)
        """, (admin_id, message, success, fail))
        conn.commit()


# ─── HEALTH CHECK (NEW) ───────────────────────────────────────────────────────

def get_db_stats():
    """Get database statistics for monitoring"""
    with get_connection() as conn:
        user_count = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]
        member_count = conn.execute("SELECT COUNT(*) as count FROM users WHERE is_member = 1").fetchone()["count"]
        broadcast_count = conn.execute("SELECT COUNT(*) as count FROM broadcast_log").fetchone()["count"]
        
        with _session_cache_lock:
            session_size = len(_session_cache)
            buffer_size = len(_all_buffers)

        return {
            "total_users": user_count,
            "total_members": member_count,
            "total_broadcasts": broadcast_count,
            "session_cache_size": session_size,
            "buffer_cache_size": buffer_size,
        }


def get_in_memory_stats():
    """Get in-memory statistics without database queries, ensuring complete thread-safety"""
    with _session_cache_lock:
        session_size = len(_session_cache)
        buffer_size = len(_all_buffers)
    return {
        "session_cache_size": session_size,
        "buffer_cache_size": buffer_size,
    }

def cleanup_stale_sessions(max_age_hours=24):
    """
    Clean up session cache for users inactive >24 hours.
    Called from job_cleanup_sessions.
    Returns number of sessions cleaned.
    """
    from datetime import datetime, timedelta
    
    with _session_cache_lock:
        user_ids = list(_session_cache.keys())
    if not user_ids:
        return 0

    cleaned = 0
    stale_users = []
    
    placeholders = ",".join("?" for _ in user_ids)
    query = f"SELECT id, last_active FROM users WHERE id IN ({placeholders})"
    
    try:
        with get_connection() as conn:
            rows = conn.execute(query, user_ids).fetchall()
    except Exception as e:
        logger.error("[db] cleanup_stale_sessions failed to query DB: %s", e)
        return 0

    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    found_user_ids = set()
    
    for row in rows:
        uid = row["id"]
        found_user_ids.add(uid)
        try:
            last_active = datetime.fromisoformat(row["last_active"])
            if last_active < cutoff:
                stale_users.append(uid)
        except Exception:
            pass

    # Hapus juga user yang tidak ditemukan di DB tapi ada di RAM cache
    for uid in user_ids:
        if uid not in found_user_ids:
            stale_users.append(uid)
    
    with _session_cache_lock:
        for user_id in stale_users:
            _session_cache.pop(user_id, None)
            _all_buffers.pop(user_id, None)
            cleaned += 1
    
    return cleaned


# ─── PAYMENT FUNCTIONS ────────────────────────────────────────────────────────

def create_payment(
    user_id: int,
    order_id: str,
    amount: int,
    package_days: int,
    payment_number: str = None,
    expired_at: str = None,
    qr_chat_id: int = None,
    qr_message_id: int = None,
) -> bool:
    """Create new payment record"""
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO payments (
                    user_id, order_id, amount, package_days,
                    payment_method, payment_number, expired_at, status,
                    qr_chat_id, qr_message_id
                )
                VALUES (?, ?, ?, ?, 'qris', ?, ?, 'pending', ?, ?)
            """, (user_id, order_id, amount, package_days, payment_number, expired_at, qr_chat_id, qr_message_id))
            conn.commit()
        logger.info(f"[DB] Payment created: {order_id} for user {user_id}")
        return True
    except sqlite3.IntegrityError as e:
        logger.error(f"[DB] Payment create failed (duplicate?): {e}")
        return False
    except Exception as e:
        logger.error(f"[DB] Payment create exception: {e}")
        return False


def get_payment(order_id: str):
    """Get payment by order_id"""
    try:
        with get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM payments WHERE order_id = ?
            """, (order_id,)).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"[DB] Get payment exception: {e}")
        return None


def get_user_payments(user_id: int, limit: int = 10):
    """Get user's payment history"""
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM payments
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (user_id, limit)).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"[DB] Get user payments exception: {e}")
        return []


def update_payment_status(order_id: str, status: str, completed_at: str = None) -> bool:
    """Update payment status"""
    try:
        with get_connection() as conn:
            if completed_at:
                conn.execute("""
                    UPDATE payments
                    SET status = ?, completed_at = ?
                    WHERE order_id = ?
                """, (status, completed_at, order_id))
            else:
                conn.execute("""
                    UPDATE payments SET status = ? WHERE order_id = ?
                """, (status, order_id))
            conn.commit()
        logger.info(f"[DB] Payment status updated: {order_id} -> {status}")
        return True
    except Exception as e:
        logger.error(f"[DB] Update payment status exception: {e}")
        return False


def expire_old_pending_payments(minutes: int = 20) -> int:
    """
    FIX UX: Tandai payment 'pending' yang sudah lebih dari `minutes` menit
    sebagai 'expired'. QRIS Pakasir berlaku ~15 menit, jadi 20 menit aman.
    Dipanggil dari job scheduler tiap 5 menit.
    Returns: jumlah payment yang di-expire.
    """
    cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    try:
        with get_connection() as conn:
            result = conn.execute(
                """UPDATE payments
                   SET status = 'expired'
                   WHERE status = 'pending' AND created_at < ?""",
                (cutoff,)
            )
            conn.commit()
            count = result.rowcount
            if count > 0:
                logger.info("[DB] expire_old_pending_payments: %d payment di-expire", count)
            return count
    except Exception as exc:
        logger.error("[DB] expire_old_pending_payments error: %s", exc)
        return 0


def get_and_expire_old_pending_payments(minutes: int = 5) -> list:
    """
    ATOMIC: Memperbarui status pembayaran pending yang kedaluwarsa menjadi 'expired'
    dan mengembalikan detail baris yang di-update secara atomic menggunakan RETURNING clause (SQLite 3.35+).
    Mencegah race condition double-notification.
    Returns: list of dict berisi pembayaran yang di-expire oleh query ini saja.
    """
    time_clause = f"-{minutes} minutes"
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """UPDATE payments
                   SET status = 'expired'
                   WHERE status = 'pending' AND created_at < datetime('now', ?)
                   RETURNING id, user_id, order_id, amount, qr_chat_id, qr_message_id""",
                (time_clause,)
            )
            rows = cursor.fetchall()
            conn.commit()
            
            expired_list = [dict(r) for r in rows]
            if expired_list:
                logger.info("[DB] get_and_expire_old_pending_payments: %d payment di-expire", len(expired_list))
            return expired_list
    except Exception as exc:
        logger.error("[DB] get_and_expire_old_pending_payments error: %s", exc)
        return []


def complete_payment_if_pending(order_id: str, completed_at: str = None) -> bool:
    """
    ATOMIC: Set status='completed' HANYA jika status saat ini 'pending'.
    Mencegah race condition double-activation antara webhook dan manual cek.
    Returns True jika berhasil di-update (artinya kita yang pertama proses),
    False jika sudah diproses sebelumnya.
    """
    try:
        ts = completed_at or datetime.now().isoformat()
        with get_connection() as conn:
            result = conn.execute(
                """UPDATE payments
                   SET status = 'completed', completed_at = ?
                   WHERE order_id = ? AND status = 'pending'""",
                (ts, order_id),
            )
            conn.commit()
            updated = result.rowcount > 0
            if updated:
                logger.info("[DB] complete_payment_if_pending: %s marked completed", order_id)
            else:
                logger.info("[DB] complete_payment_if_pending: %s already processed, skip", order_id)
            return updated
    except Exception as exc:
        logger.error("[DB] complete_payment_if_pending error: %s", exc)
        return False


def get_payment_stats():
    """Get payment statistics including income"""
    try:
        with get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) as c FROM payments").fetchone()["c"]
            completed = conn.execute("SELECT COUNT(*) as c FROM payments WHERE status = 'completed'").fetchone()["c"]
            pending = conn.execute("SELECT COUNT(*) as c FROM payments WHERE status = 'pending'").fetchone()["c"]
            income = conn.execute("SELECT SUM(amount) as s FROM payments WHERE status = 'completed'").fetchone()["s"] or 0
            return {"total": total, "completed": completed, "pending": pending, "income": income}
    except Exception as e:
        logger.error(f"[DB] Get payment stats exception: {e}")
        return {"total": 0, "completed": 0, "pending": 0, "income": 0}


# Only initialize automatically when run directly as main script
if __name__ == "__main__":
    init_db()