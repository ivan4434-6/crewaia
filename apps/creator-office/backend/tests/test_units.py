"""Small, fast checks of the rules the app relies on."""

from __future__ import annotations

import mimetypes
import re
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from creator_office import security
from creator_office.agents.steps import count_placeholders
from creator_office.connectors import platform_specs as specs
from creator_office.database import session_scope
from creator_office.media import subtitles, thumbnails
from creator_office.models import Render, Run, Step
from creator_office.pipeline import commands, worker
from creator_office.services import community
from creator_office.settings import APP_ROOT, Settings, get_settings
from tests.helpers import SRT, Api, drain


def test_passwords_and_secrets() -> None:
    stored = security.hash_password("una contraseña larga")
    assert stored.startswith("scrypt$") and "una contraseña" not in stored
    assert security.verify_password("una contraseña larga", stored)
    assert not security.verify_password(
        "otra", stored
    ) and not security.verify_password("x", "not-a-hash")
    blob = security.encrypt_json({"access_token": "muy-secreto"})
    assert "muy-secreto" not in blob and security.decrypt_json(blob) == {
        "access_token": "muy-secreto"
    }
    assert security.mask_secret("sk-1234567890abcd") == "••••abcd"
    assert security.derive_key("a") != security.derive_key("b")


def test_session_cookie_for_embedded_previews(monkeypatch: pytest.MonkeyPatch) -> None:
    from creator_office.settings import get_settings

    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "none")
    monkeypatch.setenv("SESSION_COOKIE_PARTITIONED", "true")
    get_settings.cache_clear()
    cookie = security.build_session_cookie("tok", secure_request=False)
    assert (
        "SameSite=None" in cookie
        and "Secure" in cookie
        and "Partitioned" in cookie
        and "HttpOnly" in cookie
    )


def test_subtitle_parsing_and_windows() -> None:
    segments = subtitles.parse(SRT)
    assert [round(s.start, 1) for s in segments] == [0.5, 8.2, 16.3]
    window = subtitles.window(segments, 8.0, 14.0)
    assert len(window) == 1 and window[0].start == pytest.approx(
        0.2, abs=0.01
    )  # re-timed to the clip
    assert "bloques" in subtitles.excerpt(segments, 8.0, 14.0)
    vtt = "WEBVTT\n\n00:01.000 --> 00:03.000\n<v Ana>Hola <b>mundo</b>\n"
    assert subtitles.parse(vtt)[0].text == "Hola mundo"
    assert subtitles.parse("sin marcas de tiempo") == []


def test_platform_rules() -> None:
    def by_id(checks: list[dict]) -> dict[str, str]:
        return {c["id"]: c["status"] for c in checks}

    vertical = {
        "duration": 30.0,
        "video": {"width": 1080, "height": 1920, "codec": "h264"},
        "audio": {"codec": "aac"},
    }
    assert by_id(specs.validate_video("tiktok", vertical, 10_000_000)) == {
        "duration": "pass",
        "aspect": "pass",
    }
    too_long = {**vertical, "duration": 4000.0}
    assert by_id(specs.validate_video("youtube_shorts", too_long))["duration"] == "fail"
    assert by_id(specs.validate_video("tiktok", None))["video"] == "fail"
    assert by_id(specs.validate_metadata("youtube", "", "desc", []))["title"] == "fail"
    assert (
        by_id(specs.validate_metadata("youtube", "T" * 101, "desc", []))["title"]
        == "fail"
    )
    assert (
        by_id(specs.validate_metadata("instagram_reels", "", "#a " * 31, []))[
            "hashtags"
        ]
        == "fail"
    )
    assert (
        by_id(specs.validate_metadata("youtube", "Título con [dato]", "desc", []))[
            "placeholders"
        ]
        == "fail"
    )
    assert (
        specs.overall(
            specs.validate_metadata(
                "youtube", "Un título normal", "Una descripción.", ["uno"]
            )
        )
        == "pass"
    )


