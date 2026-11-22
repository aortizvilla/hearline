"""Private podcast feed: each listener's episodes in any podcast app that accepts a feed link
(Apple Podcasts "Follow a Show by URL", Pocket Casts, Overcast, Castro, AntennaPod...).
Spotify does not let listeners add feeds by link.

    GET  /api/podcast                          the signed-in user's feed link (created once)
    POST /api/podcast/reset                    new link; the old one stops working
    GET  /api/feed/<token>.xml                 the feed (RSS 2.0 + Apple iTunes tags)
    GET  /api/feed/<token>/episodes/<id>.mp3   redirect to the episode audio

Podcast apps can't sign in, so the secret token in the link is the key (like a private
calendar link). The feed is marked itunes:block so directories never list it. Audio links
point here and redirect to a freshly signed storage link at download time, because podcast apps
download whenever they like and S3 links expire.
"""

from __future__ import annotations

import secrets
import xml.etree.ElementTree as ET
from datetime import UTC
from email.utils import format_datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, select

from app.api.routes import current_user
from app.config import get_settings, language_names
from app.database.db import get_session
from app.database.models import Episode, User, utcnow
from app.pipeline.step4_assemble import episode_sources
from app.providers.storage import get_storage, local_path

router = APIRouter(prefix="/api")
SessionDep = Annotated[Session, Depends(get_session)]
UserDep = Annotated[User, Depends(current_user)]
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("content", CONTENT)
MAX_ITEMS = 60  # about two months of daily episodes


def _base() -> str:
    return get_settings().public_base_url.rstrip("/")


def feed_url(user: User) -> str:
    return f"{_base()}/api/feed/{user.feed_token}.xml"


def _ensure_token(session: Session, user: User, reset: bool = False) -> User:
    if reset or not user.feed_token:
        user.feed_token = secrets.token_urlsafe(24)  # 192 bits: unguessable
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def _links(user: User) -> dict[str, str]:
    url = feed_url(user)
    return {"feed_url": url, "apple_podcasts_url": "podcast://" + url.split("://", 1)[1]}


@router.get("/podcast")
def get_podcast(session: SessionDep, user: UserDep) -> dict[str, str]:
    """The signed-in user's private feed link (and an Apple Podcasts subscribe link)."""
    return _links(_ensure_token(session, user))


@router.post("/podcast/reset")
def reset_podcast(session: SessionDep, user: UserDep) -> dict[str, str]:
    """A new feed link; apps using the old one stop getting episodes."""
    return _links(_ensure_token(session, user, reset=True))


def _user_by_token(session: Session, token: str) -> User:
    user = session.exec(select(User).where(User.feed_token == token)).first() if token else None
    if user is None:
        raise HTTPException(status_code=404, detail="Feed not found.")
    return user


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def _size(episode: Episode) -> int:
    if episode.audio_bytes:
        return episode.audio_bytes
    path = local_path(episode.audio_path or "")  # episodes made before sizes were stored
    return path.stat().st_size if path.is_file() else 0


def build_feed(session: Session, user: User) -> bytes:
    """RSS 2.0 with the iTunes tags Apple Podcasts and most apps read."""
    now = utcnow()
    episodes = session.exec(
        select(Episode).where(Episode.user_id == user.id, Episode.status == "ready",
                              col(Episode.audio_path).is_not(None),
                              col(Episode.published_at) <= now)
        .order_by(col(Episode.published_at).desc()).limit(MAX_ITEMS)).all()
    language = user.profile.language if user.profile else "en"
    name = (user.name or "").strip()
    title = f"Hearline · {name}" if name and name.lower() != "listener" else "Hearline"
    image = get_settings().podcast_image_url or next(
        (e.image_url for e in episodes if e.image_url), "")

    rss = ET.Element("rss", {"version": "2.0"})
    ch = _sub(rss, "channel")
    _sub(ch, "title", title)
    _sub(ch, "link", _base())
    _sub(ch, "description", "Your personal daily news briefing from Hearline: the topics you "
                            "follow, researched and read for you.")
    _sub(ch, "language", language)
    _sub(ch, "generator", "Hearline")
    _sub(ch, "lastBuildDate", format_datetime(now))
    _sub(ch, f"{{{ITUNES}}}author", "Hearline")
    _sub(ch, f"{{{ITUNES}}}summary", f"Personal news briefing in "
                                     f"{language_names().get(language, language)}.")
    _sub(ch, f"{{{ITUNES}}}explicit", "false")
    _sub(ch, f"{{{ITUNES}}}type", "episodic")
    _sub(ch, f"{{{ITUNES}}}block", "Yes")  # private: never listed in podcast directories
    ET.SubElement(ch, f"{{{ITUNES}}}category", {"text": "News"})
    if image:
        _sub(ch, f"{{{ITUNES}}}image", href=image)
        img = _sub(ch, "image")
        _sub(img, "url", image)
        _sub(img, "title", title)
        _sub(img, "link", _base())

    for ep in episodes:
        item = _sub(ch, "item")
        _sub(item, "title", ep.title)
        _sub(item, "description", ep.description)
        sources = episode_sources(session, ep)
        html = (f"<p>{_esc(ep.description)}</p>" + ("<p>Sources:</p><ul>" + "".join(
            f'<li><a href="{_esc(s["url"])}">{_esc(s["headline"])}</a> '
            f"({_esc(s['source_name'])})</li>" for s in sources) + "</ul>" if sources else ""))
        _sub(item, f"{{{CONTENT}}}encoded", html)
        _sub(item, "guid", f"hearline-episode-{ep.id}", isPermaLink="false")
        _sub(item, "pubDate", format_datetime(ep.published_at.replace(tzinfo=UTC)
                                              if ep.published_at.tzinfo is None
                                              else ep.published_at))
        audio = f"{_base()}/api/feed/{user.feed_token}/episodes/{ep.id}.mp3"
        mime = "audio/mpeg" if (ep.audio_path or "").endswith(".mp3") else "audio/wav"
        _sub(item, "enclosure", url=audio, length=str(_size(ep)), type=mime)
        if ep.duration_seconds:
            _sub(item, f"{{{ITUNES}}}duration", str(ep.duration_seconds))
        if ep.image_url:
            _sub(item, f"{{{ITUNES}}}image", href=ep.image_url)
        _sub(item, f"{{{ITUNES}}}explicit", "false")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(
        '"', "&quot;")


@router.get("/feed/{token}.xml")
def podcast_feed(token: str, session: SessionDep) -> Response:
    """The private feed (no sign-in: the token is the key)."""
    xml = build_feed(session, _user_by_token(session, token))
    return Response(xml, media_type="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/feed/{token}/episodes/{episode_id}.mp3")
def podcast_audio(token: str, episode_id: int, session: SessionDep) -> RedirectResponse:
    """Redirect a podcast app to the episode audio (a fresh signed link in S3 mode)."""
    user = _user_by_token(session, token)
    ep = session.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id or not ep.audio_path or ep.status != "ready":
        raise HTTPException(status_code=404, detail="Episode not found.")
    url = get_storage().url(ep.audio_path)
    return RedirectResponse(url if "://" in url else _base() + url, status_code=302)
