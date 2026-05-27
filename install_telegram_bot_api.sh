#!/bin/bash
# ==============================================================================
# Script Pemasangan Telegram Bot API Server Lokal (Precompiled Binary)
# Aman dipush ke GitHub karena membaca API_ID & API_HASH dari file .env lokal!
# ==============================================================================

set -e # Berhenti jika ada error

# 1. Pastikan file .env ada di direktori bot
if [ ! -f ".env" ]; then
    echo "[ERROR] File .env tidak ditemukan di folder ini!"
    echo "        Pastikan dijalankan di dalam direktori bot /opt/botcv"
    exit 1
fi

# 2. Baca API_ID dan API_HASH dari file .env
API_ID=$(grep -E "^API_ID=" .env | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d '[:space:]')
API_HASH=$(grep -E "^API_HASH=" .env | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d '[:space:]')

# Jika tidak ditemukan di .env, tampilkan error dan keluar
if [ -z "$API_ID" ] || [ -z "$API_HASH" ]; then
    echo "[ERROR] API_ID atau API_HASH tidak ditemukan di file .env!"
    echo "        Pastikan file .env Anda berisi baris berikut:"
    echo "        API_ID=xxx"
    echo "        API_HASH=xxx"
    exit 1
fi

echo "============================================="
echo "Memulai Instalasi Telegram Bot API Server..."
echo "============================================="

# 3. Buat folder penyimpanan data lokal
echo "[1/5] Membuat direktori penyimpanan data lokal..."
sudo mkdir -p /var/lib/telegram-bot-api
sudo touch /var/log/telegram-bot-api.log

# 4. Unduh Biner Pra-Kompilasi (glibc 2.36+ untuk Ubuntu 24.04)
echo "[2/5] Mengunduh precompiled binary (glibc 2.36+)..."
URL="https://github.com/jakbin/telegram-bot-api-binary/releases/download/2026-05-23glibc236/telegram-bot-api"
sudo wget -q -O /usr/local/bin/telegram-bot-api "$URL" || sudo curl -L -o /usr/local/bin/telegram-bot-api "$URL"

# 5. Setel izin eksekusi berkas
echo "[3/5] Mengatur perizinan berkas (chmod +x)..."
sudo chmod +x /usr/local/bin/telegram-bot-api

# 6. Buat berkas Systemd Service
echo "[4/5] Membuat systemd service (/etc/systemd/system/telegram-bot-api.service)..."
sudo bash -c "cat <<EOF > /etc/systemd/system/telegram-bot-api.service
[Unit]
Description=Telegram Bot API Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/telegram-bot-api --local --api-id=$API_ID --api-hash=$API_HASH --http-port=8082 --dir=/var/lib/telegram-bot-api --log=/var/log/telegram-bot-api.log --verbosity=1
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF"

# 7. Aktifkan & Jalankan Layanan
echo "[5/5] Memulai layanan via Systemd..."
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot-api
sudo systemctl restart telegram-bot-api

echo ""
echo "============================================="
echo "🎉 INSTALASI TELEGRAM BOT API BERHASIL!"
echo "============================================="
echo "Status Layanan:"
sudo systemctl status telegram-bot-api --no-pager -l | head -n 12
echo ""
echo "Tes Respons Server Lokal:"
curl -s http://localhost:8082
echo ""
echo "Catatan: Layanan berjalan di port 8082 (http://localhost:8082)"
echo "============================================="
