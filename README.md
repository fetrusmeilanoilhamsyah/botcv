# DiBot CV FEE

Bot Telegram Python untuk konversi, manajemen, dan merge file VCF (vCard) dan TXT kontak. Dilengkapi sistem pembayaran QRIS otomatis via Pakasir, membership VIP, dan webhook handler.

---

## Fitur

### Konversi & Manajemen File
| Fitur | Deskripsi |
|---|---|
| TXT → VCF | Konversi daftar nomor ke file VCF dengan nama kontak custom |
| VCF → TXT | Ekstrak nomor dari VCF ke TXT |
| XLSX/CSV → TXT | Ekstrak nomor dari spreadsheet ke TXT |
| Merge VCF/TXT | Gabungkan banyak file VCF/TXT jadi satu, deduplikasi otomatis |
| Pecah VCF | Pisah VCF besar jadi beberapa file kecil sesuai jumlah kontak |
| Rename VCF | Ganti nama kontak di dalam file VCF secara massal |
| Count | Hitung jumlah kontak dalam file VCF/TXT |

### Sistem VIP & Pembayaran
- Pembayaran QRIS otomatis via **Pakasir** (tanpa konfirmasi manual)
- QR code render langsung di chat, VIP aktif otomatis setelah bayar
- Webhook handler untuk konfirmasi pembayaran real-time
- Fallback ke mode manual jika Pakasir tidak dikonfigurasi
- Paket: 1 minggu / 2 minggu / 3 minggu / 1 bulan

### Infrastruktur
- **Connection pool** SQLite (32 koneksi, WAL mode) — tidak crash meski concurrent
- **Async DB wrapper** — semua DB call via thread pool, event loop tidak diblokir
- **Session disk-based** — proses file besar tanpa OOM
- **Rate limiting** per user (semaphore) + per file
- **Scheduled jobs** — expire VIP otomatis, cleanup session, expire payment stale
- **Health check endpoint** — `GET /health`
- **Webhook HMAC verification** — keamanan double-layer (HMAC + order_id + amount)

---

## Perintah Bot

### User
| Command | Deskripsi |
|---|---|
| `/start` | Mulai bot / reset sesi |
| `/vip` | Lihat paket & beli VIP via QRIS |
| `/akun` | Cek status akun & VIP |
| `/referral` | Kode referral kamu |
| `/txttovcf` | Mulai konversi TXT → VCF |
| `/vcftotxt` | Mulai konversi VCF → TXT |
| `/xlsxtotxt` | Mulai konversi XLSX/CSV → TXT |
| `/merge` | Gabungkan file VCF/TXT |
| `/pecahvcf` | Pecah VCF jadi beberapa file |
| `/rename` | Rename kontak dalam VCF |
| `/count` | Hitung kontak dalam file |
| `/done` | Selesaikan proses yang sedang berjalan |
| `/reset` | Reset sesi aktif |

### Admin Only
| Command | Deskripsi |
|---|---|
| `/admin` | Panel admin utama |
| `/addvip <user_id> <hari>` | Tambah VIP manual |
| `/delvip <user_id>` | Cabut VIP |
| `/newmember` | Tambah member manual |
| `/delmember` | Hapus member |
| `/daftar` | Lihat semua user terdaftar |
| `/stat` | Statistik bot |
| `/broadcast` | Broadcast teks ke semua user |
| `/mediabroadcast` | Broadcast media ke semua user |
| `/resetdatabase` | Reset log & sesi (user & VIP aman) |

---

## Kebutuhan

