import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN     = os.getenv("BOT_TOKEN")
ADMIN_IDS     = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@admin")
GROUP_LINK    = os.getenv("GROUP_LINK", "https://t.me/grup")
HARGA_MEMBER  = os.getenv("HARGA_MEMBER", "Hubungi admin")
TUTORIAL_LINK = os.getenv("TUTORIAL_LINK", "https://t.me/tutorialnotceve")

TMP_DIR       = os.path.join(os.path.dirname(__file__), "tmp", "sessions")

# File handling limits
MAX_FILES_PER_SESSION = 40096
MAX_UPLOAD_SIZE_MB = 500
MAX_CONTACTS_PER_FILE = 10000

# Job intervals (seconds)
JOB_EXPIRE_VIP_INTERVAL = 3600      # 1 hour
JOB_CLEANUP_SESSION_INTERVAL = 1800  # 30 minutes
JOB_NOTIFY_EXPIRY_INTERVAL = 3600   # 1 hour

# Session cleanup
SESSION_STUCK_TIMEOUT = 4 * 3600  # 4 hours
SESSION_INACTIVE_TIMEOUT = 24 * 3600  # 24 hours

# Error handling
ERROR_ALERT_COOLDOWN = 60  # seconds

# Thread pool
THREAD_POOL_WORKERS = 8
THREAD_POOL_TIMEOUT = 300  # 5 minutes
