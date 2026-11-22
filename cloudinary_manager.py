#!/usr/bin/env python3
"""
Cloudinary Storage & Auto-Purge Manager for V380 CCTV
Manages real-time snapshot overwrites, motion-triggered event archives,
and automated retention cleanup (purging files older than N days).
"""

import os
import time
import datetime
import hashlib
import requests
from requests.auth import HTTPBasicAuth

class CloudinaryManager:
    def __init__(self, cloud_name=None, api_key=None, api_secret=None, retention_days=3):
        self.cloud_name = cloud_name or os.environ.get("CLOUDINARY_CLOUD_NAME", "kngcbgye")
        self.api_key = api_key or os.environ.get("CLOUDINARY_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("CLOUDINARY_API_SECRET", "")
        self.retention_days = int(retention_days or os.environ.get("RETENTION_DAYS", 3))
        self.base_url = f"https://api.cloudinary.com/v1_1/{self.cloud_name}"
        self.last_purge_date = None

    def upload_image(self, img_bytes, public_id="cctv_latest", tags="live_feed,v380", overwrite=True):
        """Uploads image bytes with signed authentication."""
        t = int(time.time())
        params = {
            "overwrite": "true" if overwrite else "false",
            "public_id": public_id,
            "tags": tags,
            "timestamp": str(t)
        }
        to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params.keys())) + self.api_secret
        sig = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()

        data = {
            "api_key": self.api_key,
            "timestamp": t,
            "public_id": public_id,
            "overwrite": "true" if overwrite else "false",
            "tags": tags,
            "signature": sig
        }
        files = {"file": ("snapshot.jpg", img_bytes, "image/jpeg")}

        try:
            resp = requests.post(f"{self.base_url}/image/upload", data=data, files=files, timeout=12)
            if resp.status_code == 200:
                return resp.json().get("secure_url")
            else:
                print(f"[CLOUDINARY] Upload Error ({resp.status_code}): {resp.text}")
                return None
        except Exception as e:
            print(f"[CLOUDINARY] Upload Exception: {e}")
            return None

    def upload_motion_event(self, img_bytes):
        """Uploads motion event frame tagged with current date for auto-purge."""
        today_str = datetime.date.today().strftime("%Y%m%d")
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        public_id = f"cctv_motion/motion_{ts_str}"
        tags = f"motion_event,tgl_{today_str},parkiran_depan"

        # 1. Upload motion event file
        url = self.upload_image(img_bytes, public_id=public_id, tags=tags, overwrite=False)

        # 2. Also update cctv_latest (overwriting previous latest)
        self.upload_image(img_bytes, public_id="cctv_latest", tags="live_feed,v380", overwrite=True)

        return url

    def list_recent_motion_events(self, max_results=20):
        """Retrieves list of recent motion events from Cloudinary."""
        try:
            url = f"{self.base_url}/resources/image/tags/motion_event?max_results={max_results}"
            resp = requests.get(url, auth=HTTPBasicAuth(self.api_key, self.api_secret), timeout=8)
            if resp.status_code == 200:
                resources = resp.json().get("resources", [])
                events = []
                for res in resources:
                    events.append({
                        "public_id": res.get("public_id"),
                        "url": res.get("secure_url"),
                        "created_at": res.get("created_at"),
                        "bytes": res.get("bytes")
                    })
                return events
            return []
        except Exception as e:
            print(f"[CLOUDINARY] List Events Exception: {e}")
            return []

    def purge_old_records(self, max_past_check_days=30):
        """
        Deletes all motion events older than retention_days (e.g. older than 3 days).
        Uses daily date tags (tgl_YYYYMMDD) for 1-call batch deletion.
        """
        today = datetime.date.today()
        cutoff_date = today - datetime.timedelta(days=self.retention_days)
        deleted_summary = {}

        print(f"[PURGE] Menjalankan pembersihan harian. Batas simpan: {self.retention_days} hari (sebelum {cutoff_date})")

        # Check past date tags from cutoff date backward
        for i in range(self.retention_days + 1, self.retention_days + max_past_check_days):
            old_date = today - datetime.timedelta(days=i)
            tag_name = f"tgl_{old_date.strftime('%Y%m%d')}"
            try:
                url = f"{self.base_url}/resources/image/tags/{tag_name}"
                resp = requests.delete(url, auth=HTTPBasicAuth(self.api_key, self.api_secret), timeout=10)
                if resp.status_code == 200:
                    del_data = resp.json()
                    del_count = len(del_data.get("deleted", {}))
                    if del_count > 0:
                        deleted_summary[tag_name] = del_count
                        print(f"[PURGE] ✅ Berhasil menghapus {del_count} rekaman lama pada tag {tag_name}")
            except Exception as e:
                print(f"[PURGE] Gagal cek/hapus tag {tag_name}: {e}")

        self.last_purge_date = today
        return deleted_summary
