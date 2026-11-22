#!/usr/bin/env python3
"""
V380 CCTV 24/7 Motion Recording to Cloudinary & Live Web Hub
Includes:
- WAN Cloud Relay Authentication & H.264 Streamer
- Real-time Frame-Difference Motion Detection Engine
- Automated Cloudinary Motion Event Archiving
- Daily Auto-Purge of Old Files (Retention Policy)
- Web Surveillance Dashboard
"""

import os
import sys
import time
import socket
import struct
import hashlib
import base64
import threading
import subprocess
import requests
import concurrent.futures
from io import BytesIO
from PIL import Image, ImageChops, ImageStat
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from flask import Flask, Response, jsonify, render_template_string, request

from cloudinary_manager import CloudinaryManager
from r2_manager import R2Manager
from video_recorder import MotionVideoRecorder, upload_snapshot_to_cloudinary, list_recent_videos, purge_old_videos, get_hls_url

app = Flask(__name__)

# --- Configuration (Configurable via Environment Variables) ---
DEVICE_ID = int(os.environ.get("DEVICE_ID", "34417145"))
SHARED_USERNAME = os.environ.get("SHARED_USERNAME", "ldK2QjAs79")
RAW_PASSWORD = os.environ.get("RAW_PASSWORD", "")
SHARED_ACCOUNT_OWNER = os.environ.get("SHARED_ACCOUNT_OWNER", "7985700")
DISPATCH_URL = os.environ.get("DISPATCH_URL", "http://dispatch.av380.net:8001/api/v1/get_stream_server")
AES_SECRET_KEY = b"macrovideo+*#!^@"
PORT = int(os.environ.get("PORT", 8888))

# Motion Detection Parameters
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", 3.0))
MOTION_COOLDOWN_SEC = int(os.environ.get("MOTION_COOLDOWN_SEC", 25))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", 3))
SEGMENT_DURATION_SEC = int(os.environ.get("SEGMENT_DURATION_SEC", 30))

# Initialize Cloudinary Manager (image snapshots + legacy)
cld = CloudinaryManager(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    retention_days=RETENTION_DAYS
)

# Initialize Cloudflare R2 Manager (graceful fallback if not activated)
r2 = R2Manager(
    account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
    access_key_id=os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", ""),
    secret_access_key=os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", ""),
    bucket_name=os.environ.get("CLOUDFLARE_R2_BUCKET", "v380-cctv-recordings"),
    retention_days=RETENTION_DAYS
)

# Initialize Video Recorder (MP4 segments → Cloudinary HLS)
video_recorder = MotionVideoRecorder(segment_duration=SEGMENT_DURATION_SEC)

# Initialize Cloudinary Manager
cld = CloudinaryManager(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    retention_days=RETENTION_DAYS
)

# Initialize Cloudflare R2 Manager
r2 = R2Manager(
    account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
    access_key_id=os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", ""),
    secret_access_key=os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", ""),
    bucket_name=os.environ.get("CLOUDFLARE_R2_BUCKET", "v380-cctv-recordings"),
    retention_days=RETENTION_DAYS
)

def pkcs7_pad(data):
    padder = padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()

def encrypt_aes_ecb(data, key):
    padded = pkcs7_pad(data)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()

def change_dev_pwd(pwd):
    if len(pwd) <= 28:
        return pwd
    m = hashlib.md5(pwd.encode('utf-8')).hexdigest()
    m16 = m[8:24]
    return base64.b64encode(m16.encode('utf-8')).decode('utf-8')

