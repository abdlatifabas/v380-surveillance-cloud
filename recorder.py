#!/usr/bin/env python3
"""
V380 CCTV Standalone Cloud Recorder (untuk GitHub Actions)
- Menghubungi cloud dispatch & autentikasi relay via protokol biner V380
- Menangkap frame video stream (H.264 -> MJPEG via FFmpeg)
- Mengupload snapshot terbaru (cctv_latest.jpg) ke Cloudinary
- Menganalisis gerakan (frame difference)
- Jika terdeteksi gerakan: meng-encode video MP4 dan mengupload ke Cloudinary dengan HLS eager playlist
- Mengupdate cctv_manifest.json ke Cloudinary untuk dashboard Cloudflare Pages
- 100% serverless, zero-cost, berjalan terjadwal via GitHub Actions
"""

import os
import io
import sys
import time
import socket
import struct
import hashlib
import base64
import datetime
import requests
import subprocess
import json
import threading
import collections
from PIL import Image, ImageChops, ImageStat, ImageDraw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from requests.auth import HTTPBasicAuth

# ─── Konfigurasi dari Environment / Secrets ──────────────────────────────────
DEVICE_ID            = int(os.environ.get("DEVICE_ID", "34417145"))
SHARED_USERNAME      = os.environ.get("SHARED_USERNAME", "ldK2QjAs79")
RAW_PASSWORD         = os.environ.get("RAW_PASSWORD", "")
DISPATCH_URL         = os.environ.get("DISPATCH_URL", "http://dispatch.av380.net:8001/api/v1/get_stream_server")
AES_SECRET_KEY       = b"macrovideo+*#!^@"

CLOUD_NAME           = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
API_KEY              = os.environ.get("CLOUDINARY_API_KEY", "")
API_SECRET           = os.environ.get("CLOUDINARY_API_SECRET", "")
BASE_URL             = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}"

TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_NOTIF_MODE  = os.environ.get("TELEGRAM_NOTIF_MODE", "all")
DASHBOARD_URL        = "https://abdlatifabas.github.io/v380-surveillance-cloud/"

RETENTION_DAYS       = int(os.environ.get("RETENTION_DAYS", "3"))
MOTION_THRESHOLD     = float(os.environ.get("MOTION_THRESHOLD", "3.0"))
SEGMENT_DURATION_SEC = int(os.environ.get("SEGMENT_DURATION_SEC", "25"))
MAX_STREAM_SEC       = int(os.environ.get("MAX_STREAM_SEC", "45"))

