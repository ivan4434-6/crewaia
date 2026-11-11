"""Reads the sources a user allowed: reference links and RSS/Atom feeds.

Only public http(s) pages are requested, the site's robots.txt is honoured,
and nothing is returned that was not present in the response. Dates are
reported exactly as the page declares them, or not at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml import ElementTree

from creator_office.net import http_client, is_certificate_error
from creator_office.security import UnsafeUrlError, assert_public_url

USER_AGENT = (
    "CreatorOfficeAI/0.1 (research assistant; reads only links provided by its user)"
)
MAX_BYTES = 1_500_000
TIMEOUT = httpx.Timeout(12.0, connect=6.0)


@dataclass
class SourceItem:
    url: str
    title: str = ""
    description: str = ""
    published: str | None = None  # ISO date as declared by the source
    site: str = ""
    excerpt: str = ""
    kind: str = "page"  # page | feed_item
    feed_url: str = ""


@dataclass
class SourceResult:
    requested_url: str
    ok: bool
    error: str = ""
    items: list[SourceItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.time_datetime = ""
        self.json_ld: list[str] = []
        self.paragraphs: list[str] = []
        self._stack: list[str] = []
        self._buffer: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag in (
            "script",
            "style",
            "noscript",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
        ):
            if tag == "script" and values.get("type") == "application/ld+json":
                self._stack.append("jsonld")
                self._buffer = []
                return
            self._skip += 1
            return
        if tag == "meta":
            key = (
                values.get("property")
                or values.get("name")
                or values.get("itemprop")
                or ""
            ).lower()
            if key and values.get("content"):
                self.meta.setdefault(key, values["content"].strip())
        elif tag == "time" and values.get("datetime") and not self.time_datetime:
            self.time_datetime = values["datetime"]
        elif tag in ("title", "p", "h1"):
            self._stack.append(tag)
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._stack and self._stack[-1] == "jsonld" and tag == "script":
            self.json_ld.append("".join(self._buffer))
            self._stack.pop()
            return
        if tag in (
            "script",
            "style",
            "noscript",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
        ):
            self._skip = max(0, self._skip - 1)
            return
        if self._stack and self._stack[-1] == tag:
            text = " ".join("".join(self._buffer).split())
            self._stack.pop()
            if tag == "title" and not self.title:
                self.title = text
            elif tag == "h1" and not self.meta.get("h1"):
                self.meta["h1"] = text
            elif tag == "p" and len(text) > 60 and not self._skip:
                self.paragraphs.append(text)

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._buffer.append(data)


def _iso_date(value: str | None) -> str | None:
    """Normalise a declared date; return ``None`` rather than guessing."""
    if not value:
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
            if not match:
                return None
            return match.group(0)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed > datetime.now(UTC):
        return None
    return parsed.date().isoformat()


def _json_ld_date(blobs: list[str]) -> str | None:
    def walk(node: Any) -> str | None:
        if isinstance(node, dict):
            for key in ("datePublished", "dateCreated", "uploadDate"):
                if isinstance(node.get(key), str):
                    return node[key]
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None

    for blob in blobs:
        try:
            found = walk(json.loads(blob))
        except ValueError:
            continue
        if found:
            return found
    return None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_html(url: str, html: str) -> SourceItem:
    parser = _PageParser()
    parser.feed(html)
    meta = parser.meta
    published = (
        meta.get("article:published_time")
        or meta.get("datepublished")
        or meta.get("date")
        or meta.get("dc.date")
        or _json_ld_date(parser.json_ld)
        or parser.time_datetime
    )
    return SourceItem(
        url=url,
        title=_clip(meta.get("og:title") or parser.title or meta.get("h1", ""), 200),
        description=_clip(
            meta.get("og:description") or meta.get("description", ""), 400
        ),
        published=_iso_date(published),
        site=meta.get("og:site_name") or urlparse(url).hostname or "",
        excerpt=_clip(" ".join(parser.paragraphs[:6]), 1200),
    )


_TAG = re.compile(r"<[^>]+>")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(url: str, xml: str, limit: int = 8) -> list[SourceItem]:
    root = ElementTree.fromstring(xml)
    items: list[SourceItem] = []
    site = urlparse(url).hostname or ""
    for node in root.iter():
        if _strip_ns(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in node:
            name = _strip_ns(child.tag)
            if name == "link":
                href = child.attrib.get("href") or (child.text or "").strip()
                if href and (
                    child.attrib.get("rel", "alternate") == "alternate"
                    or "link" not in fields
                ):
                    fields["link"] = href
            elif name in (
                "title",
                "description",
                "summary",
                "pubdate",
                "published",
                "updated",
                "date",
            ):
                fields.setdefault(name, (child.text or "").strip())
        link = fields.get("link", "")
        if not link:
            continue
        items.append(
            SourceItem(
                url=urljoin(url, link),
                title=_clip(_TAG.sub("", fields.get("title", "")), 200),
                description=_clip(
                    _TAG.sub(
                        "", fields.get("description") or fields.get("summary", "")
                    ),
                    400,
                ),
                published=_iso_date(
                    fields.get("pubdate")
                    or fields.get("published")
                    or fields.get("date")
                    or fields.get("updated")
                ),
                site=site,
                kind="feed_item",
                feed_url=url,
            )
        )
        if len(items) >= limit:
            break
    return items


def new_client() -> httpx.Client:
    return http_client(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    )


def _robots_allows(client: httpx.Client, url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = client.get(robots_url)
    except httpx.HTTPError:
        return True  # unreachable robots.txt is treated like a missing one
    if response.status_code >= 400:
        return True
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(USER_AGENT, url)


def _read_limited(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        chunks.append(chunk)
        if total >= MAX_BYTES:
            break
    return b"".join(chunks)


def fetch_source(url: str, client: httpx.Client | None = None) -> SourceResult:
    """Fetch one allowed source. Never raises: failures are reported in the result."""
    result = SourceResult(requested_url=url, ok=False)
    own_client = client is None
    try:
        safe_url = assert_public_url(url)
    except UnsafeUrlError as exc:
        result.error = str(exc)
        return result
    http = client or new_client()
    try:
        if not _robots_allows(http, safe_url):
            result.error = "El sitio no permite la lectura automática (robots.txt). No se ha consultado."
            return result
        current = safe_url
        for _ in range(4):  # follow redirects by hand so each hop is re-validated
            with http.stream("GET", current) as response:
                if response.status_code in (
                    301,
                    302,
                    303,
                    307,
                    308,
                ) and response.headers.get("location"):
                    current = assert_public_url(
                        urljoin(current, response.headers["location"])
                    )
                    continue
                if response.status_code >= 400:
                    result.error = (
                        f"La fuente respondió con el código {response.status_code}."
                    )
                    return result
                content_type = response.headers.get("content-type", "").lower()
                body = _read_limited(response)
                encoding = response.encoding or "utf-8"
            break
        else:
            result.error = "Demasiadas redirecciones."
            return result
        text = body.decode(encoding, errors="replace")
        head = text[:400].lstrip().lower()
        if "xml" in content_type or head.startswith(("<?xml", "<rss", "<feed")):
            result.items = parse_feed(current, text)
            result.ok = bool(result.items)
            if not result.ok:
                result.error = "El feed no contiene entradas legibles."
        elif "html" in content_type or "<html" in head or "<!doctype html" in head:
            item = parse_html(current, text)
            if not item.title and not item.excerpt:
                result.error = "La página no contiene texto legible (puede requerir JavaScript o inicio de sesión)."
            else:
                result.items = [item]
                result.ok = True
        else:
            result.error = (
                f"Tipo de contenido no admitido ({content_type or 'desconocido'})."
            )
    except UnsafeUrlError as exc:
        result.error = str(exc)
    except httpx.TimeoutException:
        result.error = "La fuente no respondió a tiempo."
    except httpx.HTTPError as exc:
        if is_certificate_error(exc):
            result.error = "No se pudo verificar el certificado de seguridad del sitio. No se ha consultado."
        else:
            result.error = f"No se pudo conectar con la fuente ({type(exc).__name__})."
    except ElementTree.ParseError:
        result.error = "El feed no es XML válido."
    finally:
        if own_client:
            http.close()
    return result