class V380StreamManager:
    def __init__(self):
        self.device_id = DEVICE_ID
        self.username = SHARED_USERNAME
        self.chg_password = change_dev_pwd(RAW_PASSWORD)
        self.latest_frame = None
        self.prev_gray_thumb = None
        self.lock = threading.Lock()
        self.is_running = True
        self.connected_relay = None
        self.fps = 0
        self.total_frames = 0
        self.status = "Inisialisasi..."
        self.start_time = time.time()
        self.motion_events_count = 0
        self.last_motion_time = 0
        self.last_motion_diff = 0.0

        # Load initial frame if available
        for fpath in ['/root/.gemini/antigravity-cli/brain/759b05ef-210f-43e8-80e7-9e9edf3631ab/live_cctv_snapshot.jpg', '/tmp/live_test_fresh_1.jpg']:
            if os.path.exists(fpath):
                with open(fpath, 'rb') as f:
                    self.latest_frame = f.read()
                break

    def authenticate_relay(self, relay_ip):
        pkt = bytearray(256)
        struct.pack_into('<I', pkt, 0, 1167)
        struct.pack_into('<I', pkt, 4, 1072)
        pkt[8] = 3
        struct.pack_into('<I', pkt, 9, 11)
        struct.pack_into('<I', pkt, 13, self.device_id)
        domain = f'{self.device_id}.nvdvr.net'.encode('ascii')
        pkt[17:17+len(domain)] = domain
        struct.pack_into('<I', pkt, 67, 8800)
        user_b = self.username.encode('ascii')
        pkt[71:71+len(user_b)] = user_b

        rk = b'abcdef0123456789'
        pkt[103:119] = rk

        enc1 = encrypt_aes_ecb(self.chg_password.encode('utf-8'), AES_SECRET_KEY)
        enc2 = encrypt_aes_ecb(enc1, rk)
        pkt[119:119+len(enc2)] = enc2
        pkt[231] = 2

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(1.5)
        try:
            s.connect((relay_ip, 8800))
            s.sendall(pkt)
            resp = s.recv(256)
        except Exception:
            return None, None
        finally:
            s.close()

        if len(resp) >= 8:
            cmd, ret = struct.unpack_from('<II', resp, 0)
            if ret == 1001:
                ticket = struct.unpack_from('<I', resp, 13)[0]
                session = struct.unpack_from('<I', resp, 17)[0]
                return ticket, session
        return None, None

    def find_active_relay(self):
        platform = 10001
        t = int(time.time())
        sign_raw = f'dev_id={self.device_id}&platform={platform}&timestamp={t}hsdata2022'
        sign = hashlib.sha1(sign_raw.encode()).hexdigest()
        dispatch_ips = []
        try:
            resp = requests.post(
                DISPATCH_URL,
                json={'dev_id': self.device_id, 'platform': platform, 'timestamp': t, 'sign': sign},
                timeout=6.0
            )
            dispatch_ips = [item['ip'] for item in resp.json().get('data', [])]
        except Exception as e:
            print(f"[STREAMER] Dispatch error: {e}")

        # 1. Prioritize dispatch IPs
        for ip in dispatch_ips:
            ticket, session = self.authenticate_relay(ip)
            if ticket is not None and ticket != 0 and session != 0:
                return ip, ticket, session

        # 2. Fallbacks
        fallbacks = [
            '45.33.60.129', '138.2.233.2', '54.203.132.115', '50.116.11.56',
            '35.160.49.146', '45.33.37.15', '96.126.102.130', '192.9.128.89',
            '45.56.95.234', '172.232.185.236', '44.236.21.44'
        ]
        for ip in fallbacks:
            if ip in dispatch_ips:
                continue
            ticket, session = self.authenticate_relay(ip)
            if ticket is not None and ticket != 0 and session != 0:
                return ip, ticket, session

        return None, None, None

    def check_motion(self, current_frame_bytes):
        """Analyzes frame difference for motion detection."""
        try:
            img = Image.open(BytesIO(current_frame_bytes)).convert('L').resize((160, 90))
            if self.prev_gray_thumb is not None:
                diff = ImageChops.difference(self.prev_gray_thumb, img)
                stat = ImageStat.Stat(diff)
                diff_val = stat.mean[0]
                self.last_motion_diff = round(diff_val, 2)

                now = time.time()
                if diff_val >= MOTION_THRESHOLD and (now - self.last_motion_time) >= MOTION_COOLDOWN_SEC:
                    self.last_motion_time = now
                    self.motion_events_count += 1
                    print(f"[MOTION] 🚨 Gerakan Terdeteksi! (Diff: {diff_val:.2f}) → Merekam segmen VIDEO {SEGMENT_DURATION_SEC}s ke Cloudinary...")

                    # 1. Juga upload snapshot JPG (untuk Cloudflare Pages preview)
                    threading.Thread(
                        target=upload_snapshot_to_cloudinary,
                        args=(current_frame_bytes,),
                        daemon=True
                    ).start()

                    # 2. Mulai rekam segmen video MP4 → Cloudinary (HLS streaming)
                    video_recorder.start_segment(current_frame_bytes)

            # Kirim setiap frame ke video_recorder jika sedang merekam
            if video_recorder.is_recording:
                video_recorder.feed_frame(current_frame_bytes)

            self.prev_gray_thumb = img
        except Exception as e:
            pass

    def stream_loop(self):
        while self.is_running:
            self.status = "Menghubungi Cloud Relay..."
            relay_ip, ticket, session = self.find_active_relay()
            if not relay_ip:
                self.status = "Menunggu ketersediaan Cloud Relay..."
                time.sleep(4)
                continue

            self.connected_relay = relay_ip
            self.status = f"Terhubung ke Relay ({relay_ip})"

            s_stream = None
            ffmpeg_proc = None
            try:
                s_stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s_stream.settimeout(15.0)
                s_stream.connect((relay_ip, 8800))

                # Cmd 301 (Stream Login)
                cmd301 = bytearray(256)
                struct.pack_into('<I', cmd301, 0, 301)
                struct.pack_into('<I', cmd301, 4, 1072)
                domain = f'{self.device_id}.nvdvr.net'.encode('ascii')
                cmd301[8:8+len(domain)] = domain
                struct.pack_into('<I', cmd301, 58, 8800)
                struct.pack_into('<I', cmd301, 62, self.device_id)
                struct.pack_into('<I', cmd301, 66, ticket)
                struct.pack_into('<I', cmd301, 70, session)
                struct.pack_into('<I', cmd301, 74, 1) # HD Quality
                cmd301[78] = 20
                struct.pack_into('<I', cmd301, 79, 1)

                s_stream.sendall(cmd301)
                resp401 = s_stream.recv(32)

                # Launch FFmpeg decoder pipeline
                ffmpeg_proc = subprocess.Popen([
                    'ffmpeg', '-y', '-f', 'h264', '-i', '-',
                    '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '3', '-'
                ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

                # Thread to continuously read JPEGs from FFmpeg stdout
                def read_jpegs(proc):
                    buf = bytearray()
                    frame_times = []
                    motion_check_counter = 0
                    while self.is_running and proc.poll() is None:
                        try:
                            chunk = proc.stdout.read(8192)
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
                                with self.lock:
                                    self.latest_frame = frame
                                    self.total_frames += 1

                                now = time.time()
                                frame_times.append(now)
                                frame_times = [t for t in frame_times if now - t <= 2.0]
                                self.fps = round(len(frame_times) / 2.0, 1)

                                # Motion check every 4 frames (~4 times per second)
                                motion_check_counter += 1
                                if motion_check_counter % 4 == 0:
                                    self.check_motion(frame)

                        except Exception:
                            break

                t_jpeg = threading.Thread(target=read_jpegs, args=(ffmpeg_proc,), daemon=True)
                t_jpeg.start()

                # Send Cmd 303 (Start Stream)
                cmd303 = bytearray(256)
                struct.pack_into('<I', cmd303, 0, 303)
                struct.pack_into('<H', cmd303, 4, 0x3001)
                s_stream.sendall(cmd303)
                self.status = "Streaming Aktif (HD 1280x720)"

                # Stream packet reassembly
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

                while self.is_running:
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
                            if sc >= 0:
                                nal_data = full[sc:]
                                try:
                                    ffmpeg_proc.stdin.write(nal_data)
                                    ffmpeg_proc.stdin.flush()
                                except (BrokenPipeError, OSError):
                                    break

            except Exception as e:
                print(f"[STREAMER] Exception: {e}")
            finally:
                # Graceful shutdown
                try:
                    shutdown_pkt = bytearray(80)
                    struct.pack_into('<I', shutdown_pkt, 0, 199056)
                    s_stream.sendall(shutdown_pkt)
                except:
                    pass

                if ffmpeg_proc:
                    try:
                        ffmpeg_proc.stdin.close()
                        ffmpeg_proc.terminate()
                        ffmpeg_proc.wait(timeout=1)
                    except:
                        pass
                if s_stream:
                    try:
                        s_stream.close()
                    except:
                        pass
                time.sleep(2)

streamer = V380StreamManager()

# Background scheduler for daily retention purge
def auto_purge_daemon():
    while True:
        try:
            cld.purge_old_records()
            purge_old_videos(retention_days=RETENTION_DAYS)  # juga hapus video lama
            if r2.is_enabled:
                r2.purge_old_records()
        except Exception as e:
            print(f"[PURGE] Exception: {e}")
        # Run check every 12 hours
        time.sleep(12 * 3600)

@app.route('/')
def index():
    html = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>V380 Smart Motion CCTV - Parkiran Depan</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0a0f1d;
            --bg-secondary: #131c31;
            --accent-green: #10b981;
            --accent-blue: #3b82f6;
            --accent-red: #ef4444;
            --accent-purple: #8b5cf6;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --border-color: #24304f;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-primary);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        header {
            background-color: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            padding: 1rem 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
        }

        .brand-title {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            font-size: 1.2rem;
            font-weight: 700;
        }

        .live-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            background-color: rgba(239, 68, 68, 0.2);
            color: var(--accent-red);
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.25rem 0.6rem;
            border-radius: 9999px;
            text-transform: uppercase;
        }

        .live-dot {
            width: 8px; height: 8px;
            background-color: var(--accent-red);
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); opacity: 0.8; }
            50% { transform: scale(1.3); opacity: 1; }
            100% { transform: scale(0.95); opacity: 0.8; }
        }

        .main-container {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 1.5rem;
            max-width: 1400px;
            margin: 0 auto;
            width: 100%;
            gap: 1.5rem;
        }

        .player-wrapper {
            background-color: #000;
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            position: relative;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
            aspect-ratio: 16 / 9;
            display: flex;
            justify-content: center;
            align-items: center;
        }

        .stream-view {
            width: 100%; height: 100%;
            object-fit: contain;
            display: block;
        }

        .overlay-hud {
            position: absolute;
            top: 1rem; left: 1rem; right: 1rem;
            display: flex; justify-content: space-between;
            pointer-events: none;
        }

        .hud-tag {
            background: rgba(10, 15, 29, 0.75);
            backdrop-filter: blur(6px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            padding: 0.4rem 0.8rem;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .hud-bottom {
            position: absolute;
            bottom: 1rem; left: 1rem; right: 1rem;
            display: flex; justify-content: space-between;
            align-items: flex-end;
            pointer-events: none;
        }

        .grid-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1.25rem;
        }

        .card {
            background-color: var(--bg-secondary);
            border-radius: 10px;
            border: 1px solid var(--border-color);
            padding: 1.25rem;
        }

        .card-header {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .card-value {
            font-size: 1.25rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
        }

        .card-subtext {
            font-size: 0.8rem;
            color: var(--text-muted);
        }

        .btn-group {
            display: flex;
            gap: 0.75rem;
            margin-top: 1rem;
            flex-wrap: wrap;
        }

        .btn {
            background-color: var(--accent-blue);
            color: white;
            border: none;
            padding: 0.6rem 1.2rem;
            border-radius: 6px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.9rem;
        }

        .btn:hover {
            opacity: 0.9;
            transform: translateY(-1px);
        }

        .btn-purple { background-color: var(--accent-purple); }
        .btn-secondary { background-color: var(--border-color); color: var(--text-main); }
        .btn-secondary:hover { background-color: #2d3b5e; }

        .timeline-section {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 1.25rem;
        }

        .gallery-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 1rem;
            margin-top: 1rem;
        }

        .event-card {
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.2s;
            text-decoration: none;
            color: var(--text-main);
            display: block;
        }

        .event-card:hover {
            transform: scale(1.02);
            border-color: var(--accent-purple);
        }

        .event-card img {
            width: 100%;
            height: 120px;
            object-fit: cover;
            display: block;
        }

        .event-card-body {
            padding: 0.6rem;
            font-size: 0.75rem;
        }

        footer {
            padding: 1rem;
            text-align: center;
            font-size: 0.8rem;
            color: var(--text-muted);
            border-top: 1px solid var(--border-color);
            margin-top: auto;
        }
    </style>
</head>
<body>
    <header>
        <div class="brand-title">
            <span>📹 V380 Smart Surveillance</span>
            <span class="live-badge"><span class="live-dot"></span> LIVE FEED</span>
        </div>
        <div style="font-size: 0.85rem; color: var(--text-muted);">
            Cloud Storage: <strong>Cloudinary (kngcbgye)</strong> • Auto-Purge: <strong>{{ retention_days }} Hari</strong>
        </div>
    </header>

    <div class="main-container">
        <div class="player-wrapper" id="playerContainer">
            <img src="/video_feed" class="stream-view" id="cctvStream" alt="Feed Kamera CCTV">
            
            <div class="overlay-hud">
                <div class="hud-tag">
                    <span style="color: var(--accent-green);">●</span>
                    <span>Kamera Parkiran Depan</span>
                </div>
                <div class="hud-tag">
                    <span>DID: <strong>34417145</strong></span>
                    <span style="color: var(--border-color);">|</span>
                    <span id="hudFps">25.0 FPS</span>
                </div>
            </div>

            <div class="hud-bottom">
                <div class="hud-tag">
                    <span>Status: <strong id="hudStatus" style="color: var(--accent-green);">ONLINE</strong></span>
                    <span style="color: var(--border-color);">|</span>
                    <span id="hudRelay">Relay: Terhubung</span>
                </div>
                <div class="hud-tag" style="pointer-events: auto;">
                    <button onclick="toggleFullscreen()" style="background:none; border:none; color:white; cursor:pointer; font-weight:600;">
                        ⛶ Fullscreen
                    </button>
                </div>
            </div>
        </div>

        <div class="grid-info">
            <div class="card">
                <div class="card-header">Deteksi Gerakan (Motion Detection)</div>
                <div class="card-value" style="color: var(--accent-purple);" id="cardMotionEvents">0 Kejadian</div>
                <div class="card-subtext">Sensitivitas Delta: <strong>{{ motion_threshold }}</strong> • Selisih Terakhir: <span id="cardLastDiff">0.0</span></div>
                <div class="card-subtext" style="margin-top:0.25rem;">Otomatis simpan foto HD ke Cloudinary saat ada aktivitas</div>
            </div>

            <div class="card">
                <div class="card-header">Cloudinary Auto-Purge Storage</div>
                <div class="card-value" style="color: var(--accent-green);">Hemat Kuota Aktif</div>
                <div class="card-subtext">Retensi: <strong>{{ retention_days }} Hari</strong> (File lebih tua otomatis dihapus)</div>
                <div class="card-subtext" style="margin-top:0.25rem;">Slot Utama: <code>cctv_latest.jpg</code> (Ditiban berkala)</div>
            </div>

            <div class="card">
                <div class="card-header">Koneksi & Output Video</div>
                <div class="card-value">1280 × 720 (HD)</div>
                <div class="card-subtext">Format: MJPEG over HTTP • WAN Relay Port 8800</div>
                <div class="card-subtext" style="margin-top:0.25rem;">Throughput: <span id="cardFps">25 FPS</span></div>
            </div>
        </div>

        <div class="card">
            <div class="card-header">Kontrol Cepat</div>
            <div class="btn-group">
                <a href="/snapshot.jpg" target="_blank" class="btn">📸 Unduh Snapshot HD</a>
                <a href="https://res.cloudinary.com/{{ cloud_name }}/image/upload/cctv_latest.jpg" target="_blank" class="btn btn-purple">☁️ Buka cctv_latest di Cloudinary</a>
                <button onclick="reloadStream()" class="btn btn-secondary">🔄 Muat Ulang Aliran Video</button>
                <button onclick="triggerPurge()" class="btn btn-secondary">🧹 Bersihkan File Lama Sekarang</button>
            </div>
        </div>

        <div class="timeline-section">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h3 style="font-size: 1rem; font-weight: 700;">🚨 Riwayat Rekaman Gerakan di Cloudinary</h3>
                <button onclick="loadMotionEvents()" class="btn btn-secondary" style="padding: 0.35rem 0.75rem; font-size: 0.8rem;">⚡ Refresh Riwayat</button>
            </div>
            <div class="gallery-grid" id="motionGallery">
                <div style="color: var(--text-muted); font-size: 0.85rem;">Memuat daftar rekaman gerakan dari Cloudinary...</div>
            </div>
        </div>
    </div>

    <footer>
        V380 Cloudinary Motion Surveillance & Auto-Purge • Tanpa Server / VPS Berbayar
    </footer>

    <script>
        function toggleFullscreen() {
            const container = document.getElementById('playerContainer');
            if (!document.fullscreenElement) {
                container.requestFullscreen().catch(err => alert(err.message));
            } else {
                document.exitFullscreen();
            }
        }

        let isOnline = true;
        function reloadStream() {
            const img = document.getElementById('cctvStream');
            img.src = '/video_feed?t=' + new Date().getTime();
        }

        const streamImg = document.getElementById('cctvStream');
        streamImg.onerror = () => setTimeout(reloadStream, 3000);

        setInterval(() => {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('hudFps').innerText = data.fps + ' FPS';
                    document.getElementById('cardFps').innerText = data.fps + ' FPS (' + data.total_frames + ' frames)';
                    document.getElementById('hudStatus').innerText = data.status;
                    document.getElementById('cardMotionEvents').innerText = data.motion_events + ' Kejadian';
                    document.getElementById('cardLastDiff').innerText = data.last_diff;
                    if (data.relay) {
                        document.getElementById('hudRelay').innerText = 'Relay: ' + data.relay;
                    }

                    const currentlyActive = data.status.includes('Streaming Aktif');
                    if (currentlyActive && !isOnline) {
                        reloadStream();
                    }
                    isOnline = currentlyActive;
                })
                .catch(() => {});
        }, 1500);

        function loadMotionEvents() {
            fetch('/api/motion_events')
                .then(r => r.json())
                .then(events => {
                    const gallery = document.getElementById('motionGallery');
                    if (!events || events.length === 0) {
                        gallery.innerHTML = '<div style="color: var(--text-muted); font-size: 0.85rem;">Belum ada kejadian gerakan yang tersimpan di Cloudinary hari ini.</div>';
                        return;
                    }
                    gallery.innerHTML = events.map(e => `
                        <a href="${e.url}" target="_blank" class="event-card">
                            <img src="${e.url}" alt="Motion Event">
                            <div class="event-card-body">
                                <div><strong>ID:</strong> ${e.public_id.split('/').pop()}</div>
                                <div style="color: var(--text-muted); margin-top: 2px;">${e.created_at || 'Baru saja'}</div>
                            </div>
                        </a>
                    `).join('');
                })
                .catch(() => {});
        }

        function triggerPurge() {
            if (confirm('Jalankan pembersihan rekaman lama (> {{ retention_days }} hari) di Cloudinary sekarang?')) {
                fetch('/api/purge_now', { method: 'POST' })
                    .then(r => r.json())
                    .then(res => {
                        alert('Pembersihan selesai!\nDetail: ' + JSON.stringify(res.deleted_summary));
                        loadMotionEvents();
                    })
                    .catch(e => alert('Error: ' + e));
            }
        }

        window.onload = () => {
            loadMotionEvents();
        };
    </script>
