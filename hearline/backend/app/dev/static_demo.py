"""Static demo snapshot: the demo listeners' app, saved as files for a static host (dev only).

    python -m app.cli export-demo            -> ../frontend/demo-data/

Calls the real API (in process, on the store in DATABASE_URL) for each demo listener and
saves every answer the frontend needs as JSON, plus the audio those answers point to. A
frontend built with VITE_STATIC_DEMO=1 (src/staticDemo.ts) reads these files instead of an
API, so the whole demo runs on a static host (Cloudflare Pages) with no server, no database and
no API keys.

    index.json                    the demo listeners (name, email) and when this was exported
    api/options.json         /api/options, /api/interests, /api/plans
    api/admin/dashboard.json /api/admin/dashboard (last 30 days)
    u/<id>/profile.json      each listener's /api/profile
    u/<id>/episodes.json     each listener's /api/episodes (all of them)
    audio/...                     the episode MP3s and voice samples those files link to

Only demo listeners (app/dev/demo.py LISTENERS) are exported: their data is mock data
(example.com addresses, generated episodes), safe to publish. Nothing secret is written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from sqlmodel import Session, func, select

from app.config import get_settings
from app.database.db import make_engine
from app.database.models import User, utcnow
from app.dev.demo import LISTENERS, api_client

# Not in public/: only static demo builds include it (frontend/vite.config.ts, demoData)
DEFAULT_OUT = Path(__file__).resolve().parents[3] / "frontend" / "demo-data"


def _audio_paths(value: Any) -> set[str]:
    """Every /audio/... link inside an API answer."""
    found: set[str] = set()
    if isinstance(value, str) and value.startswith("/audio/"):
        found.add(value.removeprefix("/audio/"))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _audio_paths(v)
    elif isinstance(value, list):
        for v in value:
            found |= _audio_paths(v)
    return found


def export_demo(out: Path = DEFAULT_OUT) -> dict[str, Any]:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    api = api_client(engine)
    if out.exists():
        shutil.rmtree(out)  # a snapshot replaces the previous one entirely
    (out / "api" / "admin").mkdir(parents=True)

    def save(path: str, data: Any) -> Any:
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")
        return data

    def get(path: str, user_id: int | None = None) -> Any:
        headers = {"X-User-Id": str(user_id)} if user_id else {}
        resp = api.get(path, headers=headers)
        if resp.status_code != 200:
            raise SystemExit(f"export-demo: GET {path} -> {resp.status_code} {resp.text[:200]}")
        return resp.json()

    audio: set[str] = set()
    for name in ("options", "interests", "plans"):
        audio |= _audio_paths(save(f"api/{name}.json", get(f"/api/{name}")))

    emails = [spec["email"] for spec in LISTENERS]
    with Session(engine) as s:
        users = s.exec(select(User).where(func.lower(User.email).in_(emails))).all()
    listeners = []
    for user in sorted(users, key=lambda u: emails.index(u.email.lower())):
        uid = user.id
        profile = save(f"u/{uid}/profile.json", get("/api/profile", uid))
        episodes = save(f"u/{uid}/episodes.json", get("/api/episodes", uid))
        audio |= _audio_paths(profile) | _audio_paths(episodes)
        listeners.append({"user_id": uid, "name": user.name, "email": user.email,
                          "language": profile.get("language"), "episodes": len(episodes)})
    if not listeners:
        raise SystemExit("export-demo: no demo listeners in this store; run `app.cli demo`")

    save("api/admin/dashboard.json", get("/api/admin/dashboard?days=30", listeners[0]["user_id"]))
    save("index.json", {"exported_at": utcnow().isoformat(), "listeners": listeners})

    missing = []
    for rel in sorted(audio):
        src = settings.audio_dir / rel
        if not src.exists():
            missing.append(rel)
            continue
        dst = out / "audio" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return {"out": str(out), "listeners": listeners, "audio_files": len(audio) - len(missing),
            "missing_audio": missing, "megabytes": round(size / 1e6, 1)}
