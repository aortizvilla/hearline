"""Audio storage: local in dev, S3 in production (a fake S3 client, no network)."""

from pathlib import Path

from app.config import get_settings
from app.providers.storage import LocalStorage, S3Storage


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str]] = []

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict) -> None:  # noqa: N803
        self.objects[key] = Path(filename).read_bytes()
        self.uploads.append((key, ExtraArgs["ContentType"]))

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[key])

    def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:  # noqa: N803
        return f"https://{Params['Bucket']}.s3/{Params['Key']}?expires={ExpiresIn}"


def test_local_storage_serves_from_the_audio_dir() -> None:
    assert LocalStorage().url("users/1/episodes/1.mp3") == "/audio/users/1/episodes/1.mp3"


def test_s3_uploads_downloads_and_signs() -> None:
    s3 = FakeS3()
    storage = S3Storage("hearline-audio", prefix="prod", url_ttl_sec=600, client=s3)
    path = get_settings().audio_dir / "clips" / "polly-neural" / "story1.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ID3audio")

    storage.save("clips/polly-neural/story1.mp3")
    assert s3.uploads == [("prod/clips/polly-neural/story1.mp3", "audio/mpeg")]

    path.unlink()  # another worker machine: not on its disk yet
    assert storage.fetch("clips/polly-neural/story1.mp3").read_bytes() == b"ID3audio"
    assert storage.url("users/1/episodes/1.mp3") == (
        "https://hearline-audio.s3/prod/users/1/episodes/1.mp3?expires=600")


def test_cloudfront_links_are_signed() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    storage = S3Storage("hearline-audio", cloudfront_domain="cdn.example.com",
                        cloudfront_key_id="K2ABC", cloudfront_private_key=pem, client=FakeS3())
    url = storage.url("users/1/episodes/1.mp3")
    assert url.startswith("https://cdn.example.com/users/1/episodes/1.mp3?Expires=")
    assert "Signature=" in url and "Key-Pair-Id=K2ABC" in url


def test_cloudfront_key_may_arrive_on_one_line() -> None:
    """Production env files hold the PEM with literal \\n (hearlineCDK/scripts/put_secrets.py)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.providers.storage import S3Storage

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    store = S3Storage("bucket", "audio", cloudfront_domain="hearline.example",
                      cloudfront_key_id="K123", cloudfront_private_key=pem.replace("\n", "\\n"),
                      client=object())
    url = store.url("users/1/episodes/2.mp3")
    assert url.startswith("https://hearline.example/audio/users/1/episodes/2.mp3?")
    assert "Signature=" in url and "Key-Pair-Id=K123" in url
