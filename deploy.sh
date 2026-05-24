#!/bin/bash
# deploy.sh - Deploy & Update DiBot CV FEE
# Menggunakan systemd (bukan PM2/nohup) — otomatis restart jika crash.
#
# Setup pertama kali: lihat README.md bagian "VPS Deployment"
# Setelah setup, cukup jalankan: bash deploy.sh

set -e  # langsung berhenti kalau ada command yang error

SERVICE_NAME="botcv"
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Deteksi virtualenv yang ada (VPS menggunakan 'venv', beberapa lokal mungkin '.venv')
if [ -d "$BOT_DIR/venv" ]; then
    VENV_PY="$BOT_DIR/venv/bin/python"
elif [ -d "$BOT_DIR/.venv" ]; then
    VENV_PY="$BOT_DIR/.venv/bin/python"
else
    VENV_PY="$BOT_DIR/venv/bin/python"
fi

echo ""
echo "========================================"
echo "  DiBot CV FEE — Deploy Script"
echo "  Dir : $BOT_DIR"
echo "  Svc : $SERVICE_NAME"
echo "========================================"
echo ""

# ── 1. Pastikan .env ada ──────────────────────────────────────────────────────
if [ ! -f "$BOT_DIR/.env" ]; then
    echo "[ERROR] File .env tidak ditemukan!"
    echo "        Salin dari contoh: cp .env.example .env"
    echo "        Lalu isi BOT_TOKEN, ADMIN_IDS, dll."
    exit 1
fi

# ── 2. Pull update dari Git ───────────────────────────────────────────────────
echo "[1/4] Git pull..."
git -C "$BOT_DIR" pull origin main

# ── 3. Install / update dependencies ─────────────────────────────────────────
echo "[2/4] Install dependencies..."
if [ ! -f "$VENV_PY" ]; then
    echo "      Membuat virtual environment baru..."
    python3 -m venv "$BOT_DIR/venv"
    VENV_PY="$BOT_DIR/venv/bin/python"
fi
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -r "$BOT_DIR/requirements.txt"
echo "      Dependencies OK."

# ── 4. Pastikan folder logs & tmp ada ────────────────────────────────────────
mkdir -p "$BOT_DIR/logs"
mkdir -p "$BOT_DIR/tmp/sessions"

# ── 5. Reload & restart service via systemd ───────────────────────────────────
echo "[3/4] Restart service systemd..."
if systemctl is-active --quiet "$SERVICE_NAME"; then
    sudo systemctl restart "$SERVICE_NAME"
    echo "      Service di-restart."
elif systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    sudo systemctl start "$SERVICE_NAME"
    echo "      Service di-start (sebelumnya stopped)."
else
    echo ""
    echo "[WARN] Service '$SERVICE_NAME' belum terdaftar di systemd."
    echo "       Setup dulu dengan langkah di README.md bagian 'VPS Deployment'."
    echo "       Atau jalankan manual: .venv/bin/python main.py"
    exit 1
fi

# ── 6. Cek status akhir ───────────────────────────────────────────────────────
echo "[4/4] Status:"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager -l | head -20

echo ""
echo "Deploy selesai. Bot jalan via systemd ($SERVICE_NAME)."
echo "Log real-time: sudo journalctl -u $SERVICE_NAME -f"
echo ""
