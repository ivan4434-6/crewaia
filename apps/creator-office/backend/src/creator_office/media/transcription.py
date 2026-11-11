"""Optional speech-to-text connector (OpenAI-compatible ``/audio/transcriptions``).

Disabled unless ``TRANSCRIPTION_MODEL`` and ``TRANSCRIPTION_API_KEY`` are
configured. Without it the Clipper works from audio and picture signals, or
from subtitles the user uploads.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx

from creator_office.media import ffmpeg
from creator_office.net import http_client
from creator_office.settings import get_settings
from creator_office.text import shorten

CHUNK_SECONDS = (
    20 * 60
)  # ~7 MB of 48 kbps mono MP3, well under the usual 25 MB API limit


class TranscriptionError(RuntimeError):
    pass


def status() -> dict[str, Any]:
    settings = get_settings()
    wanted = (
        ("TRANSCRIPTION_MODEL", settings.transcription_model),
        ("TRANSCRIPTION_API_KEY", settings.transcription_api_key),
    )
    missing = [name for name, value in wanted if not value]
    return {
        "configured": not missing,
        "model": settings.transcription_model,
        "missing": missing,
        "alternative": "Sube un archivo de subtítulos (.srt o .vtt) junto al vídeo, o deja que Clipper trabaje solo con las señales de audio e imagen.",
    }


def transcribe(
    video: Path,
    *,
    duration: float,
    language: str = "",
    cancel: threading.Event | None = None,
    on_progress: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Return ``[{start, end, text}]`` for the whole file, chunked for long videos.

    An empty list means the service heard no speech. Anything that keeps the
    answer from being trusted raises ``TranscriptionError``: the caller then
    works without a transcript instead of with a made-up one.
    """
    settings = get_settings()
    if not status()["configured"]:
        raise TranscriptionError("La transcripción no está configurada.")
    # Only this service's own key travels to it: never the key of the AI provider.
    headers = {"Authorization": f"Bearer {settings.transcription_api_key}"}
    url = settings.transcription_base_url.rstrip("/") + "/audio/transcriptions"
    segments: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory(prefix="co-stt-") as tmp,
        http_client(timeout=600, transport=transport) as client,
    ):
        audio = Path(tmp) / "audio.mp3"
        offsets = [float(s) for s in range(0, max(1, int(duration)), CHUNK_SECONDS)]
        try:
            ffmpeg.extract_audio(video, audio, cancel=cancel)
            pieces = [audio]
            if len(offsets) > 1:
                pieces = [
                    Path(tmp) / f"part{index}.mp3" for index in range(len(offsets))
                ]
                for piece, offset in zip(pieces, offsets, strict=True):
                    ffmpeg._run(
                        [
                            "-ss",
                            f"{offset:.0f}",
                            "-t",
                            str(CHUNK_SECONDS),
                            "-i",
                            str(audio),
                            "-c",
                            "copy",
                            str(piece),
                        ],
                        cancel=cancel,
                    )
        except ffmpeg.FFmpegError as exc:
            raise TranscriptionError(
                "No se pudo extraer el audio del vídeo para transcribirlo."
            ) from exc
        for index, (piece, offset) in enumerate(zip(pieces, offsets, strict=True)):
            if cancel is not None and cancel.is_set():
                raise ffmpeg.Cancelled("Tarea cancelada.")
            data = {
                "model": settings.transcription_model,
                "response_format": "verbose_json",
            }
            if language:
                data["language"] = language
            try:
                with piece.open("rb") as handle:
                    response = client.post(
                        url,
                        headers=headers,
                        data=data,
                        files={"file": (piece.name, handle, "audio/mpeg")},
                    )
            except httpx.HTTPError as exc:
                raise TranscriptionError(
                    f"No se pudo conectar con el servicio de transcripción ({type(exc).__name__})."
                ) from exc
            if response.status_code >= 400:
                raise TranscriptionError(
                    f"El servicio de transcripción respondió {response.status_code}: {shorten(response.text, 200)}"
                )
            segments += _timed_lines(response, offset)
            if on_progress:
                on_progress((index + 1) / len(offsets))
    return segments


def _timed_lines(response: httpx.Response, offset: float) -> list[dict[str, Any]]:
    """The lines of one answer, moved to their place in the whole video."""
    try:
        payload = response.json()
        parts = payload.get("segments")
        if parts is None and str(payload.get("text") or "").strip():
            # Words without times cannot be matched to moments of the video.
            raise TranscriptionError(
                "El servicio devolvió texto sin marcas de tiempo; se necesita un modelo que admita response_format=verbose_json."
            )
        return [
            {
                "start": round(float(part["start"]) + offset, 2),
                "end": round(float(part["end"]) + offset, 2),
                "text": text,
            }
            for part in parts or []
            if (text := str(part.get("text") or "").strip())
        ]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise TranscriptionError(
            "El servicio de transcripción devolvió una respuesta que no se puede leer."
        ) from exc
