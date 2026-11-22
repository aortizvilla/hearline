"""Write the server's secrets to SSM Parameter Store (/hearline/secrets, SecureString).

Reads API keys and model names from hearline/backend/.env, adds the CloudFront signing key and the
origin secret from hearlineCDK/.secrets/, and keeps the Postgres password already stored (a new
one would lock the server out of its own database). Run by deploy.sh; nothing is printed.

    hearline/backend/.venv/bin/python -I hearlineCDK/scripts/put_secrets.py <region>
"""

import secrets
import sys
from pathlib import Path

import boto3
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
NAME = "/hearline/secrets"
FROM_ENV = ("LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RESEARCHER_MODEL",
            "WRITER_MODEL", "SMALL_MODEL", "TTS_PROVIDER", "ELEVENLABS_API_KEY",
            "ALERT_WEBHOOK_URL", "HTTP_USER_AGENT", "CLERK_WEBHOOK_SECRET")


def main(region: str) -> None:
    ssm = boto3.client("ssm", region_name=region)
    env = dotenv_values(ROOT / "hearline" / "backend" / ".env")
    values = {k: env[k] for k in FROM_ENV if env.get(k)}
    try:
        old = ssm.get_parameter(Name=NAME, WithDecryption=True)["Parameter"]["Value"]
        current = dict(line.split("=", 1) for line in old.splitlines() if "=" in line)
    except ssm.exceptions.ParameterNotFound:
        current = {}
    values["POSTGRES_PASSWORD"] = current.get("POSTGRES_PASSWORD") or secrets.token_hex(24)
    keys = ROOT / "hearlineCDK" / ".secrets"
    values["ORIGIN_SECRET"] = (keys / "origin-secret").read_text().strip()
    values["CLOUDFRONT_PRIVATE_KEY"] = (
        (keys / "cloudfront-private.pem").read_text().strip().replace("\n", "\\n"))
    missing = [k for k in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY") if k not in values]
    if missing:
        sys.exit(f"hearline/backend/.env is missing {', '.join(missing)}")
    ssm.put_parameter(Name=NAME, Type="SecureString", Overwrite=True,
                      Value="\n".join(f"{k}={v}" for k, v in values.items()))
    print(f"stored {len(values)} settings in {NAME}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "us-east-1")