- Python **3.10+**
- VPS Linux (minimum 1 vCPU, 1GB RAM — recommended 2GB)
- Telegram Bot Token dari [@BotFather](https://t.me/BotFather)
- (Opsional) Akun [Pakasir](https://app.pakasir.com) untuk QRIS otomatis

---

## Instalasi Lokal (Testing)

```bash
# Clone repo
git clone https://github.com/fetrusmeilanoilhamsyah/bot-cv.git
cd bot-cv

# Buat virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Konfigurasi .env
cp .env.example .env
nano .env                          # isi BOT_TOKEN, ADMIN_IDS, dll

# Buat folder yang dibutuhkan
mkdir -p logs tmp/sessions

# Jalankan
python main.py
```

---

## Deploy ke VPS (systemd)

Bot ini menggunakan **systemd** untuk auto-restart otomatis jika crash. Tidak perlu PM2 atau nohup.

### 1. Upload ke VPS

```bash
# Di VPS — clone repo
git clone https://github.com/fetrusmeilanoilhamsyah/bot-cv.git /opt/botcv
cd /opt/botcv

# Setup venv & install
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Konfigurasi .env
cp .env.example .env
nano .env
```

### 2. Buat systemd service

```bash
sudo nano /etc/systemd/system/botcv.service
```

Isi dengan:

```ini
[Unit]
Description=DiBot CV FEE - Telegram Bot
Documentation=https://github.com/fetrusmeilanoilhamsyah/bot-cv
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/botcv
ExecStart=/opt/botcv/venv/bin/python main.py
EnvironmentFile=/opt/botcv/.env

# Auto-restart jika crash
Restart=always
RestartSec=5

# Logging ke journald (bisa dilihat via journalctl)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=botcv

# Batas resource
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

### 3. Aktifkan & jalankan

```bash
sudo systemctl daemon-reload
sudo systemctl enable botcv       # auto-start saat VPS reboot
sudo systemctl start botcv        # jalankan sekarang
```

### 4. Cek status & log

```bash
# Status singkat
sudo systemctl status botcv

# Log real-time (Ctrl+C untuk keluar)
sudo journalctl -u botcv -f

# Log 100 baris terakhir
sudo journalctl -u botcv -n 100

# Log dari file (jika menggunakan RotatingFileHandler)
tail -f /opt/botcv/logs/bot.log
```

### 5. Update bot (setelah push ke GitHub)

Cukup jalankan:

```bash
cd /opt/botcv
bash deploy.sh
```

Script `deploy.sh` akan otomatis: git pull → install deps → restart systemd service.

---

## Konfigurasi Webhook Pakasir (QRIS Otomatis)

Agar pembayaran QRIS aktif otomatis tanpa konfirmasi manual:

### Setup di .env

```env
PAKASIR_ENABLED=true
PAKASIR_SLUG=your-project-slug
PAKASIR_API_KEY=your-api-key
PAKASIR_SANDBOX=false              # false untuk production
PAKASIR_WEBHOOK_SECRET=            # KOSONGKAN jika Pakasir tidak mengirim signature header (verifikasi aman via Order ID + Amount + Status)
WEBHOOK_PORT=8081
HEALTH_PORT=8080
```

> **Catatan port**: `WEBHOOK_PORT` untuk menerima callback dari Pakasir. `HEALTH_PORT` untuk health check endpoint. Pastikan berbeda.
> **PENTING**: Jika dashboard Pakasir tidak mengirimkan header signature `X-Pakasir-Signature`, kosongkan `PAKASIR_WEBHOOK_SECRET=` di `.env`. Bot akan secara otomatis beralih menggunakan pencocokan transaksi aman (Layer 2) yang 100% aman dan lancar!

### Buka port di firewall VPS

```bash
sudo ufw allow 8081/tcp            # webhook Pakasir
sudo ufw allow 8080/tcp            # health check (opsional)
```

### Daftarkan URL webhook di dashboard Pakasir

```
http://IP_VPS_KAMU:8081/webhook/pakasir
```

Atau jika pakai domain + nginx reverse proxy:
```
https://bot.domain.com/webhook/pakasir
```

### Nginx reverse proxy (opsional, untuk HTTPS)

```nginx
server {
    listen 443 ssl;
    server_name bot.domain.com;

    # ssl_certificate ...

    location /webhook/pakasir {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /health {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

---

## Struktur Project

```
botcv/
├── main.py                    # Entry point, handler routing, scheduled jobs
├── config.py                  # Semua konstanta dari .env
├── webhook_pakasir.py         # Aiohttp webhook server untuk Pakasir
├── .env                       # Konfigurasi (JANGAN commit ke git)
├── .env.example               # Template .env
├── deploy.sh                  # Script deploy via systemd
├── requirements.txt
│
├── core/
│   ├── pakasir.py             # Pakasir API client + HMAC webhook validator
│   ├── vcf_parser.py          # Parse & generate file VCF
│   ├── vcf_builder.py
│   ├── vcf_merger.py
│   ├── vcf_splitter.py
│   ├── txt_exporter.py
│   └── utils.py
│
├── database/
│   ├── db.py                  # Connection pool, semua fungsi DB sync
│   ├── db_async.py            # Async wrapper (thread pool executor)
│   ├── db_payments.py         # Re-export payment functions
│   └── migrations/
│       └── 001_init.sql
│
├── handlers/                  # Satu file per command/fitur
│   ├── start.py
│   ├── vip_pakasir.py         # /vip + flow pembayaran QRIS
│   ├── addvip.py
│   ├── merge.py
│   ├── txttovcf.py
│   ├── vcftotxt.py
│   ├── xlsxtotxt.py
│   ├── pecahvcf.py
│   ├── rename.py
│   ├── count.py
│   ├── broadcast.py
│   ├── cancel_helper.py
│   └── ...
│
├── middleware/
│   ├── auth.py                # require_member / require_admin
│   └── session.py             # Disk session management + limit guard
│
├── logs/                      # Log file (auto-created)
└── tmp/sessions/              # Temp file per user (auto-created, auto-cleanup)
```

---

## Environment Variables

| Variable | Wajib | Default | Keterangan |
|---|---|---|---|
| `BOT_TOKEN` | Ya | — | Token dari BotFather |
| `ADMIN_IDS` | Ya | `0` | ID admin, pisah koma: `123,456` |
| `ADMIN_CONTACT` | Tidak | `@admin` | Username admin untuk mention |
| `GROUP_LINK` | Tidak | — | Link grup Telegram |
| `HARGA_MEMBER` | Tidak | `Hubungi admin` | Info harga untuk mode manual |
| `TUTORIAL_LINK` | Tidak | — | Link tutorial |
| `PAKASIR_ENABLED` | Tidak | `false` | `true` untuk aktifkan QRIS otomatis |
| `PAKASIR_SLUG` | Jika enabled | — | Slug project dari dashboard Pakasir |
| `PAKASIR_API_KEY` | Jika enabled | — | API key dari dashboard Pakasir |
| `PAKASIR_SANDBOX` | Tidak | `false` | `true` untuk mode testing |
| `PAKASIR_WEBHOOK_SECRET` | Recommended | — | Secret untuk verifikasi HMAC webhook |
| `WEBHOOK_PORT` | Tidak | `8081` | Port webhook server Pakasir |
| `HEALTH_PORT` | Tidak | `8080` | Port health check endpoint |

---

## Troubleshooting

**Bot tidak start / crash:**
```bash
sudo journalctl -u botcv -n 50 --no-pager
```

**Database locked / pool exhausted:**
```bash
# Cek apakah ada proses python lain yang pegang DB
fuser /opt/botcv/database/bot.db
```

**Webhook Pakasir tidak diterima:**
```bash
# Pastikan port terbuka
sudo ufw status
# Test dari luar
curl -X POST http://IP_VPS:8081/webhook/pakasir -H "Content-Type: application/json" -d '{}'
# Harusnya balik 400 "Missing required fields" — artinya server jalan
```

**Lihat payment pending:**
```bash
sqlite3 /opt/botcv/database/bot.db "SELECT order_id, user_id, amount, status, created_at FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT 10;"
```

---

## License

Copyright (c) 2026 Fetrus Meilano Ilhamsyah — MIT License
