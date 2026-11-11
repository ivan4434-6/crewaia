"""Thumbnail and cover composition with Pillow.

Layouts place the headline inside the platform safe zone by construction and
the result is verified afterwards, so the "safe zone" check reflects the real
pixels that were drawn.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from creator_office.connectors.platform_specs import (
    DESIGN_SIZES,
    SAFE_ZONES,
    Check,
    check,
)
from creator_office.media.ffmpeg import FONTS_DIR
from creator_office.text import decimal

FONT_FILES: dict[str, str] = {
    "Anton": "Anton_400Regular.ttf",
    "Archivo Black": "ArchivoBlack_400Regular.ttf",
    "Bebas Neue": "BebasNeue_400Regular.ttf",
    "Inter": "Inter_800ExtraBold.ttf",
    "Poppins": "Poppins_700Bold.ttf",
    "Space Grotesk": "SpaceGrotesk_700Bold.ttf",
}
DEFAULT_FONT = "Anton"
LAYOUTS = ("side", "band", "center")

DEFAULT_COLORS = {
    "primary": "#22d3ee",
    "secondary": "#8b5cf6",
    "accent": "#f472b6",
    "background": "#0b1020",
    "text": "#ffffff",
}


def font_path(family: str) -> Path:
    return FONTS_DIR / FONT_FILES.get(family, FONT_FILES[DEFAULT_FONT])


@lru_cache
def ass_font_name(family: str) -> str:
    """Full font name as libass matches it (family plus style)."""
    name, style = ImageFont.truetype(str(font_path(family)), 20).getname()
    return name if not style or style == "Regular" else f"{name} {style}"


def _rgb(value: str, fallback: str = "#ffffff") -> tuple[int, int, int]:
    text = (value or fallback).lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, IndexError):
        return _rgb(fallback)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (
            max(width, round(image.width * scale)),
            max(height, round(image.height * scale)),
        ),
        Image.LANCZOS,
    )
    left, top = (resized.width - width) // 2, (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _gradient_background(size: tuple[int, int], colors: dict[str, str]) -> Image.Image:
    width, height = size
    start, end, base = (
        _rgb(colors["primary"]),
        _rgb(colors["secondary"]),
        _rgb(colors["background"]),
    )
    small = Image.new("RGB", (64, 64))
    pixels = small.load()
    assert pixels is not None  # noqa: S101
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126
            mixed = tuple(round(start[i] * (1 - t) + end[i] * t) for i in range(3))
            # Keep it dark enough for white text: blend 55% towards the brand background.
            pixels[x, y] = tuple(
                round(mixed[i] * 0.45 + base[i] * 0.55) for i in range(3)
            )
    image = small.resize(size, Image.BICUBIC)
    # Pixel-grid texture, a nod to the office's pixel art.
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cell = max(24, width // 40)
    for gy in range(0, height, cell):
        for gx in range(0, width, cell):
            if (gx // cell * 7 + gy // cell * 13) % 5 == 0:
                draw.rectangle(
                    (gx, gy, gx + cell - 2, gy + cell - 2), fill=(255, 255, 255, 14)
                )
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def _scrim(size: tuple[int, int], layout: str, kind: str) -> Image.Image:
    """Dark overlay that guarantees legibility where the headline goes."""
    width, height = size
    mask = Image.new("L", size, 0)
    px = mask.load()
    assert px is not None  # noqa: S101
    if layout == "side":
        for x in range(width):
            alpha = round(225 * max(0.0, 1 - x / (width * 0.72)) ** 1.1)
            for y in range(height):
                px[x, y] = alpha
    elif layout == "band":
        top = 0.5 if kind == "thumbnail" else 0.38
        for y in range(height):
            t = (y / height - top) / (1 - top)
            alpha = round(235 * min(1.0, max(0.0, t * 1.6)))
            if kind == "cover" and y / height > 0.8:
                alpha = 235
            for x in range(width):
                px[x, y] = alpha
    else:
        for y in range(height):
            for x in range(width):
                px[x, y] = 140
    overlay = Image.new("RGBA", size, (4, 6, 16, 255))
    overlay.putalpha(mask)
    return overlay


def _text_box(
    size: tuple[int, int], kind: str, layout: str
) -> tuple[int, int, int, int]:
    width, height = size
    safe = SAFE_ZONES["thumbnail" if kind == "thumbnail" else "vertical"]
    left, top = round(width * safe["left"]), round(height * safe["top"])
    right, bottom = (
        round(width * (1 - safe["right"])),
        round(height * (1 - safe["bottom"])),
    )
    if kind == "thumbnail":
        if layout == "side":
            return left, top + round(height * 0.12), round(width * 0.58), bottom
        if layout == "band":
            # Stay clear of the duration badge in the bottom-right corner.
            return (
                left,
                round(height * 0.56),
                round(width * (1 - safe["badge_w"])),
                bottom,
            )
        return (
            left + round(width * 0.06),
            top + round(height * 0.1),
            right - round(width * 0.06),
            bottom - round(height * 0.1),
        )
    if layout == "band":
        return left, round(height * 0.5), right, bottom
    if layout == "side":
        return left, round(height * 0.2), right, round(height * 0.5)
    return left, round(height * 0.32), right, round(height * 0.62)


def _wrap(
    words: list[str], font: ImageFont.FreeTypeFont, max_width: int
) -> list[list[str]] | None:
    lines: list[list[str]] = [[]]
    space = font.getlength(" ")
    used = 0.0
    for word in words:
        length = font.getlength(word)
        if length > max_width:
            return None
        if lines[-1] and used + space + length > max_width:
            lines.append([word])
            used = length
        else:
            used = used + (space if lines[-1] else 0) + length
            lines[-1].append(word)
    return lines


def _fit(
    words: list[str], family: str, box: tuple[int, int, int, int], max_lines: int
) -> tuple[ImageFont.FreeTypeFont, list[list[str]], int]:
    box_w, box_h = box[2] - box[0], box[3] - box[1]
    low, high = 18, max(24, box_h)
    best: tuple[ImageFont.FreeTypeFont, list[list[str]], int] | None = None
    while low <= high:
        size = (low + high) // 2
        font = ImageFont.truetype(str(font_path(family)), size)
        line_height = round(size * 1.08)
        lines = _wrap(words, font, box_w)
        if (
            lines is not None
            and len(lines) <= max_lines
            and len(lines) * line_height <= box_h
        ):
            best = (font, lines, line_height)
            low = size + 1
        else:
            high = size - 1
    if best is None:
        font = ImageFont.truetype(str(font_path(family)), 18)
        return font, _wrap(words, font, 10**6) or [words], 20
    return best


def compose(
    spec: dict[str, Any],
    dest: Path,
    *,
    background: Image.Image | None = None,
    logo: Image.Image | None = None,
) -> list[Check]:
    """Render ``spec`` to ``dest`` (JPEG) and return the checks measured on the result."""
    kind = spec.get("kind", "thumbnail")
    size = DESIGN_SIZES[kind]
    width, height = size
    layout = spec.get("layout") if spec.get("layout") in LAYOUTS else "side"
    colors = {
        **DEFAULT_COLORS,
        **{k: v for k, v in (spec.get("colors") or {}).items() if v},
    }
    family = spec.get("font") if spec.get("font") in FONT_FILES else DEFAULT_FONT
    headline = " ".join(str(spec.get("headline") or "").split())

    base = (
        _cover(background.convert("RGB"), size)
        if background is not None
        else _gradient_background(size, colors)
    )
    canvas = Image.alpha_composite(base.convert("RGBA"), _scrim(size, layout, kind))
    draw = ImageDraw.Draw(canvas)
    checks: list[Check] = []

    box = _text_box(size, kind, layout)
    text_bbox: tuple[int, int, int, int] | None = None
    if headline:
        words = headline.split(" ")
        font, lines, line_height = _fit(
            words, family, box, 3 if kind == "thumbnail" else 5
        )
        block_h = len(lines) * line_height
        y = (
            box[3] - block_h
            if layout == "band"
            else box[1] + (box[3] - box[1] - block_h) // 2
        )
        accent_word = max(words, key=len) if len(words) > 1 else None
        text_rgb, accent_rgb = _rgb(colors["text"]), _rgb(colors["accent"])
        stroke = max(2, font.size // 16)
        space = font.getlength(" ")
        min_x, max_x, top_y = width, 0, y
        # Accent bar above the block ties the design to the brand colour.
        bar_h = max(6, height // 90)
        bar_w = round(min(box[2] - box[0], font.size * 2.2))
        bar_x = box[0] if layout != "center" else (box[0] + box[2] - bar_w) // 2
        draw.rounded_rectangle(
            (bar_x, y - bar_h * 3, bar_x + bar_w, y - bar_h * 2),
            radius=bar_h // 2,
            fill=_rgb(colors["primary"]),
        )
        for line in lines:
            line_w = sum(font.getlength(w) for w in line) + space * (len(line) - 1)
            x = (
                box[0]
                if layout != "center"
                else box[0] + (box[2] - box[0] - line_w) / 2
            )
            min_x, max_x = min(min_x, round(x)), max(max_x, round(x + line_w))
            for word in line:
                fill = accent_rgb if word == accent_word else text_rgb
                draw.text(
                    (x + stroke, y + stroke * 2),
                    word,
                    font=font,
                    fill=(0, 0, 0),
                    stroke_width=stroke,
                    stroke_fill=(0, 0, 0),
                )
                draw.text(
                    (x, y),
                    word,
                    font=font,
                    fill=fill,
                    stroke_width=stroke,
                    stroke_fill=(8, 10, 20),
                )
                x += font.getlength(word) + space
            y += line_height
        text_bbox = (min_x, top_y, max_x, y)
        region = base.crop(text_bbox).filter(ImageFilter.BoxBlur(4))
        scrimmed = Image.alpha_composite(
            region.convert("RGBA"), _scrim(size, layout, kind).crop(text_bbox)
        )
        mean = tuple(round(v) for v in ImageStat.Stat(scrimmed.convert("RGB")).mean[:3])
        ratio = contrast_ratio(text_rgb, mean)  # type: ignore[arg-type]
        checks.append(
            check(
                "contrast",
                "pass" if ratio >= 4.5 else "warn",
                "Contraste del texto",
                f"{decimal(ratio)}:1 entre el texto y el fondo medido bajo el titular (recomendado ≥ 4,5:1).",
            )
        )
        relative = font.size / height
        minimum = 0.09 if kind == "thumbnail" else 0.035
        checks.append(
            check(
                "legibility",
                "pass" if relative >= minimum else "warn",
                "Tamaño del texto",
                f"Altura de letra: {decimal(relative * 100)} % del alto"
                + (
                    ""
                    if relative >= minimum
                    else ". Demasiado pequeño para pantallas móviles: acorta el titular."
                ),
            )
        )
    else:
        checks.append(check("headline", "warn", "Titular", "El diseño no tiene texto."))

    if logo is not None:
        max_w, max_h = (
            round(width * 0.2),
            round(height * (0.11 if kind == "thumbnail" else 0.06)),
        )
        mark = logo.convert("RGBA")
        mark.thumbnail((max_w, max_h), Image.LANCZOS)
        safe = SAFE_ZONES["thumbnail" if kind == "thumbnail" else "vertical"]
        position = (
            round(width * (1 - safe["right"])) - mark.width,
            round(height * safe["top"]),
        )
        if kind == "thumbnail" and layout != "side":
            position = (round(width * safe["left"]), round(height * safe["top"]))
        canvas.alpha_composite(mark, position)

    if text_bbox is not None:
        checks.append(_safe_zone_check(text_bbox, size, kind))

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dest, "JPEG", quality=90, optimize=True)
    size_mb = dest.stat().st_size / 1_048_576
    if kind == "thumbnail":
        checks.append(
            check(
                "file_size",
                "pass" if size_mb <= 2 else "fail",
                "Peso del archivo",
                f"{decimal(size_mb, 2)} MB (YouTube admite miniaturas de hasta 2 MB).",
            )
        )
    checks.append(check("dimensions", "pass", "Dimensiones", f"{width}×{height} px"))
    return checks


def _safe_zone_check(
    bbox: tuple[int, int, int, int], size: tuple[int, int], kind: str
) -> Check:
    width, height = size
    safe = SAFE_ZONES["thumbnail" if kind == "thumbnail" else "vertical"]
    left, top = width * safe["left"], height * safe["top"]
    right, bottom = width * (1 - safe["right"]), height * (1 - safe["bottom"])
    inside = (
        bbox[0] >= left - 1
        and bbox[1] >= top - 1
        and bbox[2] <= right + 1
        and bbox[3] <= bottom + 1
    )
    if kind == "thumbnail":
        badge_left, badge_top = (
            width * (1 - safe["badge_w"]),
            height * (1 - safe["badge_h"]),
        )
        if bbox[2] > badge_left and bbox[3] > badge_top:
            return check(
                "safe_zone",
                "fail",
                "Zona segura",
                "El titular invade la esquina donde YouTube muestra la duración.",
            )
    if inside:
        return check(
            "safe_zone",
            "pass",
            "Zona segura",
            "El titular queda dentro de la zona segura (márgenes aproximados de la plataforma).",
        )
    return check(
        "safe_zone",
        "fail",
        "Zona segura",
        "El titular se sale de la zona segura: la interfaz de la plataforma puede taparlo.",
    )