</body>
</html>
    """
    return render_template_string(
        html,
        cloud_name=cld.cloud_name,
        retention_days=cld.retention_days,
        motion_threshold=MOTION_THRESHOLD
    )

@app.route('/video_feed')
def video_feed():
    def generate():
        last_sent = None
        while True:
            frame = None
            with streamer.lock:
                frame = streamer.latest_frame
            if frame and frame != last_sent:
                last_sent = frame
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.04)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snapshot.jpg')
def snapshot():
    with streamer.lock:
        frame = streamer.latest_frame
    if frame:
        return Response(frame, mimetype='image/jpeg')
    return "No frame available", 404

@app.route('/api/status')
def api_status():
    return jsonify({
        "device_id": streamer.device_id,
        "username": streamer.username,
        "relay": streamer.connected_relay,
        "status": streamer.status,
        "fps": streamer.fps,
        "total_frames": streamer.total_frames,
        "uptime_sec": int(time.time() - streamer.start_time),
        "motion_events": streamer.motion_events_count,
        "last_diff": streamer.last_motion_diff,
        "retention_days": cld.retention_days,
        "cloudinary_cloud": cld.cloud_name,
        "r2_enabled": r2.is_enabled,
        "cloudflare_pages_url": "https://v380-surveillance-cloud.pages.dev"
    })

@app.route('/api/motion_events')
def api_motion_events():
    # Ambil video recordings (MP4/HLS) + fallback ke image events
    videos = list_recent_videos(max_results=20)
    images = cld.list_recent_motion_events(max_results=20)
    return jsonify({"videos": videos, "images": images})

@app.route('/api/purge_now', methods=['POST'])
def api_purge_now():
    deleted_img = cld.purge_old_records()
    deleted_vid = purge_old_videos(retention_days=RETENTION_DAYS)
    if r2.is_enabled:
        r2.purge_old_records()
    return jsonify({"success": True, "deleted_images": deleted_img, "deleted_videos": deleted_vid})

@app.route('/api/sync_cloud_snapshot', methods=['POST', 'GET'])
def api_sync_cloud_snapshot():
    """On-demand sync of current live frame to Cloudinary cctv_latest.jpg"""
    with streamer.lock:
        frame = streamer.latest_frame
    if not frame:
        return jsonify({"error": "No frame available"}), 404
    url = upload_snapshot_to_cloudinary(frame)
    return jsonify({"success": True, "cloudinary_url": url})

def periodic_snapshot_sync():
    """Every 60s: push current live frame to Cloudinary (keeps Pages dashboard updated)"""
    while True:
        try:
            with streamer.lock:
                frame = streamer.latest_frame
            if frame and streamer.fps > 0:
                upload_snapshot_to_cloudinary(frame)
        except Exception:
            pass
        time.sleep(60)

if __name__ == '__main__':
    t_worker = threading.Thread(target=streamer.stream_loop, daemon=True)
    t_worker.start()

    t_purge = threading.Thread(target=auto_purge_daemon, daemon=True)
    t_purge.start()

    t_sync = threading.Thread(target=periodic_snapshot_sync, daemon=True)
    t_sync.start()

    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
