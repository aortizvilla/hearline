"""Free web lookups for research (step 2): is an article link alive, and Wikipedia background.

looks_like_article: a link to one article, not a section, tag or newsletter page: its path has
a date (/2026/11/21/), a slug of 3+ words (a-tale-of-two-nvidias) or a long id (AP's hex ids,
numeric ids). "/tecnologia" or "/noticias/inteligencia-artificial" are not articles.

article_link_ok = looks_like_article + url_alive (what research uses).

url_alive: one GET per new story's link. 404/410/5xx/unreachable, or a "page not found" title
served with 200 (soft 404), -> the story is dropped (the researcher model must not cite pages
that don't exist). 401/402/403/429 count as alive: paywalls and bot walls (e.g. Cloudflare in
front of apnews.com) block us, not readers, so those links can't be checked.

wikipedia_background: for a term a listener may not know ("H200 chip"), the short intro of its
Wikipedia article in the story's language (English if that language has none). Uses the public
REST API: title search, then the page summary; disambiguation pages are skipped.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from urllib.parse import quote

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
TIMEOUT = httpx.Timeout(8.0, connect=5.0)
MAX_SUMMARY_CHARS = 450
NOT_FOUND_TITLE = re.compile(
    rb"<title[^>]*>[^<]*(not found|\b404\b|page introuvable|no encontrad|nicht gefunden|"
    rb"non trovat|n[\xc3\xa3a]o encontrad)", re.I)


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().http_user_agent}


ARTICLE_SEGMENT = re.compile(
    r"^(\d{4}|[0-9a-f]{16,}|\d{5,}|[a-z0-9]{0,40}(?:-[a-z0-9]+){2,}.*|.*\d{5,}.*)$", re.I)
SECTION_WORDS = {"tag", "tags", "topic", "topics", "section", "sections", "category",
                 "categories", "newsletter", "newsletters", "noticias", "temas", "tema",
                 "themen", "thema", "rubrique", "sujet", "argomenti", "assunto"}


def looks_like_article(url: str) -> bool:
    """True if the URL's path identifies a single article (see module docstring)."""
    from urllib.parse import urlsplit

    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments or (len(segments) <= 2 and segments[0].lower() in SECTION_WORDS):
        return False
    has_date = bool(re.search(r"/(19|20)\d{2}/\d{1,2}(/\d{1,2})?/", urlsplit(url).path + "/"))
    return has_date or any(ARTICLE_SEGMENT.match(s) and not s.isalpha() for s in segments
                           if s.lower() not in SECTION_WORDS)


def article_link_ok(url: str) -> bool:
    return looks_like_article(url) and url_alive(url)


def url_alive(url: str) -> bool:
    try:
        with httpx.stream("GET", url, headers=_headers(), timeout=TIMEOUT,
                          follow_redirects=True) as resp:
            if resp.status_code in (401, 402, 403, 429):
                return True  # blocked for bots, not missing
            if resp.status_code >= 400:
                return False
            head = b""
            for chunk in resp.iter_bytes():  # the <title> is near the top: read 64 KB at most
                head += chunk
                if len(head) >= 65536:
                    break
            return not NOT_FOUND_TITLE.search(head)
    except httpx.HTTPError as exc:
        log.info("Article link unreachable %s: %s", url, exc)
        return False


def _short(text: str) -> str:
    """First sentences of an intro, up to MAX_SUMMARY_CHARS."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    cut = text[:MAX_SUMMARY_CHARS]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > 100 else cut.rsplit(" ", 1)[0] + "..."


@lru_cache(maxsize=2048)
def _lookup(term: str, language: str) -> dict[str, str] | None:
    base = f"https://{language}.wikipedia.org"
    try:
        found = httpx.get(f"{base}/w/rest.php/v1/search/title", params={"q": term, "limit": 1},
                          headers=_headers(), timeout=TIMEOUT)
        pages = found.json().get("pages", []) if found.status_code == 200 else []
        if not pages:
            return None
        summary = httpx.get(f"{base}/api/rest_v1/page/summary/{quote(pages[0]['key'], safe='')}",
                            headers=_headers(), timeout=TIMEOUT, follow_redirects=True)
        data = summary.json() if summary.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Wikipedia lookup failed for %r (%s): %s", term, language, exc)
        return None
    extract = (data.get("extract") or "").strip()
    if data.get("type") == "disambiguation" or len(extract) < 40:
        return None
    return {"term": term, "summary": _short(extract), "language": language,
            "url": (data.get("content_urls") or {}).get("desktop", {}).get("page", ""),
            "title": data.get("title", "")}


def wikipedia_background(term: str, language: str = "en") -> dict[str, str] | None:
    """Wikipedia intro for `term` in `language`, else English; None if nothing fits."""
    term = term.strip()[:100]
    if len(term) < 2:
        return None
    return _lookup(term, language) or (_lookup(term, "en") if language != "en" else None)