def test_thumbnail_checks_are_measured(tmp_path: Path) -> None:
    out = tmp_path / "mini.jpg"
    checks = thumbnails.compose(
        {
            "kind": "thumbnail",
            "headline": "TRES LUCES BARATAS",
            "layout": "side",
            "font": "Anton",
            "background": "gradient",
        },
        out,
    )
    with Image.open(out) as image:
        assert image.size == (1280, 720)
    by_id = {c["id"]: c for c in checks}
    assert by_id["contrast"]["status"] == "pass" and ":1" in by_id["contrast"]["detail"]
    assert (
        by_id["safe_zone"]["status"] == "pass"
        and by_id["file_size"]["status"] == "pass"
    )
    # Dark text over the dark scrim is measured and reported, not waved through.
    dark = {
        "kind": "thumbnail",
        "headline": "ILEGIBLE",
        "layout": "center",
        "font": "Anton",
        "background": "gradient",
        "colors": {"text": "#151515"},
    }
    measured = {c["id"]: c for c in thumbnails.compose(dark, tmp_path / "dark.jpg")}
    assert (
        measured["contrast"]["status"] == "warn"
        and measured["contrast"]["detail"] != by_id["contrast"]["detail"]
    )


def test_comment_rules_explain_themselves() -> None:
    assert community.classify("¿Dónde compraste el micro?")[0] == "question"
    category, reason = community.classify(
        "Gana dinero fácil, escríbeme por Telegram www.x.xyz"
    )
    assert category == "spam" and "enlace" in reason
    assert community.classify("El audio no se escucha en el minuto 2")[0] == "issue"
    assert community.classify("Muy útil, gracias")[0] == "positive"
    assert (
        community.classify("Muy buen vídeo, me ha servido un montón")[0] == "positive"
    )
    assert community.classify("primero")[0] == "other"
    rows = community.parse_import("autor;texto\nlucia;Buen vídeo\nmarco;¿Y la parte 2?")
    assert rows == [
        {"author": "lucia", "text": "Buen vídeo", "posted_at": ""},
        {"author": "marco", "text": "¿Y la parte 2?", "posted_at": ""},
    ]


def test_order_console_runs_engine_actions_but_never_approves(api: Api) -> None:
    pid = api.post(
        "/api/projects",
        {"title": "Guía de micrófonos baratos", "platforms": ["youtube"]},
    )["id"]
    drain()

    def order(text: str) -> dict:
        return api.post("/api/chat", {"text": text})["messages"][1]

    assert "intérprete de órdenes" in order("ayuda")["text"]
    status = order("¿qué falta?")
    assert (
        "Guía de micrófonos baratos" in status["text"]
        and "espera tu aprobación" in status["text"]
    )
    refused = order("aprueba la investigación")
    assert refused["action"]["ok"] is False and "No apruebo por ti" in refused["text"]
    assert (
        next(
            s
            for s in api.get(f"/api/projects/{pid}")["steps"]
            if s["kind"] == "research"
        )["status"]
        == "awaiting_approval"
    )
    assert "No publico desde aquí" in order("publica el vídeo ya")["text"]
    assert order("prepara algo para mañana")["action"]["ok"] is False
    assert api.get(f"/api/projects/{pid}")["state"] == "active"

    assert order("pausa micrófonos")["action"]["type"] == "pause"
    assert api.get(f"/api/projects/{pid}")["state"] == "paused"
    assert order("reanuda el proyecto")["action"]["type"] == "resume"
    assert order("relanza a Scout")["action"]["type"] == "rerun"
    assert (
        next(
            s
            for s in api.get(f"/api/projects/{pid}")["steps"]
            if s["kind"] == "research"
        )["status"]
        == "queued"
    )
    assert "No se puede" in order("reintenta el guion")["text"]
    assert commands.normalize("¡Reanúda  el  Guión!") == "reanuda el guion"
    assert len(api.get("/api/chat")["messages"]) >= 16


def test_order_console_reads_the_verb_not_the_words_of_the_title(api: Api) -> None:
    """A title is free text: «Cómo pausar…» contains two words that are also orders."""
    title = "Cómo pausar un directo sin perder audiencia"
    pid = api.post("/api/projects", {"title": title, "platforms": ["youtube"]})["id"]
    drain()

    def order(text: str) -> dict:
        return api.post("/api/chat", {"text": text})["messages"][1]

    def state() -> str:
        return api.get(f"/api/projects/{pid}")["state"]

    # Asking about it by name is a question, even though the name says «pausar».
    assert (
        order(f"estado de {title}")["action"]["type"] == "status"
        and state() == "active"
    )
    assert order(f"pausa {title}")["action"]["type"] == "pause" and state() == "paused"
    # «cómo» in the name must not turn the order into a status request.
    assert (
        order(f"reanuda {title}")["action"]["type"] == "resume" and state() == "active"
    )
    assert (
        order(f"pausa «{title}»")["action"]["type"] == "pause" and state() == "paused"
    )
    assert order("¿cómo va todo?")["action"]["type"] == "status" and state() == "paused"
    assert (
        order("reanuda directo")["action"]["type"] == "resume" and state() == "active"
    )
    # Naming a step with «cancela» does not cancel the whole project by surprise.
    refused = order("cancela el guion")
    assert (
        refused["action"]["ok"] is False
        and "Un paso no se cancela" in refused["text"]
        and state() == "active"
    )
    assert (
        order(f"cancela «{title}»")["action"]["type"] == "cancel"
        and state() == "cancelled"
    )


