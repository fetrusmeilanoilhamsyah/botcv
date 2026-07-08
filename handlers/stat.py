"""
stat.py — Dashboard owner: statistik bot realtime, bersih tanpa emoji berlebihan.
"""
import os
import time
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from middleware.auth import require_admin, is_admin

_START_TIME = time.time()
_JAKARTA = timezone(timedelta(hours=7))


def _uptime_str() -> str:
    secs = int(time.time() - _START_TIME)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    d, h   = divmod(h, 24)
    if d:
        return f"{d}h {h}j {m}m"
    if h:
        return f"{h}j {m}m {s}d"
    return f"{m}m {s}d"


def _fmt_rp(n: int) -> str:
    return f"Rp {n:,}".replace(",", ".")


def _server_health() -> str:
    """Cek disk & RAM usage VPS secara sederhana."""
    lines = []
    # Disk
    try:
        st = os.statvfs("/")
        total_gb  = st.f_blocks * st.f_frsize / (1024 ** 3)
        free_gb   = st.f_bavail * st.f_frsize / (1024 ** 3)
        used_gb   = total_gb - free_gb
        pct       = used_gb / total_gb * 100
        lines.append(f"Disk    : {used_gb:.1f}/{total_gb:.1f} GB ({pct:.0f}%)")
    except Exception:
        lines.append("Disk    : -")

    # RAM (Linux /proc/meminfo)
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.strip().split()[0])
        total_mb = mem["MemTotal"] / 1024
        avail_mb = mem["MemAvailable"] / 1024
        used_mb  = total_mb - avail_mb
        pct      = used_mb / total_mb * 100
        lines.append(f"RAM     : {used_mb:.0f}/{total_mb:.0f} MB ({pct:.0f}%)")
    except Exception:
        lines.append("RAM     : -")

    # Tmp session dir
    tmp_dir = os.path.join("tmp", "sessions")
    tmp_count = 0
    tmp_mb    = 0.0
    if os.path.exists(tmp_dir):
        for root, _, files in os.walk(tmp_dir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    tmp_mb += os.path.getsize(fp)
                    tmp_count += 1
                except Exception:
                    pass
    tmp_mb /= (1024 * 1024)
    lines.append(f"Sesi    : {tmp_count} file ({tmp_mb:.2f} MB)")

    return "\n".join(lines)


async def cmd_stat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    now_jakarta = datetime.now(_JAKARTA)

    # ── User stats ──────
    users        = db.get_all_users_detail()
    total        = len(users)
    members_list = [u for u in users if u["is_member"]]
    total_member = len(members_list)
    total_non    = total - total_member
    vip_timed    = [u for u in members_list if u.get("expired_at")]
    vip_perm     = [u for u in members_list if not u.get("expired_at")]

    # ── Payment stats ───
    pay = db.get_payment_stats()

    # ── Top 3 user aktif (exclude admin) ────────────
    all_top = db.get_top_users(50)
    top3 = [u for u in all_top if not is_admin(u.get("id", 0))][:3]

    # ── Server health ───
    health = _server_health()

    # ── VIP list (maks 10, urutkan expired terdekat) 
    try:
        vip_sorted = sorted(
            vip_timed,
            key=lambda x: datetime.fromisoformat(x["expired_at"].replace("Z", "+00:00"))
        )
    except Exception:
        vip_sorted = vip_timed

    vip_rows = []
    for idx, u in enumerate(vip_sorted[:10], 1):
        name     = u["full_name"] or u["username"] or str(u["id"])
        username = f"@{u['username']}" if u["username"] else "-"
        try:
            exp  = datetime.fromisoformat(u["expired_at"].replace("Z", "+00:00")).astimezone(_JAKARTA)
            sisa = (exp.replace(tzinfo=None) - datetime.now()).days
            sisa_str = f"{sisa}h lagi" if sisa >= 0 else "EXPIRED"
            exp_str  = exp.strftime("%d/%m/%Y")
        except Exception:
            exp_str  = "-"
            sisa_str = "-"
        usage = u.get("usage_count", 0)
        vip_rows.append(
            f"  {idx}. {name} ({username})\n"
            f"     {exp_str} — {sisa_str}  |  Penggunaan: {usage}x"
        )

    vip_block = "\n".join(vip_rows) if vip_rows else "  Tidak ada."

    # ── Top 3 block ──────
    top3_lines = []
    for idx, u in enumerate(top3, 1):
        name = u["full_name"] or u.get("username") or "-"
        top3_lines.append(f"  {idx}. {name} — {u['usage_count']}x")
    top3_block = "\n".join(top3_lines) if top3_lines else "  Belum ada data."

    # ── Rakitan pesan ────
    SEP = "─" * 28
    msg = (
        f"<b>STATISTIK BOT</b>\n"
        f"{now_jakarta.strftime('%d/%m/%Y %H:%M')} WIB  |  Uptime: {_uptime_str()}\n"
        f"{SEP}\n"
        f"<b>USER</b>\n"
        f"  Total        : {total}\n"
        f"  Non-member   : {total_non}\n"
        f"  VIP berjangka: {len(vip_timed)}\n"
        f"  VIP permanen : {len(vip_perm)}\n"
        f"{SEP}\n"
        f"<b>PEMBAYARAN (QRIS)</b>\n"
        f"  Transaksi    : {pay['total']}x\n"
        f"  Sukses       : {pay['completed']}x\n"
        f"  Pending      : {pay['pending']}x\n"
        f"  Omset        : <b>{_fmt_rp(pay['income'])}</b>\n"
        f"{SEP}\n"
        f"<b>KESEHATAN SERVER</b>\n"
        f"{health}\n"
        f"{SEP}\n"
        f"<b>TOP 3 PENGGUNA (non-admin)</b>\n"
        f"{top3_block}\n"
        f"{SEP}\n"
        f"<b>VIP AKTIF (maks 10, terdekat expired)</b>\n"
        f"{vip_block}"
    )

    await update.message.reply_text(msg, parse_mode="HTML")