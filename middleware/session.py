"""
session.py
Kelola folder sementara per user di tmp/sessions/{user_id}/

FIX DISK LIMIT: Tambahkan cek ukuran disk per-session dan global
agar VPS tidak habis disk walau ada user yang upload besar.
"""
import logging
import os
import shutil
import stat
from config import TMP_DIR

try:
    from config import MAX_DISK_PER_SESSION_MB, GLOBAL_MAX_DISK_MB
except ImportError:
    MAX_DISK_PER_SESSION_MB = 200
    GLOBAL_MAX_DISK_MB      = 10_000

logger = logging.getLogger(__name__)


def get_user_dir(user_id: int) -> str:
    path = os.path.join(TMP_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def _force_remove(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _dir_size_bytes(path: str) -> int:
    """Hitung total ukuran folder dalam bytes."""
    total = 0
    if not os.path.exists(path):
        return 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


def check_session_disk_limit(user_id: int) -> bool:
    """
    FIX DISK: Cek apakah user masih di bawah MAX_DISK_PER_SESSION_MB.
    Return True = boleh upload, False = ditolak.
    """
    path      = os.path.join(TMP_DIR, str(user_id))
    used_bytes = _dir_size_bytes(path)
    limit_bytes = MAX_DISK_PER_SESSION_MB * 1024 * 1024
    if used_bytes >= limit_bytes:
        logger.warning(
            "[session] User %s melebihi disk limit: %.1f MB / %d MB",
            user_id, used_bytes / 1024 / 1024, MAX_DISK_PER_SESSION_MB
        )
        return False
    return True


def check_global_disk_limit() -> bool:
    """
    FIX DISK: Cek apakah total tmp/ masih di bawah GLOBAL_MAX_DISK_MB.
    Return True = OK, False = VPS hampir penuh.
    """
    used_bytes  = _dir_size_bytes(TMP_DIR)
    limit_bytes = GLOBAL_MAX_DISK_MB * 1024 * 1024
    if used_bytes >= limit_bytes:
        logger.error(
            "[session] GLOBAL disk limit! %.1f GB / %d GB — bot menolak upload baru",
            used_bytes / 1024 / 1024 / 1024, GLOBAL_MAX_DISK_MB // 1024
        )
        return False
    return True


def get_session_size(user_id: int) -> int:
    """Hitung total ukuran file sesi user dalam bytes."""
    return _dir_size_bytes(os.path.join(TMP_DIR, str(user_id)))


def clear_user_dir(user_id: int):
    """Hapus semua file sesi user."""
    path = os.path.join(TMP_DIR, str(user_id))
    if os.path.exists(path):
        for root, dirs, files in os.walk(path):
            for file in files:
                try:
                    filepath = os.path.join(root, file)
                    os.chmod(filepath, stat.S_IWRITE)
                    os.unlink(filepath)
                except Exception:
                    pass
        try:
            shutil.rmtree(path, onexc=_force_remove)
        except Exception:
            pass
    os.makedirs(path, exist_ok=True)


def clear_user_dir_bg(user_id: int):
    """Jalankan clear_user_dir di background thread pool agar tidak memblokir event loop utama."""
    import asyncio
    async def _bg():
        try:
            await asyncio.to_thread(clear_user_dir, user_id)
        except Exception:
            pass
    try:
        asyncio.create_task(_bg())
    except Exception:
        pass


def cleanup_old_sessions(max_age_hours: int = 24) -> int:
    """
    FIX DISK: Hapus session folder user yang sudah tidak aktif lebih dari
    max_age_hours jam. Dipanggil dari job scheduler.
    Returns: jumlah folder yang dihapus.
    """
    import time
    cleaned = 0
    cutoff  = time.time() - (max_age_hours * 3600)
    if not os.path.exists(TMP_DIR):
        return 0
    for folder_name in os.listdir(TMP_DIR):
        folder_path = os.path.join(TMP_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        try:
            mtime = os.path.getmtime(folder_path)
            if mtime < cutoff:
                shutil.rmtree(folder_path, onexc=_force_remove)
                cleaned += 1
                logger.debug("[session] Removed stale session dir: %s", folder_name)
        except Exception as exc:
            logger.warning("[session] Error cleanup %s: %s", folder_name, exc)
    if cleaned:
        logger.info("[session] cleanup_old_sessions: removed %d stale dirs", cleaned)
    return cleaned


def clear_all_sessions():
    """Hapus semua folder sesi semua user. Dipakai saat /resetdatabase."""
    if os.path.exists(TMP_DIR):
        for folder in os.listdir(TMP_DIR):
            folder_path = os.path.join(TMP_DIR, folder)
            if os.path.isdir(folder_path):
                for root, dirs, files in os.walk(folder_path):
                    for file in files:
                        try:
                            filepath = os.path.join(root, file)
                            os.chmod(filepath, stat.S_IWRITE)
                            os.unlink(filepath)
                        except Exception:
                            pass
                try:
                    shutil.rmtree(folder_path, onexc=_force_remove)
                except Exception:
                    pass
    os.makedirs(TMP_DIR, exist_ok=True)
