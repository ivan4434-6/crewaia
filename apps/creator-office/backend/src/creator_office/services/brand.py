"""Brand kit: defaults, validation and the text handed to agents."""

from __future__ import annotations

import re
from typing import Any

from creator_office.media.thumbnails import (
    DEFAULT_COLORS,
    DEFAULT_FONT,
    FONT_FILES,
    LAYOUTS,
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def default_brand(name: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "voice": "",
        "do": [],
        "dont": [],
        "colors": dict(DEFAULT_COLORS),
        "font": DEFAULT_FONT,
        "layout": "side",
        "logo_asset_id": None,
        "subtitles_default": True,
        "default_hashtags": [],
        "allowed_sources": [],
    }


def merged(brand: dict[str, Any] | None, workspace_name: str = "") -> dict[str, Any]:
    base = default_brand(workspace_name)
    for key, value in (brand or {}).items():
        if key == "colors" and isinstance(value, dict):
            base["colors"].update(
                {k: v for k, v in value.items() if isinstance(v, str) and _HEX.match(v)}
            )
        elif key in base:
            base[key] = value
    return base


def sanitize(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    brand = merged(current)
    for key in ("name", "voice"):
        if isinstance(payload.get(key), str):
            brand[key] = payload[key].strip()[:2000]
    for key in ("do", "dont", "default_hashtags", "allowed_sources"):
        if isinstance(payload.get(key), list):
            brand[key] = [
                str(item).strip()[:300] for item in payload[key] if str(item).strip()
            ][:30]
    if isinstance(payload.get("colors"), dict):
        for name, value in payload["colors"].items():
            if name in DEFAULT_COLORS and isinstance(value, str) and _HEX.match(value):
                brand["colors"][name] = value.lower()
    if payload.get("font") in FONT_FILES:
        brand["font"] = payload["font"]
    if payload.get("layout") in LAYOUTS:
        brand["layout"] = payload["layout"]
    if isinstance(payload.get("subtitles_default"), bool):
        brand["subtitles_default"] = payload["subtitles_default"]
    if "logo_asset_id" in payload and (
        payload["logo_asset_id"] is None or isinstance(payload["logo_asset_id"], str)
    ):
        brand["logo_asset_id"] = payload["logo_asset_id"]
    brand["default_hashtags"] = [
        tag if tag.startswith("#") else f"#{tag}" for tag in brand["default_hashtags"]
    ]
    return brand


def voice_brief(brand: dict[str, Any]) -> str:
    lines: list[str] = []
    if brand.get("name"):
        lines.append(f"Brand: {brand['name']}")
    if brand.get("voice"):
        lines.append(f"Brand voice: {brand['voice']}")
    if brand.get("do"):
        lines.append("Always: " + "; ".join(brand["do"]))
    if brand.get("dont"):
        lines.append("Never: " + "; ".join(brand["dont"]))
    if brand.get("default_hashtags"):
        lines.append("Brand hashtags: " + " ".join(brand["default_hashtags"]))
    return (
        "\n".join(lines)
        if lines
        else "No brand voice has been defined yet: stay neutral and clear."
    )
