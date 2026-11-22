"""Where audio files live: the local disk (dev) or S3 (production), behind one interface.

Paths are always relative to AUDIO_DIR (clips/..., phrases/..., users/<id>/episodes/...) and
that is what the database stores. Workers always write to AUDIO_DIR first; it doubles as a
local cache in S3 mode.

    save(path)   publish a file just written under AUDIO_DIR
    fetch(path)  a local copy to read (S3: downloaded once into AUDIO_DIR)
    delete(path) remove it (account deletion)
    url(path)    what a listener's player gets (S3: an expiring signed link, so an episode
                 can only be played through the API that checked who is asking)

AUDIO_STORAGE=s3 also needs AUDIO_BUCKET. With CLOUDFRONT_DOMAIN + CLOUDFRONT_KEY_ID +
CLOUDFRONT_PRIVATE_KEY, links go through CloudFront (cheaper egress, 1 TB/month free) instead
of straight to S3.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.database.models import utcnow

CONTENT_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}


def local_path(rel_path: str) -> Path:
    return get_settings().audio_dir / rel_path


class AudioStorage(ABC):
    name = "base"

    @abstractmethod
    def save(self, rel_path: str) -> None: ...

    @abstractmethod
    def fetch(self, rel_path: str) -> Path: ...

    @abstractmethod
    def url(self, rel_path: str) -> str: ...

    def delete(self, rel_path: str) -> None:
        """Remove a file everywhere it is kept (account deletion). Missing files are fine."""
        local_path(rel_path).unlink(missing_ok=True)


class LocalStorage(AudioStorage):
    """Files stay in AUDIO_DIR; the API serves them at /audio (dev only: no access check)."""

    name = "local"

    def save(self, rel_path: str) -> None:
        pass

    def fetch(self, rel_path: str) -> Path:
        return local_path(rel_path)

    def url(self, rel_path: str) -> str:
        return f"/audio/{rel_path}"


class S3Storage(AudioStorage):
    name = "s3"

    def __init__(self, bucket: str, prefix: str = "", region: str = "", url_ttl_sec: int = 86400,
                 cloudfront_domain: str = "", cloudfront_key_id: str = "",
                 cloudfront_private_key: str = "", client: Any = None) -> None:
        if not bucket:
            raise RuntimeError("AUDIO_STORAGE=s3 needs AUDIO_BUCKET")
        self.bucket, self.prefix, self.ttl = bucket, prefix.strip("/"), url_ttl_sec
        if client is None:
            import boto3

            s = get_settings()
            client = boto3.client("s3", region_name=region or None,
                                  aws_access_key_id=s.aws_access_key_id or None,
                                  aws_secret_access_key=s.aws_secret_access_key or None)
        self.client = client
        self.cdn = cloudfront_domain.rstrip("/")
        self.signer = None
        if self.cdn and cloudfront_key_id and cloudfront_private_key:
            from botocore.signers import CloudFrontSigner
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            # env files hold one line per value: the PEM may arrive with literal "\n"
            pem = cloudfront_private_key.replace("\\n", "\n")
            key = serialization.load_pem_private_key(pem.encode(), None)
            self.signer = CloudFrontSigner(
                cloudfront_key_id,
                lambda msg: key.sign(msg, padding.PKCS1v15(), hashes.SHA1()))  # type: ignore[union-attr,call-arg,arg-type]  # noqa: E501

    def key(self, rel_path: str) -> str:
        return f"{self.prefix}/{rel_path}" if self.prefix else rel_path

    def save(self, rel_path: str) -> None:
        content_type = CONTENT_TYPES.get(Path(rel_path).suffix.lower(), "application/octet-stream")
        self.client.upload_file(str(local_path(rel_path)), self.bucket, self.key(rel_path),
                                ExtraArgs={"ContentType": content_type})

    def fetch(self, rel_path: str) -> Path:
        path = local_path(rel_path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, self.key(rel_path), str(path))
        return path

    def delete(self, rel_path: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.key(rel_path))
        local_path(rel_path).unlink(missing_ok=True)   # and the local cache

    def url(self, rel_path: str) -> str:
        if self.signer is not None:
            return self.signer.generate_presigned_url(
                f"https://{self.cdn}/{self.key(rel_path)}",
                date_less_than=utcnow() + timedelta(seconds=self.ttl))
        return str(self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self.key(rel_path)},
            ExpiresIn=self.ttl))


@lru_cache
def get_storage() -> AudioStorage:
    s = get_settings()
    if s.audio_storage == "s3":
        return S3Storage(s.audio_bucket, s.audio_prefix, s.aws_region, s.audio_url_ttl_sec,
                         s.cloudfront_domain, s.cloudfront_key_id, s.cloudfront_private_key)
    if s.audio_storage in ("", "local"):
        return LocalStorage()
    raise RuntimeError(f"Unknown AUDIO_STORAGE {s.audio_storage!r} (local | s3)")
