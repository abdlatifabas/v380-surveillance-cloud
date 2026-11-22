#!/usr/bin/env python3
"""
Video Recorder & HLS Streamer untuk V380 CCTV
Merekam segmen video MP4 (motion-triggered) dan upload ke Cloudinary sebagai video.
Juga memproduksi playlist HLS (.m3u8) agar bisa diputar langsung di browser
melalui Cloudflare Pages tanpa perlu server backend.
"""

import os
import io
import time
import datetime
import hashlib
import threading
import subprocess
import requests
from requests.auth import HTTPBasicAuth

CLOUD_NAME   = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
API_KEY      = os.environ.get("CLOUDINARY_API_KEY", "")
API_SECRET   = os.environ.get("CLOUDINARY_API_SECRET", "")
BASE_URL     = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}"
UPLOAD_URL   = f"{BASE_URL}/video/upload"

# Durasi setiap segmen rekaman (detik)
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION_SEC", 30))


def _signed_params(params: dict) -> dict:
    """Menghasilkan parameter bertanda tangan (signed) untuk upload Cloudinary."""
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params)) + API_SECRET
    sig = hashlib.sha1(to_sign.encode()).hexdigest()
    result = dict(params)
    result["api_key"] = API_KEY
    result["signature"] = sig
    return result