def test_cancelling_a_running_export_stops_ffmpeg_and_leaves_no_fake_result(
    api: Api, sample_video: Path
) -> None:
    pid = api.post(
        "/api/projects",
        {
            "title": "Exportación larga",
            "platforms": ["youtube"],
            "options": {"clip_min_s": 10, "clip_max_s": 20},
        },
    )["id"]
    api.request(
        "POST",
        f"/api/projects/{pid}/uploads?kind=source&filename=v.mp4&rights=true",
        content=sample_video.read_bytes(),
    )
    detail = api.post(
        f"/api/projects/{pid}/clips", {"start": 0, "end": 22, "title": "Todo"}
    )
    clip_id = detail["clips"][0]["id"]
    detail = api.post(
        f"/api/clips/{clip_id}/renders", {"format": "horizontal", "framing": "crop"}
    )
    assert (
        detail["renders"][0]["status"] == "queued"
        and detail["jobs"][0]["agent"] == "editor"
    )
    run_id = detail["jobs"][0]["id"]

    thread = threading.Thread(target=worker.run_now, args=(run_id,))
    thread.start()
    for _ in range(200):  # wait until FFmpeg is really encoding
        with session_scope() as session:
            if (
                session.get(Run, run_id).status == "working"
                and session.scalar(select(Render.status)) == "rendering"
            ):
                break
        threading.Event().wait(0.05)
    editor = next(a for a in api.get("/api/office")["agents"] if a["key"] == "editor")
    assert editor["status"] == "working" and editor["current"][0]["id"] == run_id
    api.post(f"/api/runs/{run_id}/cancel")
    thread.join(timeout=30)
    assert not thread.is_alive()

    detail = api.get(f"/api/projects/{pid}")
    assert (
        detail["renders"][0]["status"] == "pending"
        and detail["renders"][0]["url"] is None
    )
    assert api.get(f"/api/runs?project_id={pid}")["runs"][0]["status"] == "cancelled"
    assert (
        next(a for a in api.get("/api/office")["agents"] if a["key"] == "editor")[
            "status"
        ]
        == "idle"
    )
    with session_scope() as session:
        assert session.scalar(
            select(Step.status).where(Step.project_id == pid, Step.kind == "edit")
        ) in ("pending", "blocked")


def test_spanish_copy_helpers() -> None:
    from creator_office.text import decimal, shorten

    assert decimal(15.63) == "15,6" and decimal(1.5, 2) == "1,50"
    caption = "Agentes de IA que ayudan a un creador pequeño a planificar y publicar vídeo sin perder el control\n\n#agentes"
    assert (
        shorten(caption)
        == "Agentes de IA que ayudan a un creador pequeño a planificar…"
    )
    assert shorten("Título corto") == "Título corto"
    assert len(shorten("x" * 200)) == 60 and shorten("x" * 200).endswith("…")


def test_gaps_left_in_a_script_are_counted_wherever_a_person_would_find_them() -> None:
    content = {
        "working_title": "Una semana de vídeos",
        "hooks": ["Lo que nadie te cuenta: [tu dato]", "Un gancho ya escrito"],
        "outline": ["Inicio", "[Punto que falta]"],
        "titles": ["[Título alternativo]"],
        "sections": [
            {
                "heading": "Inicio",
                "text": "[Lo que dices] y [un ejemplo]",
                "visual": "[Lo que se ve]",
            }
        ],
        "cta": "Suscríbete",
        "platform_copy": [
            {
                "platform": "tiktok",
                "title": "",
                "description": "Texto con [hueco]",
                "hashtags": ["#video"],
            }
        ],
        "notes": "Sustituye todos los textos entre [corchetes].",  # explains the convention: not a gap
    }
    assert count_placeholders(content) == 7
    assert (
        count_placeholders(
            {"hooks": ["Opción [a] o [b]"], "sections": [{"heading": "Sin texto"}]}
        )
        == 0
    )  # too short to be a gap