# ─── V380 Kriptografi & Protokol Biner ─────────────────────────────────────────
def pkcs7_pad(data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()

def encrypt_aes_ecb(data: bytes, key: bytes) -> bytes:
    padded = pkcs7_pad(data)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()

def change_dev_pwd(pwd: str) -> str:
    if len(pwd) <= 28:
        return pwd
    m = hashlib.md5(pwd.encode('utf-8')).hexdigest()
    m16 = m[8:24]
    return base64.b64encode(m16.encode('utf-8')).decode('utf-8')

def find_active_relay(device_id: int, username: str, chg_pwd: str):
    platform = 10001
    t = int(time.time())
    sign_raw = f'dev_id={device_id}&platform={platform}&timestamp={t}hsdata2022'
    sign = hashlib.sha1(sign_raw.encode()).hexdigest()
    dispatch_ips = []
    try:
        resp = requests.post(
            DISPATCH_URL,
            json={'dev_id': device_id, 'platform': platform, 'timestamp': t, 'sign': sign},
            timeout=6.0
        )
        dispatch_ips = [item['ip'] for item in resp.json().get('data', [])]
        print(f"[DISPATCH] Mendapatkan {len(dispatch_ips)} relay IP: {dispatch_ips}")
    except Exception as e:
        print(f"[DISPATCH] Error: {e}")

    fallbacks = [
        '45.33.60.129', '138.2.233.2', '54.203.132.115', '50.116.11.56',
        '35.160.49.146', '45.33.37.15', '96.126.102.130', '192.9.128.89',
        '143.198.232.176', '159.223.167.140', '167.99.147.202'
    ]

    all_ips = dispatch_ips + [ip for ip in fallbacks if ip not in dispatch_ips]

    for attempt in range(2):
        if attempt > 0:
            print("[RELAY] Menunggu 3 detik sebelum retry...")
            time.sleep(3)
        for ip in all_ips:
            ticket, session = authenticate_relay(ip, device_id, username, chg_pwd)
            if ticket is not None and ticket != 0 and session != 0:
                print(f"[RELAY] ✅ Autentikasi sukses ke {ip} (ticket={ticket}, session={session})")
                return ip, ticket, session

    return None, None, None

def authenticate_relay(relay_ip: str, device_id: int, username: str, chg_pwd: str):
    pkt = bytearray(256)
    struct.pack_into('<I', pkt, 0, 1167)
    struct.pack_into('<I', pkt, 4, 1072)
    pkt[8] = 3
    struct.pack_into('<I', pkt, 9, 11)
    struct.pack_into('<I', pkt, 13, device_id)
    domain = f'{device_id}.nvdvr.net'.encode('ascii')
    pkt[17:17+len(domain)] = domain
    struct.pack_into('<I', pkt, 67, 8800)
    user_b = username.encode('ascii')
    pkt[71:71+len(user_b)] = user_b

    rk = b'abcdef0123456789'
    pkt[103:119] = rk

    enc1 = encrypt_aes_ecb(chg_pwd.encode('utf-8'), AES_SECRET_KEY)
    enc2 = encrypt_aes_ecb(enc1, rk)
    pkt[119:119+len(enc2)] = enc2
    pkt[231] = 2

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(6.0)
    try:
        s.connect((relay_ip, 8800))
        s.sendall(pkt)
        resp = s.recv(256)
    except Exception as e:
        print(f"[AUTH] {relay_ip} koneksi gagal: {e}")
        return None, None
    finally:
        s.close()

    if len(resp) >= 8:
        cmd, ret = struct.unpack_from('<II', resp, 0)
        print(f"[AUTH] {relay_ip} response: cmd={cmd}, ret={ret}")
        if ret == 1001:
            ticket = struct.unpack_from('<I', resp, 13)[0]
            session = struct.unpack_from('<I', resp, 17)[0]
            return ticket, session
        elif ret == 1002:
            print(f"[AUTH] {relay_ip} status: Kamera sedang cooldown / sesi aktif (1002)")
    return None, None

# ─── Visual Watermark Helper ──────────────────────────────────────────────────
def apply_cctv_watermark(frame_bytes: bytes, motion_score: float = 0.0) -> bytes:
    """Menambahkan burn-in OSD watermark permanen pada frame CCTV (bukti hukum)."""
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size

        wib = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
        ts_str = wib.strftime("%Y-%m-%d %H:%M:%S WIB")
        osd_top = f"🔴 CCTV CAM-01 | DID: {DEVICE_ID} | PARKIRAN DEPAN"
        osd_bottom = f"{ts_str} | DETEKSI: {motion_score:.1f}" if motion_score > 0 else f"{ts_str} | LIVE"

        draw.rectangle([(0, 0), (w, 24)], fill=(0, 0, 0))
        draw.rectangle([(0, h - 24), (w, h)], fill=(0, 0, 0))
        draw.text((10, 5), osd_top, fill=(255, 65, 65))
        draw.text((10, h - 19), osd_bottom, fill=(240, 240, 240))

        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=88)
        return out_buf.getvalue()
    except Exception:
        return frame_bytes

