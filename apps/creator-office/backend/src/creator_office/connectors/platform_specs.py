"""Per-platform requirements used for validation, export presets and safe zones.

These limits come from each platform's public documentation as known when the
connector was written. Platforms change them without notice, so the UI shows
them as reference values and links to the official page; they were not checked
against the live documentation by this application.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from creator_office.text import decimal


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    name: str
    provider: str
    video_format: str
    title_max: int
    title_required: bool
    description_max: int
    hashtags_max: int | None
    tags_total_max: int | None
    duration_min_s: float
    duration_max_s: float
    duration_note: str
    size_warn_mb: int
    aspect: str  # "vertical" | "horizontal"
    cover: str  # which design kind is used as cover
    docs_url: str


SPECS: dict[str, PlatformSpec] = {
    "youtube": PlatformSpec(
        "youtube",
        "YouTube",
        "youtube",
        "horizontal",
        title_max=100,
        title_required=True,
        description_max=5000,
        hashtags_max=None,
        tags_total_max=500,
        duration_min_s=1,
        duration_max_s=12 * 3600,
        duration_note="Las cuentas sin verificar están limitadas a 15 minutos.",
        size_warn_mb=256_000,
        aspect="horizontal",
        cover="thumbnail",
        docs_url="https://developers.google.com/youtube/v3/docs/videos/insert",
    ),
    "youtube_shorts": PlatformSpec(
        "youtube_shorts",
        "YouTube Shorts",
        "youtube",
        "vertical",
        title_max=100,
        title_required=True,
        description_max=5000,
        hashtags_max=None,
        tags_total_max=500,
        duration_min_s=1,
        duration_max_s=180,
        duration_note="YouTube clasifica como Short los vídeos verticales o cuadrados de hasta 3 minutos.",
        size_warn_mb=256_000,
        aspect="vertical",
        cover="cover",
        docs_url="https://support.google.com/youtube/answer/15424877",
    ),
    "tiktok": PlatformSpec(
        "tiktok",
        "TikTok",
        "tiktok",
        "vertical",
        title_max=2200,
        title_required=False,
        description_max=0,
        hashtags_max=None,
        tags_total_max=None,
        duration_min_s=3,
        duration_max_s=600,
        duration_note="El máximo real lo fija cada cuenta (creator_info.max_video_post_duration_sec).",
        size_warn_mb=4000,
        aspect="vertical",
        cover="cover",
        docs_url="https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
    ),
    "instagram_reels": PlatformSpec(
        "instagram_reels",
        "Instagram Reels",
        "instagram",
        "vertical",
        title_max=0,
        title_required=False,
        description_max=2200,
        hashtags_max=30,
        tags_total_max=None,
        duration_min_s=3,
        duration_max_s=900,
        duration_note="Para aparecer en la pestaña Reels se recomiendan entre 5 y 90 segundos en 9:16.",
        size_warn_mb=300,
        aspect="vertical",
        cover="cover",
        docs_url="https://developers.facebook.com/docs/instagram-platform/content-publishing",
    ),
}

# Fractions of the frame that platform interface elements tend to cover.
# Approximate by nature: none of the platforms publishes one fixed layout.
SAFE_ZONES: dict[str, dict[str, float]] = {
    "vertical": {"top": 0.12, "bottom": 0.28, "left": 0.06, "right": 0.14},
    "horizontal": {"top": 0.05, "bottom": 0.07, "left": 0.05, "right": 0.05},
    "square": {"top": 0.08, "bottom": 0.12, "left": 0.06, "right": 0.06},
    # YouTube thumbnail: the duration badge sits on the bottom-right corner.
    "thumbnail": {
        "top": 0.06,
        "bottom": 0.08,
        "left": 0.05,
        "right": 0.05,
        "badge_w": 0.2,
        "badge_h": 0.18,
    },
}

DESIGN_SIZES: dict[str, tuple[int, int]] = {
    "thumbnail": (1280, 720),
    "cover": (1080, 1920),
}

Check = dict[str, Any]


def check(id: str, status: str, label: str, detail: str = "") -> Check:
    """status: pass | warn | fail | info"""
    return {"id": id, "status": status, "label": label, "detail": detail}


def hashtags_in(text: str) -> list[str]:
    return [word for word in text.split() if word.startswith("#") and len(word) > 1]


def has_placeholder(text: str) -> bool:
    """Bracketed scaffolding left by the demo engine (or by a person) to fill in."""
    start = text.find("[")
    return start != -1 and text.find("]", start) != -1


def validate_video(
    platform: str, media: dict[str, Any] | None, size_bytes: int = 0
) -> list[Check]:
    spec = SPECS[platform]
    if not media or not media.get("video"):
        return [
            check(
                "video",
                "fail",
                "Vídeo",
                "No hay ninguna pieza de vídeo lista para esta publicación.",
            )
        ]
    out: list[Check] = []
    duration = float(media.get("duration") or 0)
    video = media["video"]
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if duration < spec.duration_min_s:
        out.append(
            check(
                "duration",
                "fail",
                "Duración",
                f"{decimal(duration)} s: por debajo del mínimo de {spec.duration_min_s:g} s.",
            )
        )
    elif duration > spec.duration_max_s:
        out.append(
            check(
                "duration",
                "fail",
                "Duración",
                f"{duration:.0f} s: supera el máximo de {spec.duration_max_s:g} s. {spec.duration_note}",
            )
        )
    else:
        out.append(
            check(
                "duration",
                "pass",
                "Duración",
                f"{decimal(duration)} s. {spec.duration_note}",
            )
        )
    is_vertical = height > width
    if spec.aspect == "vertical" and not is_vertical:
        out.append(
            check(
                "aspect",
                "warn",
                "Formato",
                f"{width}×{height}: esta plataforma espera vídeo vertical (9:16).",
            )
        )
    elif spec.aspect == "horizontal" and is_vertical:
        out.append(
            check(
                "aspect",
                "warn",
                "Formato",
                f"{width}×{height}: un vídeo vertical se publicará como Short.",
            )
        )
    else:
        out.append(check("aspect", "pass", "Formato", f"{width}×{height}"))
    if video.get("codec") not in ("h264", "hevc"):
        out.append(
            check(
                "codec",
                "warn",
                "Códec",
                f"Códec {video.get('codec')}: se recomienda H.264.",
            )
        )
    if not media.get("audio"):
        out.append(check("audio", "warn", "Audio", "La pieza no tiene pista de audio."))
    size_mb = size_bytes / 1_048_576
    if size_mb > spec.size_warn_mb:
        out.append(
            check(
                "size",
                "warn",
                "Tamaño",
                f"{size_mb:.0f} MB: puede superar el límite de la plataforma; compruébalo.",
            )
        )
    return out


def validate_metadata(
    platform: str, title: str, description: str, tags: list[str]
) -> list[Check]:
    spec = SPECS[platform]
    out: list[Check] = []
    caption = description if spec.title_max == 0 else title
    # TikTok and Instagram publish a single text: name it the way the screen does.
    title_label = "Título" if spec.description_max else "Texto"
    description_label = "Descripción" if spec.title_max else "Texto"
    if spec.title_required and not title.strip():
        out.append(check("title", "fail", title_label, "Falta el título."))
    elif spec.title_max and len(title) > spec.title_max:
        out.append(
            check(
                "title",
                "fail",
                title_label,
                f"{len(title)} caracteres: el máximo es {spec.title_max}.",
            )
        )
    elif spec.title_max and title.strip():
        out.append(
            check(
                "title",
                "pass",
                title_label,
                f"{len(title)}/{spec.title_max} caracteres.",
            )
        )
    if spec.provider == "youtube":  # YouTube rejects angle brackets in both fields
        if "<" in title or ">" in title:
            out.append(
                check(
                    "title_chars",
                    "fail",
                    "Título",
                    "No puede contener los caracteres < ni >.",
                )
            )
        if "<" in description or ">" in description:
            out.append(
                check(
                    "description_chars",
                    "fail",
                    "Descripción",
                    "No puede contener los caracteres < ni >.",
                )
            )
    if spec.description_max:
        if len(description) > spec.description_max:
            out.append(
                check(
                    "description",
                    "fail",
                    description_label,
                    f"{len(description)} caracteres: el máximo es {spec.description_max}.",
                )
            )
        elif not description.strip():
            out.append(
                check(
                    "description",
                    "warn",
                    description_label,
                    "El texto está vacío."
                    if not spec.title_max
                    else "La descripción está vacía.",
                )
            )
        else:
            out.append(
                check(
                    "description",
                    "pass",
                    description_label,
                    f"{len(description)}/{spec.description_max} caracteres.",
                )
            )
    if not caption.strip() and not spec.title_required:
        out.append(check("caption", "warn", "Texto", "La publicación no tiene texto."))
    if spec.hashtags_max is not None:
        count = len(hashtags_in(description)) + len(hashtags_in(title))
        if count > spec.hashtags_max:
            out.append(
                check(
                    "hashtags",
                    "fail",
                    "Hashtags",
                    f"{count} hashtags: el máximo es {spec.hashtags_max}.",
                )
            )
    if spec.tags_total_max is not None:
        total = sum(len(t) for t in tags) + max(0, len(tags) - 1)
        if total > spec.tags_total_max:
            out.append(
                check(
                    "tags",
                    "fail",
                    "Etiquetas",
                    f"{total} caracteres en etiquetas: el máximo es {spec.tags_total_max}.",
                )
            )
    if has_placeholder(title) or has_placeholder(description):
        out.append(
            check(
                "placeholders",
                "fail",
                "Texto sin completar",
                "Quedan marcadores entre corchetes [ ] por rellenar.",
            )
        )
    return out


def overall(checks: list[Check]) -> str:
    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        return "fail"
    return "warn" if "warn" in statuses else "pass"
