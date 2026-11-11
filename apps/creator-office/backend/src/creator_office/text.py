"""Small helpers for user-facing Spanish copy."""

from __future__ import annotations

import re


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``plural(1, "fuente")`` → "1 fuente"; ``plural(3, "fuente")`` → "3 fuentes"."""
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count} {word}"


def choose(count: int, singular: str, plural_form: str) -> str:
    """Pick a phrase by number without printing the number."""
    return singular if count == 1 else plural_form


def decimal(value: float, digits: int = 1) -> str:
    """Spanish decimal comma: ``decimal(15.63)`` → "15,6"."""
    return f"{value:.{digits}f}".replace(".", ",")


def number(value: float, digits: int = 1) -> str:
    """A figure as the interface writes it: "1840", "18.400", "8,8".

    Whole numbers lose the decimals, and thousands are only grouped from five
    digits on, as Spanish style does.
    """
    rounded = round(value, digits)
    whole = rounded == int(rounded)
    text = f"{int(rounded):,}" if whole else f"{rounded:,.{digits}f}"
    if abs(rounded) < 10000:
        text = text.replace(",", "")
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def shorten(text: str, limit: int = 60) -> str:
    """One line of at most ``limit`` characters, cut at a word and marked with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def timecode(seconds: float) -> str:
    """Position in a video as people read it: ``timecode(83.4)`` → "1:23"."""
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


_AUTO_CLIP_TITLE = re.compile(r"Clip \d+:\d{2}–\d+:\d{2}")


def clip_title(start: float, end: float) -> str:
    """The name a clip gets when nothing better is known: its span."""
    return f"Clip {timecode(start)}–{timecode(end)}"


def is_auto_clip_title(title: str) -> bool:
    """True for a title that only restates the span, which must follow the span when it is trimmed."""
    return bool(_AUTO_CLIP_TITLE.fullmatch(title.strip()))
