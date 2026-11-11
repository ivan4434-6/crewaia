"""Clip candidates from measured signals.

Every candidate starts and ends on something that was actually detected in
the file (a pause, a scene change, a subtitle boundary), and carries the
numbers behind its ranking so the suggestion can be explained without
guessing what is being said or shown.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from typing import Any

from creator_office.media.ffmpeg import FRAME_SECONDS, Signals
from creator_office.media.subtitles import Segment, excerpt


@dataclass
class Candidate:
    id: str
    start: float
    end: float
    score: float
    signals: dict[str, Any] = field(default_factory=dict)
    excerpt: str = ""

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 2)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration"] = self.duration
        return data


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _loudness_between(signals: Signals, start: float, end: float) -> list[float]:
    first = max(0, int(start / FRAME_SECONDS))
    last = min(len(signals.loudness), max(first + 1, int(end / FRAME_SECONDS)))
    return signals.loudness[first:last]


def _quiet_seconds(signals: Signals, start: float, end: float) -> float:
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e in signals.silences)


def build_candidates(
    signals: Signals,
    *,
    min_seconds: float,
    max_seconds: float,
    limit: int,
    transcript: list[Segment] | None = None,
) -> tuple[list[Candidate], str]:
    """Return ranked, mostly non-overlapping candidates and the method used."""
    duration = signals.duration
    transcript = transcript or []
    if duration <= min_seconds:
        only = Candidate(
            "c1",
            0.0,
            round(duration, 2),
            0.0,
            {"method": "full"},
            excerpt(transcript, 0, duration),
        )
        return [only], "full"

    starts: dict[float, dict[str, Any]] = {0.0: {"kind": "inicio"}}
    ends: dict[float, dict[str, Any]] = {round(duration, 2): {"kind": "final"}}
    for s, e in signals.silences:
        if e < duration - 1:
            starts[round(max(0.0, e - 0.1), 2)] = {
                "kind": "pausa",
                "pause": round(e - s, 2),
            }
        if s > 1:
            ends[round(min(duration, s + 0.2), 2)] = {
                "kind": "pausa",
                "pause": round(e - s, 2),
            }
    for cut in signals.scene_cuts:
        if 1 < cut < duration - 1:
            starts.setdefault(round(cut, 2), {"kind": "plano"})
            ends.setdefault(round(cut, 2), {"kind": "plano"})
    for seg in transcript:
        if seg.start < duration - 1:
            starts.setdefault(round(seg.start, 2), {"kind": "frase"})
        if seg.end > 1:
            ends.setdefault(round(min(duration, seg.end), 2), {"kind": "frase"})

    detected = len(starts) + len(ends) > 2
    if not detected:
        return _uniform(signals, min_seconds, max_seconds, limit), "uniform"

    overall = _mean(signals.loudness) if signals.loudness else 0.0
    start_points, end_points = sorted(starts), sorted(ends)
    scored: list[Candidate] = []
    for p in start_points:
        low = bisect_left(end_points, p + min_seconds)
        high = bisect_right(end_points, p + max_seconds)
        for q in end_points[low:high]:
            window = _loudness_between(signals, p, q)
            delta = round(_mean(window) - overall, 1) if window else 0.0
            quiet = _quiet_seconds(signals, p, q) / (q - p)
            cuts = sum(1 for c in signals.scene_cuts if p + 0.5 < c < q - 0.5)
            start_info, end_info = starts[p], ends[q]
            score = max(-1.0, min(1.0, delta / 6))
            score += {"pausa": 0.5, "frase": 0.35, "plano": 0.3, "inicio": 0.1}.get(
                start_info["kind"], 0
            )
            score += {"pausa": 0.5, "frase": 0.35, "plano": 0.3, "final": 0.1}.get(
                end_info["kind"], 0
            )
            score -= quiet * 1.2
            score += 0.1 * min(cuts, 3)
            scored.append(
                Candidate(
                    id="",
                    start=p,
                    end=q,
                    score=round(score, 3),
                    signals={
                        "method": "signals",
                        "start": start_info,
                        "end": end_info,
                        "loudness_delta_db": delta if signals.loudness else None,
                        "quiet_ratio": round(quiet, 2),
                        "scene_cuts": cuts,
                    },
                )
            )
    if not scored:
        return _uniform(signals, min_seconds, max_seconds, limit), "uniform"

    scored.sort(key=lambda c: (-c.score, c.start))
    chosen: list[Candidate] = []
    for candidate in scored:
        if all(_overlap(candidate, other) < 0.3 for other in chosen):
            chosen.append(candidate)
            if len(chosen) >= limit:
                break
    for index, candidate in enumerate(chosen, start=1):
        candidate.id = f"c{index}"
        candidate.signals["rank"] = index
        candidate.excerpt = excerpt(transcript, candidate.start, candidate.end)
    return chosen, "signals"


def _overlap(a: Candidate, b: Candidate) -> float:
    shared = min(a.end, b.end) - max(a.start, b.start)
    if shared <= 0:
        return 0.0
    return shared / min(a.duration, b.duration)


def _uniform(
    signals: Signals, min_seconds: float, max_seconds: float, limit: int
) -> list[Candidate]:
    duration = signals.duration
    length = min(max_seconds, max(min_seconds, duration / max(1, limit)))
    out: list[Candidate] = []
    position = 0.0
    while position + min_seconds <= duration and len(out) < limit:
        end = min(duration, position + length)
        out.append(
            Candidate(
                f"c{len(out) + 1}",
                round(position, 2),
                round(end, 2),
                0.0,
                {"method": "uniform", "rank": len(out) + 1},
            )
        )
        position = end
    return out


def describe(candidate: dict[str, Any], language: str = "es") -> str:
    """Plain-language account of the measured signals behind a candidate."""
    signals = candidate.get("signals", {})
    english = language == "en"
    method = signals.get("method")
    if method == "full":
        return (
            "The video is shorter than the minimum clip length, so it is used whole."
            if english
            else ("El vídeo dura menos que el mínimo por clip: se propone entero.")
        )
    if method == "uniform":
        return (
            "No pauses, scene changes or subtitles were detected, so the video was split evenly. Review the cut points."
            if english
            else "No se detectaron pausas, cambios de plano ni subtítulos: el vídeo se dividió en tramos iguales. Revisa los puntos de corte."
        )
    parts: list[str] = []
    start, end = signals.get("start", {}), signals.get("end", {})
    if english:
        start_text = {
            "pausa": f"starts right after a {start.get('pause', 0):g} s pause",
            "plano": "starts on a scene change",
            "frase": "starts at the beginning of a transcript line",
            "inicio": "starts at the beginning of the video",
        }
        end_text = {
            "pausa": "ends on a pause",
            "plano": "ends on a scene change",
            "frase": "ends at the end of a transcript line",
            "final": "ends with the video",
        }
    else:
        start_text = {
            "pausa": f"empieza justo después de una pausa de {start.get('pause', 0):g} s".replace(
                ".", ","
            ),
            "plano": "empieza en un cambio de plano",
            "frase": "empieza al inicio de una frase de la transcripción",
            "inicio": "empieza al principio del vídeo",
        }
        end_text = {
            "pausa": "termina en una pausa",
            "plano": "termina en un cambio de plano",
            "frase": "termina al final de una frase de la transcripción",
            "final": "termina con el vídeo",
        }
    if start.get("kind") in start_text:
        parts.append(start_text[start["kind"]])
    if end.get("kind") in end_text:
        parts.append(end_text[end["kind"]])
    delta = signals.get("loudness_delta_db")
    if delta is not None and abs(delta) >= 1:
        number = f"{abs(delta):.1f}"
        if english:
            parts.append(
                f"average volume {number} dB {'above' if delta > 0 else 'below'} the rest of the video"
            )
        else:
            parts.append(
                f"volumen medio {number.replace('.', ',')} dB por {'encima' if delta > 0 else 'debajo'} del resto del vídeo"
            )
    cuts = signals.get("scene_cuts", 0)
    if cuts:
        parts.append(
            f"{cuts} scene change{'s' if cuts != 1 else ''} inside"
            if english
            else f"{cuts} cambio{'s' if cuts != 1 else ''} de plano en su interior"
        )
    quiet = signals.get("quiet_ratio", 0)
    if quiet >= 0.25:
        parts.append(
            f"{round(quiet * 100)}% of it is quiet"
            if english
            else f"un {round(quiet * 100)} % del tramo es silencio"
        )
    text = "; ".join(parts)
    return (text[:1].upper() + text[1:] + ".") if text else ""
