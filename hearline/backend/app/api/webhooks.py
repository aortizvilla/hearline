"""Clerk webhook: keeps a listener's name and email in step with their login account, and
erases everything when the login account is deleted.

Clerk signs each delivery with Svix: HMAC-SHA256 over "{svix-id}.{svix-timestamp}.{body}" with
the endpoint's signing secret (CLERK_WEBHOOK_SECRET, "whsec_..."). Unsigned, stale (> 5 min) or
tampered requests are refused. Without a secret the endpoint does not exist (404).

Clerk dashboard → Webhooks → add https://<domain>/api/webhooks/clerk with the events
user.created, user.updated and user.deleted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlmodel import select

from app.accounts import delete_account
from app.api.routes import SessionDep, login_user, sync_identity
from app.config import get_settings
from app.database.models import User

router = APIRouter(prefix="/api/webhooks")
TOLERANCE_SEC = 300


def verify_svix(secret: str, msg_id: str, timestamp: str, body: bytes, signatures: str,
                now: float | None = None) -> bool:
    """True if one of the `v1,<base64>` signatures matches and the timestamp is fresh."""
    try:
        if abs((now or time.time()) - int(timestamp)) > TOLERANCE_SEC:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, TypeError):
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part.split(",", 1)[1])
               for part in signatures.split() if part.startswith("v1,"))


def _identity(data: dict[str, Any]) -> tuple[str, str]:
    name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p)
    primary = data.get("primary_email_address_id")
    emails = data.get("email_addresses") or []
    email = next((e.get("email_address", "") for e in emails if e.get("id") == primary),
                 emails[0].get("email_address", "") if emails else "")
    return name, email


@router.post("/clerk")
async def clerk_webhook(request: Request, session: SessionDep,
                        svix_id: str = Header(""), svix_timestamp: str = Header(""),
                        svix_signature: str = Header("")) -> dict[str, Any]:
    secret = get_settings().clerk_webhook_secret
    if not secret:
        raise HTTPException(status_code=404, detail="Not found.")
    body = await request.body()
    if not verify_svix(secret, svix_id, svix_timestamp, body, svix_signature):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    event = json.loads(body)
    kind, data = event.get("type", ""), event.get("data") or {}
    external_id = str(data.get("id") or "")
    user = (session.exec(select(User).where(User.external_id == external_id)).first()
            if external_id else None)
    if kind == "user.created" and external_id:
        name, email = _identity(data)
        login_user(session, external_id, name, email)       # same path as a first sign-in
    elif kind == "user.updated" and user:
        sync_identity(session, user, *_identity(data))
    elif kind == "user.deleted" and user:
        delete_account(session, user)
    return {"ok": True, "type": kind}
