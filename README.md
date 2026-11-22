# V380 CCTV 24/7 Motion Recording to Cloudinary / Cloudflare R2 & Edge Web Hub

Sistem pemantauan CCTV mandiri (*headless*) untuk kamera **Macro-Video V380 (DID: 34417145)** tanpa server/VPS berbayar dan tanpa kartu MicroSD:
* **Deteksi Gerakan Otomatis (*Motion-Triggered Recording*):** Menggunakan analisis perbedaan frame (*frame-differencing*) untuk merekam hanya saat ada aktivitas (orang lewat, motor masuk, gerbang buka).
* **Penyimpanan Terkelola di Cloudinary & Cloudflare R2:** Kuota gratis sangat awet (hanya memakai ~1.5 GB/bulan) karena hanya merekam saat ada kejadian.
* **Penghapusan Otomatis Berbasis Hari (*Retention Auto-Purge*):** File rekaman yang umurnya lebih dari $N$ hari (default: 3 hari) secara otomatis dihapus via API sehingga kuota tidak pernah penuh.
* **Web Dashboard di Cloudflare Pages Edge:** Antarmuka web modern yang di-hosting gratis dan permanen di Cloudflare Pages:
  👉 **`https://v380-surveillance-cloud.pages.dev`**

---

## 📐 Arsitektur Sistem

```
┌────────────────────────┐
│  Kamera CCTV V380      │
│  (Kamera Parkiran)     │
└───────────┬────────────┘
            │ H.264
            ▼
┌────────────────────────┐
│  v380-surveillance-app │◄─── Deteksi Gerakan (Frame Diff)
│  (Engine + Web Server) │
└─────┬────────────┬─────┘
      │            │
      │ Upload     │ Deploy & Sync
      │ Event      │
      ▼            ▼
┌──────────────┐ ┌───────────────────────────────────────┐
│  Cloudinary  │ │  Cloudflare Pages Edge Dashboard     │
│  & R2 Storage│ │  https://v380-surveillance-cloud...   │
└──────────────┘ └───────────────────────────────────────┘
```

---

## ⚙️ Variabel Konfigurasi (`.env`)

| Variabel | Bawaan | Keterangan |
| :--- | :--- | :--- |
| `DEVICE_ID` | `34417145` | ID Perangkat CCTV V380 |
| `SHARED_USERNAME` | `ldK2QjAs79` | Akun Sub-User (dari QR Code sharing) |
| `RAW_PASSWORD` | `(disimpan di GitHub Secrets)` | Token Sandi Sub-User (dari QR Code) |
| `CLOUDINARY_CLOUD_NAME` | `kngcbgye` | Nama Cloud Cloudinary Anda |
| `CLOUDINARY_API_KEY` | `(disimpan di GitHub Secrets)` | API Key Cloudinary |
| `CLOUDINARY_API_SECRET` | `(disimpan di GitHub Secrets)` | API Secret Cloudinary (untuk hapus file) |
| `RETENTION_DAYS` | `3` | Batas simpan hari (file lebih lama otomatis dihapus) |
| `MOTION_THRESHOLD` | `3.0` | Sensitivitas deteksi gerakan |
| `MOTION_COOLDOWN_SEC` | `25` | Jeda aman antar-kejadian gerakan (detik) |
| `PORT` | `8888` | Port web dashboard |

---

## 🚀 Cara Menjalankan

### 1. Menggunakan Docker (1-Klik)
```bash
docker compose up -d --build
```
Buka dashboard di `http://localhost:8888`.

### 2. Langsung dengan Python (Linux / Mac)
```bash
# Install FFmpeg
sudo apt update && sudo apt install -y ffmpeg python3-pip

# Install paket Python
pip install -r requirements.txt

# Jalankan aplikasi
python3 app.py
```

### 3. Pembersihan Otomatis via GitHub Actions
File workflow `.github/workflows/cctv_cron.yml` otomatis berjalan setiap 6 jam untuk memeriksa status kamera dan membersihkan foto gerakan yang berumur lebih dari 3 hari dari akun Cloudinary Anda.

---

## 📡 Endpoint API

* `GET /` : Web Dashboard pemantauan lengkap.
* `GET /video_feed` : Live video stream MJPEG.
* `GET /snapshot.jpg` : Cuplikan frame HD saat ini.
* `GET /api/status` : Telemetri status kamera, FPS, dan counter deteksi gerakan.
* `GET /api/motion_events` : Daftar rekaman kejadian gerakan dari Cloudinary.
* `POST /api/purge_now` : Memicu pembersihan file lama secara manual.

---

## 📄 Lisensi
Private / Proprietary - Hak Cipta Dilindungi Undang-Undang.
