"""Subtitle parsing (SRT / WebVTT) and generation (SRT / ASS for burn-in)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_ARROW = re.compile(r"\s*-->\s*")
_TAGS = re.compile(r"<[^>]+>|\{[^}]*\}")


def _seconds(stamp: str) -> float | None:
    match = _TIME.search(stamp)
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    return (
        int(hours or 0) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000
    )


def parse(text: str) -> list[Segment]:
    """Parse SRT or WebVTT; unknown blocks are skipped rather than guessed."""
    segments: list[Segment] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", "").strip())
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        parts = _ARROW.split(lines[timing_index], maxsplit=1)
        if len(parts) != 2:
            continue
        start, end = _seconds(parts[0]), _seconds(parts[1])
        body = " ".join(
            _TAGS.sub("", line).strip() for line in lines[timing_index + 1 :]
        ).strip()
        if start is None or end is None or end <= start or not body:
            continue
        segments.append(Segment(start, end, body))
    return sorted(segments, key=lambda s: s.start)


def window(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """Segments overlapping ``[start, end]``, re-timed so the window starts at 0."""
    out: list[Segment] = []
    for seg in segments:
        if seg.end <= start or seg.start >= end:
            continue
        out.append(
            Segment(max(0.0, seg.start - start), min(end, seg.end) - start, seg.text)
        )
    return out


def excerpt(segments: list[Segment], start: float, end: float, limit: int = 240) -> str:
    text = " ".join(seg.text for seg in window(segments, start, end))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _srt_time(value: float) -> str:
    millis = round(value * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def to_srt(segments: list[Segment]) -> str:
    return "\n".join(
        f"{index}\n{_srt_time(seg.start)} --> {_srt_time(seg.end)}\n{seg.text}\n"
        for index, seg in enumerate(segments, start=1)
    )


def _ass_time(value: float) -> str:
    centis = round(value * 100)
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6000)
    seconds, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def to_ass(
    segments: list[Segment],
    *,
    size: tuple[int, int],
    font_name: str,
    safe: dict[str, float],
) -> str:
    """ASS script whose margins keep captions inside the platform safe zone."""
    width, height = size
    font_size = round(height * (0.034 if height > width else 0.052))
    margin_l = round(width * safe.get("left", 0.05))
    margin_r = round(width * safe.get("right", 0.05))
    margin_v = round(height * safe.get("bottom", 0.06))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,"
        f"1,{max(3, font_size // 13)},1,2,{margin_l},{margin_r},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for seg in segments:
        text = (
            seg.text.replace("\\", "⧵")
            .replace("{", "(")
            .replace("}", ")")
            .replace("\n", " ")
        )
        lines.append(
            f"Dialogue: 0,{_ass_time(seg.start)},{_ass_time(seg.end)},Default,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + "\n"
