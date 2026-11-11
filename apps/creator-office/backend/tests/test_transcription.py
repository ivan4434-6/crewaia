"""The optional speech-to-text service: what is sent to it and what is believed from it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from creator_office.media import transcription
from tests.helpers import Api, drain
from tests.test_pipeline_api import PROJECT, steps, upload

LLM_KEY = "sk-clave-del-proveedor-de-ia"


def test_only_its_own_key_is_sent_and_the_times_it_gave_are_kept(
    settings_env: Callable[..., None], sample_video: Path
) -> None:
    requests: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "text": "Hoy te cuento cómo planifico una semana de vídeos.",
                "segments": [
                    {
                        "start": 0.5,
                        "end": 5.5,
                        "text": " Hoy te cuento cómo planifico una semana de vídeos. ",
                    },
                    {
                        "start": 8.0,
                        "end": 9.0,
                        "text": "   ",
                    },  # a line with no words is not a line
                ],
            },
        )

    # A model alone is not enough, and the AI provider's key is never borrowed for another service.
    settings_env(
        TRANSCRIPTION_MODEL="whisper-1",
        TRANSCRIPTION_BASE_URL="https://stt.example/v1",
        LLM_MODEL="anthropic/claude-sonnet-4-5",
        LLM_API_KEY=LLM_KEY,
    )
    assert transcription.status()["configured"] is False and transcription.status()[
        "missing"
    ] == ["TRANSCRIPTION_API_KEY"]
    with pytest.raises(transcription.TranscriptionError, match="no está configurada"):
        transcription.transcribe(
            sample_video, duration=24, transport=httpx.MockTransport(service)
        )
    assert requests == []

    settings_env(TRANSCRIPTION_API_KEY="sk-solo-transcripcion")
    assert transcription.status() == {
        **transcription.status(),
        "configured": True,
        "model": "whisper-1",
        "missing": [],
    }
    lines = transcription.transcribe(
        sample_video, duration=24, language="es", transport=httpx.MockTransport(service)
    )
    assert lines == [
        {
            "start": 0.5,
            "end": 5.5,
            "text": "Hoy te cuento cómo planifico una semana de vídeos.",
        }
    ]

    (request,) = requests
    assert str(request.url) == "https://stt.example/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sk-solo-transcripcion"
    body = request.read()
    for field in (
        b'name="model"\r\n\r\nwhisper-1',
        b'name="response_format"\r\n\r\nverbose_json',
        b'name="language"\r\n\r\nes',
        b'filename="audio.mp3"',
    ):
        assert field in body
    assert len(body) > 20_000  # the audio of the video really travelled
    assert LLM_KEY.encode() not in body and LLM_KEY not in str(request.headers)


def _answer(status: int = 200, **kwargs: Any) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, **kwargs))


def test_an_answer_that_cannot_be_trusted_is_an_error_and_silence_is_not(
    settings_env: Callable[..., None], sample_video: Path
) -> None:
    settings_env(
        TRANSCRIPTION_MODEL="whisper-1", TRANSCRIPTION_API_KEY="sk-solo-transcripcion"
    )

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    cases = {
        "respondió 401": _answer(
            401, json={"error": {"message": "Incorrect API key provided."}}
        ),
        "sin marcas de tiempo": _answer(
            json={"text": "Hola a todos."}
        ),  # words without times cannot be placed in the video
        "no se puede leer": _answer(text="<html>502 Bad Gateway</html>"),
        "no se puede leer ": _answer(
            json={"segments": [{"text": "Hola", "start": "pronto"}]}
        ),
        "No se pudo conectar": httpx.MockTransport(refused),
    }
    for message, transport in cases.items():
        with pytest.raises(transcription.TranscriptionError, match=message.strip()):
            transcription.transcribe(sample_video, duration=24, transport=transport)

    # A video nobody speaks in is a valid answer, not a failure.
    assert (
        transcription.transcribe(
            sample_video,
            duration=24,
            transport=_answer(json={"text": "", "segments": []}),
        )
        == []
    )
    assert (
        transcription.transcribe(
            sample_video, duration=24, transport=_answer(json={"text": ""})
        )
        == []
    )


def test_long_videos_are_sent_in_parts_and_each_line_lands_at_its_real_time(
    settings_env: Callable[..., None],
    sample_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(
        TRANSCRIPTION_MODEL="whisper-1", TRANSCRIPTION_API_KEY="sk-solo-transcripcion"
    )
    monkeypatch.setattr(
        transcription, "CHUNK_SECONDS", 10
    )  # the 24 s sample becomes three parts
    names: list[bytes] = []
    progress: list[float] = []

    def service(request: httpx.Request) -> httpx.Response:
        body = request.read()
        names.append(
            next(
                name
                for name in (b"part0.mp3", b"part1.mp3", b"part2.mp3")
                if name in body
            )
        )
        return httpx.Response(
            200,
            json={
                "text": "x",
                "segments": [{"start": 1.0, "end": 3.0, "text": f"Parte {len(names)}"}],
            },
        )

    lines = transcription.transcribe(
        sample_video,
        duration=24,
        on_progress=progress.append,
        transport=httpx.MockTransport(service),
    )
    assert names == [b"part0.mp3", b"part1.mp3", b"part2.mp3"]
    assert [(line["start"], line["end"], line["text"]) for line in lines] == [
        (1.0, 3.0, "Parte 1"),
        (11.0, 13.0, "Parte 2"),
        (21.0, 23.0, "Parte 3"),
    ]
    assert progress == pytest.approx([1 / 3, 2 / 3, 1.0])


def _project_with_video(api: Api, sample_video: Path) -> str:
    pid = api.post("/api/projects", PROJECT)["id"]
    drain()
    angle = api.get(f"/api/projects/{pid}")["documents"]["research"]["version"][
        "content"
    ]["angles"][0]["title"]
    api.step(pid, "research", "approve", chosen_angle=angle)
    source = upload(api, pid, "source", "semana.mp4", sample_video.read_bytes())[
        "project"
    ]["source"]
    api.patch(f"/api/assets/{source['id']}", {"rights_declared": True})
    return pid


def test_clipper_uses_the_transcription_once_and_says_what_it_got(
    api: Api,
    sample_video: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(
        TRANSCRIPTION_MODEL="whisper-1", TRANSCRIPTION_API_KEY="sk-solo-transcripcion"
    )
    answers: list[Any] = [
        transcription.TranscriptionError(
            "El servicio de transcripción respondió 503: inténtalo más tarde"
        ),
        [],  # the second time it works, and hears nobody speaking
    ]
    calls: list[str] = []

    def fake(
        video: Path, *, duration: float, language: str = "", **_: Any
    ) -> list[dict[str, Any]]:
        calls.append(language)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(transcription, "transcribe", fake)
    pid = _project_with_video(api, sample_video)
    drain()

    # A failure is told as a failure, and Clipper still works from the measured signals.
    detail = api.get(f"/api/projects/{pid}")
    clips = steps(detail)["clips"]
    assert clips["status"] == "awaiting_approval" and calls == ["es"]
    assert clips["output"]["transcript"] == {
        "available": False,
        "source": "",
        "note": "La transcripción falló: El servicio de transcripción respondió 503: inténtalo más tarde",
    }
    assert detail["source"]["transcript"] == {
        "available": False,
        "source": "",
        "segments": 0,
    }
    assert [c for c in detail["clips"] if c["status"] == "selected"]

    # Nothing was stored, so the next run asks again. This time the service hears no speech.
    api.step(pid, "clips", "rerun")
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert len(calls) == 2
    assert steps(detail)["clips"]["output"]["transcript"]["note"].startswith(
        "La transcripción automática no detectó voz en este vídeo"
    )
    assert detail["source"]["transcript"] == {
        "available": False,
        "source": "api",
        "segments": 0,
    }

    # That answer is kept: running Clipper again does not pay for the same audio twice.
    api.step(pid, "clips", "rerun")
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert len(calls) == 2
    assert steps(detail)["clips"]["output"]["transcript"]["note"].startswith(
        "La transcripción automática no detectó voz en este vídeo"
    )


def test_clipper_names_clips_after_what_the_transcription_heard(
    api: Api,
    sample_video: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(
        TRANSCRIPTION_MODEL="whisper-1", TRANSCRIPTION_API_KEY="sk-solo-transcripcion"
    )
    heard = [
        {
            "start": 0.5,
            "end": 5.5,
            "text": "Hoy te cuento cómo planifico una semana de vídeos.",
        },
        {
            "start": 8.5,
            "end": 13.5,
            "text": "El truco es grabar por bloques y editar después.",
        },
        {
            "start": 16.5,
            "end": 21.5,
            "text": "Suscríbete si quieres ver la plantilla completa.",
        },
    ]
    monkeypatch.setattr(transcription, "transcribe", lambda video, **_: heard)
    pid = _project_with_video(api, sample_video)
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert steps(detail)["clips"]["output"]["transcript"] == {
        "available": True,
        "source": "api",
        "note": "",
    }
    assert detail["source"]["transcript"] == {
        "available": True,
        "source": "api",
        "segments": 3,
    }
    (clip,) = [c for c in detail["clips"] if c["status"] == "selected"]
    assert any(
        line["text"][:20] in clip["excerpt"] for line in heard
    )  # the clip quotes words that were really heard
    # Its working title is the start of those words, and says so when it was cut short.
    words = clip["excerpt"].split()
    assert clip["title"].startswith(" ".join(words[:6])) and clip["title"].endswith(
        "…"
    ) == (len(words) > 7)
