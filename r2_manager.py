#!/usr/bin/env python3
"""
Cloudflare R2 Storage & Auto-Purge Manager for V380 CCTV
Provides zero-egress S3-compatible cloud storage on Cloudflare.
"""

import os
import datetime
try:
    import boto3
    from botocore.config import Config
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

class R2Manager:
    def __init__(self, account_id=None, access_key_id=None, secret_access_key=None, bucket_name="v380-cctv-recordings", retention_days=3):
        self.account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.access_key_id = access_key_id or os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
        self.secret_access_key = secret_access_key or os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")
        self.bucket_name = bucket_name or os.environ.get("CLOUDFLARE_R2_BUCKET", "v380-cctv-recordings")
        self.retention_days = int(retention_days or os.environ.get("RETENTION_DAYS", 3))
        self.endpoint_url = f"https://{self.account_id}.r2.cloudflarestorage.com"
        
        self.s3_client = None
        self.is_enabled = False
        self._init_client()

    def _init_client(self):
        if not BOTO3_AVAILABLE:
            print("[R2] Boto3 library tidak ditemukan. Lewati inisialisasi R2.")
            return

        if not (self.account_id and self.access_key_id and self.secret_access_key):
            print("[R2] Kredensial R2 tidak lengkap.")
            return

        try:
            self.s3_client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                config=Config(signature_version="s3v4"),
                region_name="auto"
            )
            # Quick check if connection works
            self.s3_client.list_buckets()
            self.is_enabled = True
            print("[R2] ✅ Cloudflare R2 berhasil terhubung dan siap digunakan!")
        except Exception as e:
            # R2 requires activation via dash.cloudflare.com
            self.is_enabled = False
            print(f"[R2] Cloudflare R2 belum aktif di dashboard: {e}")

    def upload_image(self, img_bytes, object_key="cctv_latest.jpg", content_type="image/jpeg"):
        if not self.is_enabled or not self.s3_client:
            return None
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=object_key,
                Body=img_bytes,
                ContentType=content_type
            )
            return f"{self.endpoint_url}/{self.bucket_name}/{object_key}"
        except Exception as e:
            print(f"[R2] Upload Error ({object_key}): {e}")
            return None

    def upload_motion_event(self, img_bytes):
        if not self.is_enabled or not self.s3_client:
            return None
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        key = f"cctv_motion/motion_{ts_str}.jpg"
        
        # 1. Upload motion event
        url = self.upload_image(img_bytes, object_key=key)
        # 2. Update latest
        self.upload_image(img_bytes, object_key="cctv_latest.jpg")
        return url

    def purge_old_records(self):
        if not self.is_enabled or not self.s3_client:
            return
        cutoff_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=self.retention_days)
        try:
            paginator = self.s3_client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix="cctv_motion/")
            for page in pages:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        if obj["LastModified"] < cutoff_date:
                            self.s3_client.delete_object(Bucket=self.bucket_name, Key=obj["Key"])
                            print(f"[R2 PURGE] Deleted expired object: {obj['Key']}")
        except Exception as e:
            print(f"[R2 PURGE] Error: {e}")