def upload_video_to_cloudinary(mp4_bytes: bytes, public_id: str, tags: str = "motion_video") -> str | None:
    """
    Upload file MP4 ke Cloudinary sebagai video.
    Cloudinary akan otomatis menyediakan streaming URL dan HLS playlist-nya.
    """
    t = int(time.time())
    params = {
        "public_id": public_id,
        "resource_type": "video",
        "tags": tags,
        "timestamp": str(t),
        "overwrite": "true",
        "eager": "sp_hd/m3u8",          # generate HLS playlist otomatis
        "eager_async": "true",
    }
    signed = _signed_params(params)
    files = {"file": ("recording.mp4", mp4_bytes, "video/mp4")}
    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/video/upload",
            data=signed, files=files, timeout=120
        )
        if resp.status_code == 200:
            data = resp.json()
            url = data.get("secure_url", "")
            print(f"[VIDEO-UPLOAD] ✅ Upload OK: {url}")
            return url
        else:
            print(f"[VIDEO-UPLOAD] ❌ Error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"[VIDEO-UPLOAD] ❌ Exception: {e}")
        return None


def upload_snapshot_to_cloudinary(jpg_bytes: bytes) -> str | None:
    """Upload snapshot JPG (overwrite cctv_latest) untuk dashboard Pages."""
    t = int(time.time())
    params = {
        "public_id": "cctv_latest",
        "overwrite": "true",
        "tags": "live_feed,v380",
        "timestamp": str(t),
    }
    signed = _signed_params(params)
    files = {"file": ("snapshot.jpg", jpg_bytes, "image/jpeg")}
    try:
        resp = requests.post(f"{BASE_URL}/image/upload", data=signed, files=files, timeout=12)
        if resp.status_code == 200:
            return resp.json().get("secure_url")
        return None
    except Exception:
        return None


def get_hls_url(public_id: str) -> str:
    """
    Cloudinary HLS streaming URL untuk video yang sudah diupload.
    Format: https://res.cloudinary.com/{cloud}/video/upload/sp_hd/{public_id}.m3u8
    """
    return f"https://res.cloudinary.com/{CLOUD_NAME}/video/upload/sp_hd/{public_id}.m3u8"


def list_recent_videos(max_results: int = 20) -> list:
    """Ambil daftar video rekaman gerakan terbaru dari Cloudinary."""
    try:
        url = f"{BASE_URL}/resources/video/tags/motion_video?max_results={max_results}&direction=desc"
        resp = requests.get(url, auth=HTTPBasicAuth(API_KEY, API_SECRET), timeout=10)
        if resp.status_code == 200:
            resources = resp.json().get("resources", [])
            result = []
            for r in resources:
                pid = r.get("public_id", "")
                result.append({
                    "public_id": pid,
                    "url": r.get("secure_url", ""),
                    "hls_url": get_hls_url(pid),
                    "created_at": r.get("created_at", ""),
                    "duration": r.get("duration", 0),
                    "bytes": r.get("bytes", 0),
                })
            return result
        return []
    except Exception as e:
        print(f"[VIDEO-LIST] Error: {e}")
        return []


def purge_old_videos(retention_days: int = 3, max_check_days: int = 30):
    """Hapus video rekaman yang lebih tua dari retention_days hari."""
    today = datetime.date.today()
    deleted = {}
    for i in range(retention_days + 1, retention_days + max_check_days):
        old_date = today - datetime.timedelta(days=i)
        tag = f"tgl_{old_date.strftime('%Y%m%d')}"
        try:
            resp = requests.delete(
                f"{BASE_URL}/resources/video/tags/{tag}",
                auth=HTTPBasicAuth(API_KEY, API_SECRET), timeout=10
            )
            if resp.status_code == 200:
                n = len(resp.json().get("deleted", {}))
                if n > 0:
                    deleted[tag] = n
                    print(f"[VIDEO-PURGE] ✅ Hapus {n} video lama: {tag}")
        except Exception:
            pass
    return deleted


class MotionVideoRecorder:
    """
    Merekam segmen video MP4 selama SEGMENT_DURATION detik saat motion terdeteksi,
    kemudian upload ke Cloudinary sebagai video (bukan gambar).
    """

    def __init__(self, segment_duration: int = SEGMENT_DURATION):
        self.segment_duration = segment_duration
        self.is_recording = False
        self.lock = threading.Lock()
        self._ffmpeg_proc = None
        self._record_thread = None
        self._segment_start = 0

    def start_segment(self, trigger_frame_bytes: bytes):
        """Mulai merekam segmen video baru jika belum dalam sesi rekaman."""
        with self.lock:
            if self.is_recording:
                return  # sudah merekam, skip
            self.is_recording = True

        t = threading.Thread(
            target=self._record_segment,
            args=(trigger_frame_bytes,),
            daemon=True
        )
        t.start()

    def _record_segment(self, trigger_frame_bytes: bytes):
        """Thread worker: pipe frame MJPEG ke ffmpeg, encode MP4, upload Cloudinary."""
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        today_str = datetime.date.today().strftime("%Y%m%d")
        public_id = f"cctv_video/motion_{ts_str}"
        tags = f"motion_video,tgl_{today_str},parkiran_depan"

        print(f"[RECORDER] 🎬 Mulai rekam segmen video {self.segment_duration}s -> {public_id}")
        mp4_buf = io.BytesIO()

        try:
            # FFmpeg: baca MJPEG dari stdin, output MP4 ke stdout
            proc = subprocess.Popen([
                "ffmpeg", "-y",
                "-f", "mjpeg", "-r", "10", "-i", "-",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "28", "-movflags", "frag_keyframe+empty_moov",
                "-t", str(self.segment_duration),
                "-f", "mp4", "-"
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            self._ffmpeg_proc = proc

            # Kirim frame trigger pertama ke stdin ffmpeg
            proc.stdin.write(trigger_frame_bytes)
            proc.stdin.flush()

            # Baca output mp4 secara async
            def read_output():
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    mp4_buf.write(chunk)

            reader_thread = threading.Thread(target=read_output, daemon=True)
            reader_thread.start()

            # Tunggu selama durasi segmen untuk mengumpulkan frame
            end_time = time.time() + self.segment_duration
            while time.time() < end_time:
                # frame baru akan dikirim dari luar via feed_frame()
                time.sleep(0.1)

            proc.stdin.close()
            proc.wait(timeout=10)
            reader_thread.join(timeout=5)

        except Exception as e:
            print(f"[RECORDER] ❌ FFmpeg error: {e}")
        finally:
            self._ffmpeg_proc = None
            with self.lock:
                self.is_recording = False

        mp4_data = mp4_buf.getvalue()
        if len(mp4_data) > 1024:
            print(f"[RECORDER] 📦 Segmen selesai, ukuran: {len(mp4_data)//1024} KB. Mengupload...")
            url = upload_video_to_cloudinary(mp4_data, public_id=public_id, tags=tags)
            if url:
                hls = get_hls_url(public_id)
                print(f"[RECORDER] 🎥 HLS URL: {hls}")
        else:
            print(f"[RECORDER] ⚠️ Data terlalu kecil ({len(mp4_data)} bytes), skip upload")

    def feed_frame(self, jpg_bytes: bytes):
        """Kirim frame JPG baru ke proses encoding yang sedang berjalan."""
        try:
            proc = self._ffmpeg_proc
            if proc and proc.stdin and not proc.stdin.closed:
                proc.stdin.write(jpg_bytes)
                proc.stdin.flush()
        except Exception:
            pass