# ─── Telegram Notifikasi Helper ──────────────────────────────────────────────
def send_telegram_alert(jpg_bytes: bytes, caption: str, mp4_url: str = "", hls_url: str = ""):
    """Kirim foto bukti dengan tombol inline interaktif dan mekanisme retry."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    buttons = []
    row1 = []
    if mp4_url:
        row1.append({"text": "▶ Tonton MP4 (Langsung)", "url": mp4_url})
    if hls_url:
        row1.append({"text": "🎥 Stream HLS", "url": hls_url})
    if row1:
        buttons.append(row1)
    buttons.append([{"text": "🌐 Buka Live Dashboard", "url": DASHBOARD_URL}])

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": buttons})
    }
    files = {"photo": ("snapshot.jpg", jpg_bytes, "image/jpeg")}

    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data=payload, files=files, timeout=25
            )
            if r.status_code == 200:
                print("[TELEGRAM] ✅ Notifikasi interaktif berhasil dikirim!")
                return
            elif r.status_code == 429:
                wait_sec = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(wait_sec)
            else:
                print(f"[TELEGRAM] ❌ {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"[TELEGRAM] ❌ Retry {attempt+1}: {e}")
            time.sleep(2)

def send_telegram_photo(jpg_bytes: bytes, caption: str):
    """Fallback kompatibilitas lama."""
    send_telegram_alert(jpg_bytes, caption)

# ─── Cloudinary Helper ────────────────────────────────────────────────────────
def _signed_params(params: dict) -> dict:
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params)) + API_SECRET
    sig = hashlib.sha1(to_sign.encode()).hexdigest()
    result = dict(params)
    result["api_key"] = API_KEY
    result["signature"] = sig
    return result

def upload_snapshot(jpg_bytes: bytes) -> str | None:
    t = int(time.time())
    today_str = datetime.date.today().strftime('%Y%m%d')
    params = {
        "public_id": "cctv_latest",
        "overwrite": "true",
        "invalidate": "true",
        "tags": f"live_feed,v380,tgl_{today_str}",
        "timestamp": str(t),
    }
    signed = _signed_params(params)
    files = {"file": ("snapshot.jpg", jpg_bytes, "image/jpeg")}
    try:
        r = requests.post(f"{BASE_URL}/image/upload", data=signed, files=files, timeout=15)
        if r.status_code == 200:
            url = r.json().get("secure_url")
            print(f"[SNAPSHOT] ✅ Upload snapshot sukses: {url}")
            return url
        else:
            print(f"[SNAPSHOT] ❌ Error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"[SNAPSHOT] ❌ Exception: {e}")
    return None

def upload_video(mp4_bytes: bytes, public_id: str, tags: str) -> str | None:
    t = int(time.time())
    params = {
        "public_id": public_id,
        "tags": tags,
        "timestamp": str(t),
        "overwrite": "true",
        "eager": "sp_hd/m3u8",
        "eager_async": "true",
    }
    signed = _signed_params(params)
    files = {"file": ("recording.mp4", mp4_bytes, "video/mp4")}
    try:
        r = requests.post(
            f"{BASE_URL}/video/upload",
            data=signed, files=files, timeout=120
        )
        if r.status_code == 200:
            url = r.json().get("secure_url", "")
            print(f"[VIDEO] ✅ Upload video sukses ({len(mp4_bytes)//1024} KB): {url}")
            return url
        else:
            print(f"[VIDEO] ❌ Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[VIDEO] ❌ Exception: {e}")
    return None

def list_recent_videos(max_results: int = 20) -> list:
    try:
        url = f"{BASE_URL}/resources/video/tags/motion_video?max_results={max_results}&direction=desc"
        r = requests.get(url, auth=HTTPBasicAuth(API_KEY, API_SECRET), timeout=10)
        if r.status_code == 200:
            return r.json().get("resources", [])
    except Exception as e:
        print(f"[LIST] Error: {e}")
    return []

def purge_old_videos(retention_days: int = RETENTION_DAYS):
    today = datetime.date.today()
    total_deleted = 0
    for i in range(retention_days + 1, retention_days + 60):
        old_date = today - datetime.timedelta(days=i)
        tag = f"tgl_{old_date.strftime('%Y%m%d')}"
        try:
            r = requests.delete(
                f"{BASE_URL}/resources/video/tags/{tag}",
                auth=HTTPBasicAuth(API_KEY, API_SECRET), timeout=10
            )
            if r.status_code == 200:
                n = len(r.json().get("deleted", {}))
                if n > 0:
                    total_deleted += n
                    print(f"[PURGE] 🗑️ Hapus {n} video lama: {tag}")
            requests.delete(
                f"{BASE_URL}/resources/image/tags/{tag}",
                auth=HTTPBasicAuth(API_KEY, API_SECRET), timeout=10
            )
        except Exception:
            pass
    if total_deleted == 0:
        print(f"[PURGE] ✅ Bersih, tidak ada data kedaluwarsa")
    return total_deleted

def get_hls_url(public_id: str) -> str:
    return f"https://res.cloudinary.com/{CLOUD_NAME}/video/upload/sp_hd/{public_id}.m3u8"

def build_and_upload_manifest():
    print("[MANIFEST] Memperbarui cctv_manifest.json di Cloudinary ...")
    videos = list_recent_videos(max_results=30)
    manifest = []
    for v in videos:
        pid = v.get("public_id", "")
        manifest.append({
            "public_id": pid,
            "url": v.get("secure_url", ""),
            "hls_url": get_hls_url(pid),
            "created_at": v.get("created_at", ""),
            "duration": round(v.get("duration", 0), 1),
            "bytes": v.get("bytes", 0),
            "thumbnail": f"https://res.cloudinary.com/{CLOUD_NAME}/video/upload/so_0,w_320,h_180,c_fill/{pid}.jpg",
        })

    json_bytes = json.dumps({
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cloud_name": CLOUD_NAME,
        "snapshot_url": f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/cctv_latest.jpg",
        "videos": manifest
    }, indent=2).encode()

    t = int(time.time())
    params = {
        "public_id": "cctv_manifest.json",
        "overwrite": "true",
        "invalidate": "true",
        "timestamp": str(t),
    }
    signed = _signed_params(params)
    files = {"file": ("cctv_manifest.json", json_bytes, "application/json")}
    try:
        r = requests.post(f"{BASE_URL}/raw/upload", data=signed, files=files, timeout=15)
        if r.status_code == 200:
            url = r.json().get("secure_url")
            print(f"[MANIFEST] ✅ cctv_manifest.json sukses diupload: {url}")
            return url
    except Exception as e:
        print(f"[MANIFEST] ❌ {e}")
    return None

# ─── Stream & Motion Recording Session ────────────────────────────────────────
def stream_and_record():
    chg_pwd = change_dev_pwd(RAW_PASSWORD)
    relay_ip, ticket, session = find_active_relay(DEVICE_ID, SHARED_USERNAME, chg_pwd)

    if not relay_ip:
        print("[STREAM] ❌ Gagal terhubung ke relay camera (kamera offline / cooldown)")
        return False

    s_stream = None
    ffmpeg_proc = None
    collected_frames = []
    pre_roll_buffer = collections.deque(maxlen=30)  # Menyimpan 3 detik frame sebelum gerakan
    first_snapshot_frame = None
    motion_detected = False
    motion_score = 0.0
    snapshot_uploaded = False

    try:
        s_stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_stream.settimeout(12.0)
        s_stream.connect((relay_ip, 8800))

        # Cmd 301 (Stream Login)
        cmd301 = bytearray(256)
        struct.pack_into('<I', cmd301, 0, 301)
        struct.pack_into('<I', cmd301, 4, 1072)
        domain = f'{DEVICE_ID}.nvdvr.net'.encode('ascii')
        cmd301[8:8+len(domain)] = domain
        struct.pack_into('<I', cmd301, 58, 8800)
        struct.pack_into('<I', cmd301, 62, DEVICE_ID)
        struct.pack_into('<I', cmd301, 66, ticket)
        struct.pack_into('<I', cmd301, 70, session)
        struct.pack_into('<I', cmd301, 74, 1)  # HD
        cmd301[78] = 20
        struct.pack_into('<I', cmd301, 79, 1)

        s_stream.sendall(cmd301)
        resp401 = s_stream.recv(32)

        # Launch FFmpeg H.264 -> MJPEG decoder
        ffmpeg_proc = subprocess.Popen([
            'ffmpeg', '-y', '-f', 'h264', '-i', '-',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '3', '-'
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        prev_gray = None
        is_running = True

        def read_jpegs():
            nonlocal prev_gray, motion_detected, motion_score, snapshot_uploaded, first_snapshot_frame
            buf = bytearray()
            frame_count = 0
            while is_running and ffmpeg_proc.poll() is None:
                try:
                    chunk = ffmpeg_proc.stdout.read(8192)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        soi = buf.find(b'\xff\xd8')
                        if soi < 0:
                            break
                        eoi = buf.find(b'\xff\xd9', soi + 2)
                        if eoi < 0:
                            break
                        frame = bytes(buf[soi:eoi+2])
                        buf = buf[eoi+2:]
                        frame_count += 1

                        # Upload frame pertama sebagai snapshot terkini ber-watermark
                        if not snapshot_uploaded:
                            snapshot_uploaded = True
                            watermarked_snap = apply_cctv_watermark(frame)
                            first_snapshot_frame = watermarked_snap
                            threading.Thread(target=upload_snapshot, args=(watermarked_snap,), daemon=True).start()

                        # Deteksi gerakan
                        try:
                            img = Image.open(io.BytesIO(frame)).convert('L').resize((160, 90))
                            if prev_gray is not None:
                                diff = ImageChops.difference(prev_gray, img)
                                score = ImageStat.Stat(diff).mean[0]
                                if score >= MOTION_THRESHOLD:
                                    motion_detected = True
                                    motion_score = max(motion_score, score)
                            prev_gray = img
                        except Exception:
                            pass

                        # Kumpulkan frame: sertakan pre-roll buffer agar momen awal tidak terpotong
                        if not motion_detected:
                            pre_roll_buffer.append(frame)
                        else:
                            if len(collected_frames) == 0 and len(pre_roll_buffer) > 0:
                                collected_frames.extend(list(pre_roll_buffer))
                                pre_roll_buffer.clear()
                            collected_frames.append(frame)

                except Exception:
                    break

        t_read = threading.Thread(target=read_jpegs, daemon=True)
        t_read.start()

        # Cmd 303 (Start Stream)
        cmd303 = bytearray(256)
        struct.pack_into('<I', cmd303, 0, 303)
        struct.pack_into('<H', cmd303, 4, 0x3001)
        s_stream.sendall(cmd303)
        print("[STREAM] 🎥 Streaming aktif, memantau gerakan...")

        # Reassemble H.264 packets
        sock_buf = bytearray()
        def read_sock_exact(n):
            nonlocal sock_buf
            while len(sock_buf) < n:
                chunk = s_stream.recv(16384)
                if not chunk:
                    return None
                sock_buf.extend(chunk)
            res = sock_buf[:n]
            sock_buf = sock_buf[n:]
            return res

        video_frags = bytearray()
        video_total = 0
        start_time = time.time()

        while time.time() - start_time < MAX_STREAM_SEC:
            # Cari sync byte 0x7F
            while True:
                b = read_sock_exact(1)
                if not b or b[0] == 0x7F:
                    break
            if not b:
                break

            rest = read_sock_exact(11)
            if not rest:
                break

            header = b + rest
            f_type = header[1]
            total_f = struct.unpack_from('<H', header, 3)[0]
            cur_f = struct.unpack_from('<H', header, 5)[0]
            pay_len = struct.unpack_from('<H', header, 7)[0]

            if pay_len == 0 or pay_len > 20000 or cur_f >= total_f:
                continue

            payload = read_sock_exact(pay_len)
            if not payload:
                break

            if f_type in (0x00, 0x01, 0x28, 0x29):
                if cur_f == 0:
                    video_frags = bytearray()
                    video_total = total_f
                video_frags.extend(payload)

                if cur_f == total_f - 1 and len(video_frags) >= 16:
                    full = bytes(video_frags)
                    video_frags = bytearray()
                    sc = full.find(b'\x00\x00\x00\x01')
                    if sc >= 0 and sc <= 16:
                        h264_frame = full[sc:]
                    else:
                        h264_frame = b'\x00\x00\x00\x01' + full[4:]
                    try:
                        ffmpeg_proc.stdin.write(h264_frame)
                        ffmpeg_proc.stdin.flush()
                    except Exception:
                        break

            # Jika gerakan terdeteksi dan frame sudah cukup untuk segmen rekaman
            if motion_detected and len(collected_frames) >= (SEGMENT_DURATION_SEC * 8):
                print(f"[MOTION] 🚨 Segmen rekaman penuh ({len(collected_frames)} frame)")
                break

        is_running = False

    except Exception as e:
        print(f"[STREAM] Exception: {e}")
    finally:
        if ffmpeg_proc:
            try:
                ffmpeg_proc.stdin.close()
                ffmpeg_proc.terminate()
            except Exception:
                pass
        if s_stream:
            try:
                s_stream.close()
            except Exception:
                pass

    # Encode MP4 jika ada gerakan
    if motion_detected and len(collected_frames) >= 10:
        print(f"[ENCODE] 🎬 Encode {len(collected_frames)} frame ke MP4 (Motion score: {motion_score:.2f})...")
        mp4_buf = io.BytesIO()
        try:
            enc = subprocess.Popen([
                'ffmpeg', '-y',
                '-f', 'mjpeg', '-r', '10', '-i', '-',
                '-c:v', 'libx264', '-preset', 'veryfast',
                '-crf', '25', '-pix_fmt', 'yuv420p',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-t', str(SEGMENT_DURATION_SEC),
                '-f', 'mp4', '-'
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            def read_out():
                while True:
                    c = enc.stdout.read(65536)
                    if not c:
                        break
                    mp4_buf.write(c)

            t_enc = threading.Thread(target=read_out, daemon=True)
            t_enc.start()

            for frame in collected_frames:
                try:
                    enc.stdin.write(frame)
                except Exception:
                    break

            try:
                enc.stdin.close()
            except Exception:
                pass
            enc.wait(timeout=30)
            t_enc.join(timeout=5)

            mp4_data = mp4_buf.getvalue()
            if len(mp4_data) > 1024:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                today = datetime.date.today().strftime("%Y%m%d")
                pid = f"cctv_video/motion_{ts}"
                tags = f"motion_video,tgl_{today},parkiran_depan"
                print(f"[UPLOAD] 📦 Mengupload segmen MP4 ({len(mp4_data)//1024} KB) ke Cloudinary...")
                url = upload_video(mp4_data, public_id=pid, tags=tags)
                if url:
                    hls = get_hls_url(pid)
                    print(f"[UPLOAD] 🎥 HLS URL: {hls}")

                    # Kirim notifikasi peringatan gerakan interaktif ke Telegram
                    try:
                        wib = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
                        time_str = wib.strftime("%d %b %Y %H:%M:%S WIB")
                        caption = (
                            f"🚨 <b>PERINGATAN GERAKAN CCTV!</b>\n\n"
                            f"📍 <b>Lokasi:</b> Parkiran Depan (DID: {DEVICE_ID})\n"
                            f"⏰ <b>Waktu:</b> <code>{time_str}</code>\n"
                            f"📊 <b>Skor Gerakan:</b> <code>{motion_score:.1f}</code>\n"
                            f"🎬 <b>Durasi:</b> ~{SEGMENT_DURATION_SEC} detik\n"
                            f"📁 <b>Format:</b> MP4 H.264 (iOS & Android Compatible)\n\n"
                            f"Gunakan tombol di bawah untuk memutar rekaman langsung:"
                        )
                        trigger_pic = collected_frames[0] if collected_frames else first_snapshot_frame
                        if trigger_pic:
                            watermarked_trigger = apply_cctv_watermark(trigger_pic, motion_score)
                            send_telegram_alert(watermarked_trigger, caption, mp4_url=url, hls_url=hls)
                    except Exception as e:
                        print(f"[TELEGRAM] Error: {e}")

                    return True
        except Exception as e:
            print(f"[ENCODE] Error: {e}")
    else:
        print("[STREAM] ℹ️ Tidak ada gerakan mencolok selama sesi pemantauan.")
        # Kirim snapshot berkala ke Telegram jika mode 'all'
        if TELEGRAM_NOTIF_MODE in ("all", "periodic", "snapshot") and first_snapshot_frame:
            try:
                wib = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
                time_str = wib.strftime("%d %b %Y %H:%M:%S WIB")
                caption = (
                    f"📸 <b>Snapshot Berkala CCTV</b>\n\n"
                    f"📍 <b>Lokasi:</b> Parkiran Depan (DID: {DEVICE_ID})\n"
                    f"⏰ <b>Waktu:</b> <code>{time_str}</code>\n"
                    f"✅ <b>Kondisi:</b> Aman / Tenang (Tidak ada gerakan)\n\n"
                    f"🌐 Status live dapat dipantau di dashboard:"
                )
                send_telegram_alert(first_snapshot_frame, caption)
            except Exception as e:
                print(f"[TELEGRAM] Snapshot Error: {e}")

    return False

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    print(f"=== V380 Cloud CCTV Recorder | Mode: {mode} | {datetime.datetime.now().isoformat()} ===")

    if mode in ("auto", "record"):
        stream_and_record()
        build_and_upload_manifest()
    elif mode == "purge":
        purge_old_videos()
        build_and_upload_manifest()
    elif mode == "manifest":
        build_and_upload_manifest()

    print("=== Selesai ===")
