"""
cancel_helper.py
Batalkan semua proses aktif user — dipanggil dari /start dan /reset.
"""
import logging
logger = logging.getLogger(__name__)

# Core handlers (timers, locks, buffers)
from handlers.merge import _user_timers as merge_timers, _user_locks as merge_locks, _clear_buffers as merge_clear
from handlers.vcftotxt import _user_timers as vcf2txt_timers, _user_locks as vcf2txt_locks, _clear_buffers as vcf2txt_clear
from handlers.txttovcf import _user_timers as ttv_timers, _user_locks as ttv_locks, _clear_buffers as ttv_clear
from handlers.pecahvcf import _user_timers as pecah_timers
from handlers.rename import _user_timers as rename_timers, _user_locks as rename_locks

# Handlers tambahan yang perlu dibersihkan (BUG 8)
from handlers.pecahtxt import _user_timers as pecahtxt_timers, _user_locks as pecahtxt_locks, _clear_buffers as pecahtxt_clear
from handlers.xlsxtovcf import _user_timers as xlsxtovcf_timers, _user_locks as xlsxtovcf_locks, _clear_buffers as xlsxtovcf_clear
from handlers.walinkweb import _button_timers as walinkweb_timers, _processing as walinkweb_processing
from handlers.duplikat import _button_timers as duplikat_timers, _user_locks as duplikat_locks, _active_requests as duplikat_requests, _clear_buffers as duplikat_clear

# Handler bantu lainnya untuk kelengkapan
from handlers.count import _user_locks as count_locks, _user_timers as count_timers, _clear_buffers as count_clear
from handlers.admin_navy import _user_locks as admin_navy_locks
from handlers.manual import _user_timers as manual_timers, _user_locks as manual_locks


_active_tasks: dict = {}


def register_active_task(user_id: int, task):
    """Mendaftarkan task aktif (misalnya loop pengiriman file) untuk user."""
    old_task = _active_tasks.get(user_id)
    if old_task and not old_task.done():
        try:
            old_task.cancel()
        except Exception:
            pass
    _active_tasks[user_id] = task


def unregister_active_task(user_id: int):
    """Membatalkan pendaftaran task aktif user."""
    _active_tasks.pop(user_id, None)


def cancel_all(user_id: int):
    """Batalkan semua proses aktif dan bersihkan memori/disk user."""
    # 0. Cancel active background task (misal loop kirim file)
    task = _active_tasks.pop(user_id, None)
    if task and not task.done():
        try:
            task.cancel()
            logger.info("Active processing task for user %s cancelled.", user_id)
        except Exception as e:
            logger.warning("Error cancelling active task for user %s: %s", user_id, e)

    # 1. Cancel semua timer debounce / asinkronus task
    timers_list = [
        merge_timers,
        vcf2txt_timers,
        ttv_timers,
        pecah_timers,
        rename_timers,
        pecahtxt_timers,
        xlsxtovcf_timers,
        walinkweb_timers,
        duplikat_timers,
        manual_timers,
        count_timers
    ]
    for timers in timers_list:
        try:
            timer = timers.pop(user_id, None)
            if timer:
                timer.cancel()
        except Exception as e:
            logger.warning("Error cancelling timer for user %s: %s", user_id, e)

    # 2. Hapus semua buffer disk (untuk TXT, VCF, XLSX ke VCF/TXT)
    clear_funcs = [
        merge_clear,
        vcf2txt_clear,
        ttv_clear,
        pecahtxt_clear,
        xlsxtovcf_clear,
        duplikat_clear,
        count_clear
    ]
    for clear_func in clear_funcs:
        try:
            clear_func(user_id)
        except Exception as e:
            logger.warning("Error clearing buffer for user %s: %s", user_id, e)

    # 3. Hapus status processing/active requests khusus
    try:
        if user_id in walinkweb_processing:
            walinkweb_processing.discard(user_id)
    except Exception as e:
        logger.warning("Error discarding walinkweb processing for user %s: %s", user_id, e)

    try:
        duplikat_requests.pop(user_id, None)
    except Exception as e:
        logger.warning("Error popping duplikat requests for user %s: %s", user_id, e)

    # 4. Hapus semua lock
    locks_list = [
        merge_locks,
        vcf2txt_locks,
        ttv_locks,
        rename_locks,
        pecahtxt_locks,
        xlsxtovcf_locks,
        duplikat_locks,
        count_locks,
        admin_navy_locks,
        manual_locks
    ]
    for locks in locks_list:
        try:
            locks.pop(user_id, None)
        except Exception as e:
            logger.warning("Error popping lock for user %s: %s", user_id, e)
