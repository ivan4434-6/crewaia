"""The workspace's time zone.

Instants are stored in UTC and the browser shows them in its own zone. Two
things are written on the server and need a place on the map: dates inside
messages that people read later (activity log, manual export notes) and
hour-of-day statistics. Both use the zone the workspace declares and always
name it, because a team member may be reading from somewhere else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT = "UTC"


def canonical(name: str | None) -> str | None:
    """Return ``name`` when it is a zone of the IANA database, otherwise ``None``."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def zone_name(settings: dict[str, Any] | None) -> str:
    """The zone stored in a workspace's settings, or UTC when it never declared one."""
    return canonical((settings or {}).get("timezone")) or DEFAULT


def label(name: str) -> str:
    """How the zone is named inside a sentence: "hora de Madrid", "UTC"."""
    if name in ("UTC", "Etc/UTC"):
        return "UTC"
    return "hora de " + name.rsplit("/", 1)[-1].replace("_", " ")


def local(moment: datetime, settings: dict[str, Any] | None) -> datetime:
    return moment.astimezone(ZoneInfo(zone_name(settings)))


def stamp(moment: datetime, settings: dict[str, Any] | None) -> str:
    """A date for a sentence, with its zone: "13/11/2026 a las 15:39 (hora de Madrid)"."""
    name = zone_name(settings)
    return f"{moment.astimezone(ZoneInfo(name)):%d/%m/%Y a las %H:%M} ({label(name)})"
