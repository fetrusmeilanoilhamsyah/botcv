import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN     = os.getenv("BOT_TOKEN")
ADMIN_IDS     = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@admin")
GROUP_LINK    = os.getenv("GROUP_LINK", "https://t.me/grup")
HARGA_MEMBER  = os.getenv("HARGA_MEMBER", "Hubungi admin")
TUTORIAL_LINK = os.getenv("TUTORIAL_LINK", "https://t.me/tutorialnotceve")

TMP_DIR = os.path.join(os.path.dirname(__file__), "tmp", "sessions")

# ── File handling limits ──────────────────────────────────────────────────────
# FIX: Limit disk per-session agar VPS tidak habis disk
# Sebelumnya: MAX_FILES_PER_SESSION=40096, MAX_UPLOAD_SIZE_MB=500 → bisa 20TB
MAX_FILES_PER_SESSION   = 100       # maks 100 file per sesi
MAX_UPLOAD_SIZE_MB      = 50        # maks 50MB per file upload
MAX_CONTACTS_PER_FILE   = 10000
MAX_DISK_PER_SESSION_MB = 200       # NEW: maks total 200MB per user session
GLOBAL_MAX_DISK_MB      = 10_000   # NEW: maks total tmp/ seluruh bot = 10GB

# ── Job intervals (seconds) ───────────────────────────────────────────────────
JOB_EXPIRE_VIP_INTERVAL       = 3600   # 1 jam
JOB_CLEANUP_SESSION_INTERVAL  = 1800   # 30 menit
JOB_NOTIFY_EXPIRY_INTERVAL    = 3600   # 1 jam
JOB_CLEANUP_DISK_INTERVAL     = 3600   # NEW: cleanup disk tiap 1 jam

# ── Session cleanup ───────────────────────────────────────────────────────────
SESSION_STUCK_TIMEOUT    = 4 * 3600   # 4 jam
SESSION_INACTIVE_TIMEOUT = 24 * 3600  # 24 jam
SESSION_CACHE_MAX_SIZE   = 2000       # NEW: max entries di _session_cache RAM

# ── Error handling ────────────────────────────────────────────────────────────
ERROR_ALERT_COOLDOWN = 60  # detik

# ── Thread pool ───────────────────────────────────────────────────────────────
THREAD_POOL_WORKERS = 8
THREAD_POOL_TIMEOUT = 300  # 5 menit

# ── Webhook security ─────────────────────────────────────────────────────────
# Set PAKASIR_WEBHOOK_SECRET di .env untuk verifikasi HMAC signature.
# Generate: python3 -c "import secrets; print(secrets.token_hex(32))"
PAKASIR_WEBHOOK_SECRET = os.getenv("PAKASIR_WEBHOOK_SECRET", "")

# PENTING: WEBHOOK_PORT (Pakasir callback) != HEALTH_PORT (monitoring)
# Default 8081 ≠ 8080 agar tidak bentrok di server yang sama.
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 8081))   # FIX: was 8080 → port conflict
HEALTH_PORT  = int(os.getenv("HEALTH_PORT",  8080))
LOCAL_BOT_API_PORT = int(os.getenv("LOCAL_BOT_API_PORT", 8082))

# ── Global concurrency shielding & RAM Caching (Anti-DDoS / Anti-Spam) ────────
GLOBAL_MAX_CONCURRENT      = 32   # Maks 32 coroutine update berjalan bersamaan (sejajar dengan DB pool 32)
GLOBAL_MAX_CONCURRENT_FILE = 32   # Maks 32 proses download/upload file bersamaan (mencegah OOM/exhaustion)
USER_CLICK_COOLDOWN        = 0.3  # Jeda minimum antar klik (debounce anti-spam)
VIP_CACHE_TTL              = 30   # Durasi simpan cache status VIP di RAM (detik)

# ─── Bot Sending & Timeout Constants ──────────────────────────────────────────
SEND_PROGRESS_INTERVAL = 10   # Update progress tiap N file
SEND_MAX_RETRIES       = 3    # Maksimum percobaan kirim file
SEND_RETRY_DELAY       = 1    # Delay jeda antar retry gagal (detik)
FILE_READ_TIMEOUT      = 15   # Timeout pembacaan file
FILE_WRITE_TIMEOUT     = 20   # Timeout penulisan file
FILE_CONNECT_TIMEOUT   = 10   # Timeout koneksi ke Bot API

SEND_BATCH_SIZE  = 10
SEND_BATCH_DELAY = 1.5
SEND_FILE_DELAY  = 0.15

SEND_PARALLEL_CONCURRENT = 5
SEND_PARALLEL_DELAY      = 0.05