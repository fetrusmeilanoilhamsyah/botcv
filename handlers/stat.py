"""
stat.py — Admin dashboard: statistik bot realtime.
"""
import os
import time
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from middleware.auth import require_admin

# Waktu startup bot (diset saat import)
_START_TIME = time.time()


def _uptime_str() -> str:
    secs = int(time.time() - _START_TIME)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}j {m}m {s}s"
    return f"{m}m {s}s"


async def cmd_stat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    users = db.get_all_users_detail()
    total = len(users)
    
    # Hitung tipe member
    members_list = [u for u in users if u["is_member"]]
    total_member = len(members_list)
    total_non    = total - total_member
    
    vip_timed = [u for u in members_list if u.get("expired_at")]
    vip_perm  = [u for u in members_list if not u.get("expired_at")]
    
    # Hitung file temp
    tmp_dir    = os.path.join("tmp", "sessions")
    tmp_count  = 0
    tmp_size   = 0
    if os.path.exists(tmp_dir):
        for root, dirs, files in os.walk(tmp_dir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    tmp_size += os.path.getsize(fp)
                    tmp_count += 1
                except Exception:
                    pass

    tmp_size_mb = tmp_size / (1024 * 1024)
    
    # Statistik Pembayaran & Omset
    pay_stats = db.get_payment_stats()
    def _fmt_money(n: int) -> str:
        return f"Rp {n:,}".replace(",", ".")
    
    # Leaderboard Top 5
    top_users = db.get_top_users(5)
    lb_text = "🏆 <b>TOP 5 USER AKTIF:</b>\n"
    if top_users:
        for idx, u in enumerate(top_users, 1):
            lb_text += f"  {idx}. {u['full_name']} — {u['usage_count']}x\n"
    else:
        lb_text += "  Belum ada data.\n"
        
    # Daftar Detail Member VIP Aktif (maks 15 agar tidak limit karakter)
    from datetime import timezone, timedelta, datetime
    jakarta_tz = timezone(timedelta(hours=7))
    
    vip_list_text = "\n💎 <b>DAFTAR MEMBER VIP AKTIF (Maks 15):</b>\n"
    if vip_timed:
        # Urutkan berdasarkan waktu expired terdekat
        try:
            vip_timed_sorted = sorted(
                vip_timed,
                key=lambda x: datetime.fromisoformat(x["expired_at"].replace("Z", "+00:00"))
            )
        except Exception:
            vip_timed_sorted = vip_timed
            
        for idx, u in enumerate(vip_timed_sorted[:15], 1):
            username = f"@{u['username']}" if u["username"] else "No username"
            name = u["full_name"] or u["username"] or str(u["id"])
            usage = u.get("usage_count", 0)
            
            try:
                exp = datetime.fromisoformat(u["expired_at"].replace("Z", "+00:00")).astimezone(jakarta_tz)
                sisa = (exp.replace(tzinfo=None) - datetime.now()).days
                exp_str = exp.strftime("%d/%m/%Y %H:%M") + " WIB"
                sisa_str = f"({sisa} hari lagi)" if sisa >= 0 else "(Expired)"
            except Exception:
                exp_str = str(u["expired_at"])
                sisa_str = ""
                
            vip_list_text += f"  {idx}. <b>{name}</b> ({username})\n"
            vip_list_text += f"     ID: <code>{u['id']}</code> | Pakai: {usage}x\n"
            vip_list_text += f"     Expired: {exp_str} {sisa_str}\n"
    else:
        vip_list_text += "  Tidak ada member VIP aktif.\n"

    # Daftar Member Permanen
    perm_list_text = "\n👑 <b>MEMBER PERMANEN / ADMIN:</b>\n"
    if vip_perm:
        for idx, u in enumerate(vip_perm[:10], 1):
            username = f"@{u['username']}" if u["username"] else "No username"
            name = u["full_name"] or str(u["id"])
            usage = u.get("usage_count", 0)
            perm_list_text += f"  {idx}. <b>{name}</b> ({username}) | Pakai: {usage}x\n"
    else:
        perm_list_text += "  Tidak ada member permanen.\n"

    msg = (
        f"📊 <b>STATISTIK BOT REALTIME</b>\n"
        f"{'─'*28}\n"
        f"⏱ <b>Uptime:</b> {_uptime_str()}\n"
        f"👥 <b>Total User:</b> {total} orang\n"
        f"💼 <b>Non-Member:</b> {total_non} orang\n"
        f"⭐ <b>Total Member:</b> {total_member} orang\n"
        f"   └ 💎 VIP Berjangka : {len(vip_timed)} orang\n"
        f"   └ 👑 Permanen/Admin : {len(vip_perm)} orang\n"
        f"📂 <b>Tmp Disk Sesi:</b> {tmp_count} file ({tmp_size_mb:.2f} MB)\n"
        f"{'─'*28}\n"
        f"💳 <b>STATISTIK PEMBAYARAN (QRIS):</b>\n"
        f"   ├ Total Transaksi : {pay_stats['total']}x\n"
        f"   ├ Sukses Terbayar : {pay_stats['completed']}x\n"
        f"   ├ Pending/Expired : {pay_stats['pending']}x\n"
        f"   └ 💰 <b>Total Omset : {_fmt_money(pay_stats['income'])}</b>\n"
        f"{'─'*28}\n"
        + lb_text
        + vip_list_text
        + perm_list_text
    )

    await update.message.reply_text(msg, parse_mode="HTML")