def test_the_server_starts_with_the_settings_as_people_write_them(
    settings_env: Callable[..., None],
) -> None:
    from creator_office.main import create_app

    # The command reads HOST, PORT and LOG_LEVEL like every other setting, in either case.
    settings_env(LOG_LEVEL="warning", PORT="8123", HOST="127.0.0.1")
    settings = get_settings()
    assert (settings.host, settings.port, settings.log_level) == (
        "127.0.0.1",
        8123,
        "warning",
    )
    with TestClient(
        create_app()
    ) as client:  # start-up configures logging with that level
        assert client.get("/api/health").status_code == 200
    settings_env(LOG_LEVEL="DEBUG")
    assert get_settings().log_level == "debug"
    # A level that does not exist is refused by name instead of crashing later.
    settings_env(LOG_LEVEL="ruidoso")
    with pytest.raises(ValidationError, match="log_level"):
        get_settings()


def test_the_example_env_file_loads_as_written_and_documents_every_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = APP_ROOT / ".env.example"
    documented = set(
        re.findall(
            r"^#?([A-Z][A-Z0-9_]*)=",
            example.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )
    assert documented == {name.upper() for name in Settings.model_fields} - {
        "WORKER_AUTOSTART"
    }  # that one is for tests

    # "Copy it to .env" is the first instruction in the README: blank values must mean "not set".
    for name in documented | {"WORKER_AUTOSTART"}:
        monkeypatch.delenv(name, raising=False)
    copied = Settings(_env_file=example)
    assert copied.model_dump() == Settings(_env_file=None).model_dump()
    assert (
        copied.llm_price_input_per_mtok is None
        and copied.port == 8000
        and copied.allowed_frame_ancestors == "'self'"
    )


@pytest.mark.parametrize(
    ("written", "policy", "frame_options"),
    [
        ("self", "'self'", "SAMEORIGIN"),
        (
            "'self'",
            "'self'",
            "SAMEORIGIN",
        ),  # some env-file loaders keep the quotes, others strip them
        ("NONE", "'none'", "DENY"),
        ("*", "*", None),
        (
            "https://intranet.example  https://otra.example",
            "https://intranet.example https://otra.example",
            None,
        ),
    ],
)
def test_who_may_frame_the_app_is_sent_in_a_form_browsers_accept(
    settings_env: Callable[..., None],
    written: str,
    policy: str,
    frame_options: str | None,
) -> None:
    from creator_office.main import create_app

    settings_env(ALLOWED_FRAME_ANCESTORS=written)
    with TestClient(create_app()) as client:
        headers = client.get("/api/health").headers
    assert headers["content-security-policy"].endswith(f"frame-ancestors {policy}")
    assert headers.get("x-frame-options") == frame_options
    settings_env(
        ALLOWED_FRAME_ANCESTORS="'self'; script-src *"
    )  # one directive only: nothing can ride along
    with pytest.raises(ValidationError, match="allowed_frame_ancestors"):
        get_settings()


def test_the_interface_is_served_as_code_even_where_the_system_calls_it_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_env: Callable[..., None]
) -> None:
    """On Windows ``mimetypes`` copies the registry, where ``.js`` is often ``text/plain``.

    This app sends ``nosniff``, so a browser would refuse that script and show a blank page.
    """
    from creator_office.main import create_app

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>x</title>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    (dist / "assets" / "app.css").write_text("body {}", encoding="utf-8")
    (dist / "favicon.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
    )
    mimetypes.guess_type("x.js")  # the table is loaded on first use
    for suffix in (".js", ".css", ".svg", ".html"):
        monkeypatch.setitem(mimetypes.types_map, suffix, "text/plain")
    settings_env(FRONTEND_DIST=str(dist))

    with TestClient(create_app()) as client:
        served = {
            path: client.get(path).headers
            for path in (
                "/",
                "/proyectos",
                "/assets/app.js",
                "/assets/app.css",
                "/favicon.svg",
            )
        }
    assert {path: headers["content-type"] for path, headers in served.items()} == {
        "/": "text/html; charset=utf-8",
        "/proyectos": "text/html; charset=utf-8",  # a client-side route gets the page
        "/assets/app.js": "text/javascript; charset=utf-8",
        "/assets/app.css": "text/css; charset=utf-8",
        "/favicon.svg": "image/svg+xml",
    }
    assert {headers["x-content-type-options"] for headers in served.values()} == {
        "nosniff"
    }
