"""Publishing through a connected account: OAuth, final confirmation and platform-confirmed states."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from creator_office import storage
from creator_office.connectors import base
from creator_office.database import session_scope, utcnow
from creator_office.models import (
    Clip,
    Connection,
    Event,
    Project,
    Publication,
    Render,
    Run,
)
from creator_office.pipeline import engine, worker
from creator_office.services import publishing
from tests.helpers import Api, drain


def scripted(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Route every connector request to ``handler`` instead of the internet."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        base,
        "http_client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(wrapped)),
    )
    return seen


def google(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "oauth2.googleapis.com" and path == "/token":
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.secret-access",
                "refresh_token": "1//secret-refresh",
                "expires_in": 3600,
                "scope": "a b",
            },
        )
    if path == "/youtube/v3/channels":
        return httpx.Response(
            200, json={"items": [{"id": "UC123", "snippet": {"title": "Canal de Ana"}}]}
        )
    if request.method == "POST" and path == "/upload/youtube/v3/videos":
        return httpx.Response(
            200, headers={"Location": "https://upload.example/session"}
        )
    if request.method == "PUT" and request.url.host == "upload.example":
        return httpx.Response(
            200, json={"id": "yt-video-1", "status": {"privacyStatus": "unlisted"}}
        )
    if path == "/upload/youtube/v3/thumbnails/set":
        return httpx.Response(200, json={})
    if path == "/revoke":
        return httpx.Response(200)
    raise AssertionError(f"unexpected {request.method} {request.url}")


def connect_youtube(api: Api) -> None:
    started = api.post("/api/integrations/youtube/connect")
    query = parse_qs(urlparse(started["url"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == [
        "http://localhost:8000/api/integrations/youtube/callback"
    ]
    response = api.client.get(
        f"/api/integrations/youtube/callback?code=abc&state={query['state'][0]}",
        follow_redirects=False,
    )
    assert (
        response.status_code == 303
        and response.headers["location"] == "/integraciones?conectado=youtube"
    )


def ready_publication(api: Api, tmp_path: Path) -> tuple[str, str]:
    """A project with one exported piece and an approved YouTube draft (built directly, no FFmpeg needed)."""
    pid = api.post(
        "/api/projects",
        {
            "title": "Tutorial de iluminación",
            "platforms": ["youtube"],
            "template": "idea_video",
        },
    )["id"]
    drain()
    workspace_id = api.session["workspace"]["id"]
    with session_scope() as session:
        relative, destination = storage.new_path(workspace_id, "pieza.mp4")
        destination.write_bytes(b"\x00" * 4096)
        media = {
            "duration": 42.0,
            "video": {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0},
            "audio": {"codec": "aac"},
        }
        asset = storage.register_file(
            session,
            workspace_id=workspace_id,
            relative_path=relative,
            filename="pieza.mp4",
            kind="export",
            origin="upload",
            created_by="test",
            project_id=pid,
            meta={"media": media},
            rights_declared=True,
        )
        clip = Clip(
            workspace_id=workspace_id,
            project_id=pid,
            asset_id=asset.id,
            start=0,
            end=42,
            title="Pieza",
            status="selected",
            origin="user",
        )
        session.add(clip)
        session.flush()
        session.add(
            Render(
                workspace_id=workspace_id,
                project_id=pid,
                clip_id=clip.id,
                format="horizontal",
                status="ready",
                asset_id=asset.id,
                origin="upload",
            )
        )
        session.flush()
        project = session.get(Project, pid)
        (pub,) = publishing.ensure_publications(session, project)
        pub.title, pub.description, pub.tags = (
            "Tutorial de iluminación en casa",
            "Tres luces baratas que cambian tu plano.",
            ["iluminación"],
        )
        publishing.validate(session, pub)
        publication_id = pub.id
    api.post(f"/api/publications/{publication_id}/actions", {"action": "approve"})
    return pid, publication_id


def test_oauth_connection_is_stored_encrypted_and_scoped(
    api: Api, settings_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {p["provider"]: p for p in api.get("/api/integrations")["platforms"]}
    assert (
        status["youtube"]["state"] == "needs_setup"
        and status["youtube"]["label"] == "Pendiente de conexión"
    )
    assert status["youtube"]["missing_settings"] == [
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
    ]
    assert (
        api.post("/api/integrations/youtube/connect", expect=409)["code"]
        == "needs_setup"
    )

    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")
    scripted(monkeypatch, google)
    assert {p["provider"]: p for p in api.get("/api/integrations")["platforms"]}[
        "youtube"
    ]["state"] == "ready"

    # A callback with a state nobody issued is rejected and stores nothing.
    forged = api.client.get(
        "/api/integrations/youtube/callback?code=abc&state=forged",
        follow_redirects=False,
    )
    assert forged.status_code == 303 and "error=youtube" in forged.headers["location"]
    with session_scope() as session:
        assert session.scalar(select(Connection)) is None

    connect_youtube(api)
    status = {p["provider"]: p for p in api.get("/api/integrations")["platforms"]}[
        "youtube"
    ]
    assert status["state"] == "connected" and status["account_name"] == "Canal de Ana"
    with session_scope() as session:
        connection = session.scalar(select(Connection))
        assert (
            "ya29" not in connection.secret
            and "secret-refresh" not in connection.secret
        )
        assert connection.account_id == "UC123"
    assert "ya29" not in json.dumps(api.get("/api/integrations"))

    api.delete("/api/integrations/youtube")
    assert {p["provider"]: p for p in api.get("/api/integrations")["platforms"]}[
        "youtube"
    ]["state"] == "ready"


def test_publication_is_published_only_when_the_platform_confirms(
    api: Api,
    tmp_path: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")
    seen = scripted(monkeypatch, google)
    connect_youtube(api)
    pid, publication_id = ready_publication(api, tmp_path)
    action = f"/api/publications/{publication_id}/actions"

    api.patch(
        f"/api/publications/{publication_id}",
        {"options": {"privacy": "unlisted", "ignored": "x"}},
    )
    # Editing options changes what was approved, so approval is asked again.
    assert (
        api.post(action, {"action": "publish", "confirm": True}, expect=409)["code"]
        == "not_approved"
    )
    api.post(action, {"action": "approve"})

    seen.clear()
    queued = api.post(action, {"action": "publish", "confirm": True})["publication"]
    assert queued["status"] == "publishing" and queued["platform_post_id"] == ""
    assert seen == []  # nothing is sent until the job actually runs
    drain()

    done = api.get(f"/api/projects/{pid}")["publications"][0]
    assert done["status"] == "published" and done["confirmation"] == "platform"
    assert (
        done["platform_post_id"] == "yt-video-1"
        and done["url"] == "https://www.youtube.com/watch?v=yt-video-1"
    )
    assert done["options"]["platform_visibility"] == "unlisted"
    upload = next(
        r
        for r in seen
        if r.method == "POST" and r.url.path == "/upload/youtube/v3/videos"
    )
    assert json.loads(upload.content)["status"]["privacyStatus"] == "unlisted"
    assert upload.headers["authorization"] == "Bearer ya29.secret-access"
    with session_scope() as session:
        events = {
            e.type: e
            for e in session.scalars(select(Event).where(Event.project_id == pid))
        }
    assert events["publication.confirmed"].actor_type == "user"
    assert events["publication.published"].actor_type == "platform"
    # Published content is frozen.
    assert (
        api.patch(f"/api/publications/{publication_id}", {"title": "Otro"}, expect=409)[
            "code"
        ]
        == "not_editable"
    )
    assert api.post(action, {"action": "discard"}, expect=409)["code"] == "not_possible"


def test_platform_errors_leave_an_error_state_never_published(
    api: Api,
    tmp_path: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")
    scripted(monkeypatch, google)
    connect_youtube(api)
    pid, publication_id = ready_publication(api, tmp_path)
    action = f"/api/publications/{publication_id}/actions"

    def quota_exceeded(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/upload/youtube/v3/videos":
            return httpx.Response(
                403,
                json={
                    "error": {
                        "message": "The request cannot be completed because you have exceeded your quota."
                    }
                },
            )
        return google(request)

    scripted(monkeypatch, quota_exceeded)
    api.post(action, {"action": "publish", "confirm": True})
    drain()
    failed = api.get(f"/api/projects/{pid}")["publications"][0]
    assert (
        failed["status"] == "error"
        and failed["confirmation"] == ""
        and failed["platform_post_id"] == ""
    )
    assert "limitado" in failed["error"] and "reintentarlo" in failed["error"]
    assert any(
        n["type"] == "error" for n in api.get("/api/notifications")["notifications"]
    )

    # Revoked access: the connection itself is flagged for reconnection.
    def revoked(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/upload/youtube/v3/videos":
            return httpx.Response(
                401, json={"error": {"message": "Invalid Credentials"}}
            )
        return google(request)

    scripted(monkeypatch, revoked)
    api.post(action, {"action": "approve"})
    api.post(action, {"action": "publish", "confirm": True})
    drain()
    assert api.get(f"/api/projects/{pid}")["publications"][0]["status"] == "error"
    status = {p["provider"]: p for p in api.get("/api/integrations")["platforms"]}[
        "youtube"
    ]
    assert status["state"] == "expired" and status["label"] == "Requiere reconexión"
    refused = api.post(action, {"action": "publish", "confirm": True}, expect=409)
    assert refused["code"] == "not_connected" and "Alternativa" in refused["message"]


def test_scheduled_upload_runs_at_its_time_through_the_connector(
    api: Api,
    tmp_path: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")
    seen = scripted(monkeypatch, google)
    connect_youtube(api)
    pid, publication_id = ready_publication(api, tmp_path)
    action = f"/api/publications/{publication_id}/actions"
    when = (utcnow() + timedelta(days=1)).isoformat()
    assert (
        api.post(
            action,
            {"action": "schedule", "scheduled_at": when, "mode": "connector"},
            expect=422,
        )["code"]
        == "not_confirmed"
    )
    scheduled = api.post(
        action,
        {
            "action": "schedule",
            "scheduled_at": when,
            "mode": "connector",
            "confirm": True,
        },
    )["publication"]
    assert scheduled["status"] == "scheduled"

    seen.clear()
    worker.tick()
    assert drain() == [] and seen == []  # not due yet

    with session_scope() as session:
        session.get(Publication, publication_id).scheduled_at = utcnow() - timedelta(
            seconds=5
        )
    worker.tick()
    assert len(drain()) == 1
    done = api.get(f"/api/projects/{pid}")["publications"][0]
    assert done["status"] == "published" and done["confirmation"] == "platform"

    calendar = api.get(
        f"/api/calendar?start={(utcnow() - timedelta(days=2)).isoformat()}&end={(utcnow() + timedelta(days=2)).isoformat()}".replace(
            "+", "%2B"
        )
    )
    assert [(i["type"], i["status"]) for i in calendar["items"]] == [
        ("publication", "published")
    ]


def test_interrupted_upload_is_reported_as_unknown_not_published(
    api: Api, tmp_path: Path
) -> None:
    pid, publication_id = ready_publication(api, tmp_path)
    with session_scope() as session:
        pub = session.get(Publication, publication_id)
        pub.status = "publishing"
        run = engine.enqueue_job(
            session,
            workspace_id=pub.workspace_id,
            project_id=pid,
            agent="publisher",
            kind="publish",
            title="Publicar",
            payload={"publication_id": pub.id},
        )
        run.status = "working"
    assert worker.recover() == []
    recovered: dict[str, Any] = api.get(f"/api/projects/{pid}")["publications"][0]
    assert recovered["status"] == "error" and "no se sabe" in recovered["error"]


def test_comment_sync_rule_is_off_until_someone_enables_it(
    api: Api,
    tmp_path: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")

    def youtube(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/youtube/v3/commentThreads":
            snippet = {
                "authorDisplayName": "Leo",
                "textOriginal": "¿Qué foco usas de relleno?",
                "publishedAt": "2026-01-05T10:00:00Z",
            }
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "snippet": {
                                "topLevelComment": {"id": "c-1", "snippet": snippet}
                            }
                        }
                    ]
                },
            )
        return google(request)

    seen = scripted(monkeypatch, youtube)
    connect_youtube(api)
    pid, publication_id = ready_publication(api, tmp_path)
    api.post(
        f"/api/publications/{publication_id}/actions",
        {"action": "publish", "confirm": True},
    )
    drain()

    # Off by default: the scheduler reads nothing on its own.
    assert api.get("/api/comments")["automation"]["sync_comments_hours"] == 0
    seen.clear()
    worker.tick()
    assert drain() == [] and seen == []

    assert (
        api.request(
            "PUT",
            "/api/comments/automation",
            expect=422,
            json={"sync_comments_hours": 1},
        )["code"]
        == "invalid_frequency"
    )
    assert (
        api.request("PUT", "/api/comments/automation", json={"sync_comments_hours": 6})[
            "sync_comments_hours"
        ]
        == 6
    )
    worker.tick()
    ran = drain()
    with session_scope() as session:
        kinds = [
            (session.get(Run, run_id).kind, session.get(Run, run_id).agent)
            for run_id in ran
        ]
    # One read of the platform; the new comment then wakes Community to classify it.
    assert kinds == [("sync_comments", "community"), ("step", "community")]
    comments = api.get(f"/api/comments?project_id={pid}")
    assert [c["text"] for c in comments["comments"]] == ["¿Qué foco usas de relleno?"]
    assert comments["comments"][0]["reply_status"] in (
        "none",
        "suggested",
    )  # reading never sends anything
    assert not any(r.method == "POST" and "comments" in r.url.path for r in seen)
    assert comments["automation"]["last_run_at"]
    events = [e["type"] for e in api.get("/api/activity")["events"]]
    assert "automation.changed" in events and "automation.sync_comments" in events

    # Not again until the period has passed, and never without an account that can read comments.
    worker.tick()
    assert drain() == []
    api.request("PUT", "/api/comments/automation", json={"sync_comments_hours": 12})
    api.delete("/api/integrations/youtube")
    worker.tick()
    assert drain() == []


def test_a_reply_goes_out_only_after_approval_and_counts_as_sent_only_when_the_platform_says_so(
    api: Api,
    tmp_path: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")
    platform = {"accepts": False}

    def youtube(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/youtube/v3/commentThreads":
            snippet = {
                "authorDisplayName": "Leo",
                "textOriginal": "¿Qué foco usas de relleno?",
                "publishedAt": "2026-01-05T10:00:00Z",
            }
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "snippet": {
                                "topLevelComment": {"id": "c-1", "snippet": snippet}
                            }
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/youtube/v3/comments":
            if not platform["accepts"]:
                return httpx.Response(
                    403, json={"error": {"message": "The comment cannot be created."}}
                )
            return httpx.Response(200, json={"id": "reply-1"})
        return google(request)

    seen = scripted(monkeypatch, youtube)
    connect_youtube(api)
    pid, publication_id = ready_publication(api, tmp_path)
    api.post(
        f"/api/publications/{publication_id}/actions",
        {"action": "publish", "confirm": True},
    )
    drain()
    api.post(f"/api/projects/{pid}/comments/sync")
    drain()

    def sent_replies() -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in seen
            if r.method == "POST" and r.url.path == "/youtube/v3/comments"
        ]

    comment = api.get(f"/api/comments?project_id={pid}")["comments"][0]
    assert comment["can_send"] is True and sent_replies() == []
    reply = f"/api/comments/{comment['id']}/reply"
    api.patch(
        f"/api/comments/{comment['id']}",
        {"reply_text": "Uso un panel LED pequeño con difusor."},
    )

    # Writing the text is not approving it, and approving it is not sending it.
    assert api.post(reply, {"action": "send"}, expect=409)["code"] == "not_approved"
    assert (
        api.post(reply, {"action": "approve"})["comment"]["reply_status"] == "approved"
    )
    assert sent_replies() == []

    # The platform refuses: the reply stays approved and unsent.
    assert api.post(reply, {"action": "send"}, expect=502)["code"] == "platform_error"
    assert (
        api.get(f"/api/comments?project_id={pid}")["comments"][0]["reply_status"]
        == "approved"
    )

    platform["accepts"] = True
    assert api.post(reply, {"action": "send"})["comment"]["reply_status"] == "sent"
    assert sent_replies()[-1] == {
        "snippet": {
            "parentId": "c-1",
            "textOriginal": "Uso un panel LED pequeño con difusor.",
        }
    }
    # Editing or sending again is refused once it is out.
    assert api.post(reply, {"action": "send"}, expect=409)["code"] == "already_sent"
    assert (
        api.patch(f"/api/comments/{comment['id']}", {"reply_text": "otra"}, expect=409)[
            "code"
        ]
        == "already_sent"
    )
    assert len(sent_replies()) == 2


def test_an_approval_covers_the_video_and_the_cover_that_were_approved(
    api: Api,
    tmp_path: Path,
    sample_video: Path,
    settings_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client-id", YOUTUBE_CLIENT_SECRET="client-secret")
    seen = scripted(monkeypatch, google)
    connect_youtube(api)
    pid, publication_id = ready_publication(api, tmp_path)
    action = f"/api/publications/{publication_id}/actions"
    clip_id = api.get(f"/api/projects/{pid}")["clips"][0]["id"]

    def publication() -> dict[str, Any]:
        return api.get(f"/api/projects/{pid}")["publications"][0]

    def replace_video(expect: int = 200) -> Any:
        url = f"/api/projects/{pid}/uploads?kind=piece&filename=nueva.mp4&clip_id={clip_id}&format=horizontal&rights=true"
        return api.request("POST", url, expect, content=sample_video.read_bytes())

    def events(kind: str) -> list[str]:
        return [
            e["message"]
            for e in api.get("/api/activity")["events"]
            if e["type"] == kind
        ]

    # A cover joins the draft: changing what the draft contains asks for a new approval, as before.
    design = api.post(
        f"/api/projects/{pid}/designs",
        {
            "kind": "thumbnail",
            "headline": "Tres luces baratas",
            "background": "gradient",
        },
    )["designs"][0]
    api.patch(f"/api/publications/{publication_id}", {"design_id": design["id"]})
    assert publication()["approved"] is False
    api.post(action, {"action": "approve"})

    # 1. The video is replaced after the approval: the approval is withdrawn, and says why.
    replace_video()
    assert publication()["approved"] is False
    (withdrawn,) = events("publication.approval_withdrawn")
    assert "porque cambió el vídeo después de aprobarla" in withdrawn
    assert (
        api.post(action, {"action": "publish", "confirm": True}, expect=409)["code"]
        == "not_approved"
    )
    assert (
        api.post(
            action,
            {
                "action": "schedule",
                "mode": "connector",
                "confirm": True,
                "scheduled_at": "2031-01-15T09:00:00Z",
            },
            expect=409,
        )["code"]
        == "not_approved"
    )

    # 2. The same for the cover.
    api.post(action, {"action": "approve"})
    api.patch(f"/api/designs/{design['id']}", {"headline": "Tres luces por 30 euros"})
    assert publication()["approved"] is False
    assert (
        "porque cambió la portada después de aprobarla"
        in events("publication.approval_withdrawn")[0]
    )

    # 3. While it waits for its date, its video and cover cannot be replaced, trimmed away or deleted.
    api.post(action, {"action": "approve"})
    api.post(
        action,
        {
            "action": "schedule",
            "mode": "connector",
            "confirm": True,
            "scheduled_at": "2031-01-15T09:00:00Z",
        },
    )
    refused = replace_video(expect=409)
    assert (
        refused["code"] == "in_use"
        and "publicación programada de YouTube" in refused["message"]
    )
    assert (
        api.patch(
            f"/api/designs/{design['id']}", {"headline": "Otra portada"}, expect=409
        )["code"]
        == "in_use"
    )
    assert (
        api.patch(f"/api/clips/{clip_id}", {"start": 2}, expect=409)["code"] == "in_use"
    )
    render_id = publication()["render"]["id"]
    assert api.delete(f"/api/renders/{render_id}", expect=409)["code"] == "in_use"
    assert publication()["status"] == "scheduled" and publication()["approved"] is True

    # 4. Even if a file were swapped behind the rules, the upload itself refuses to send what nobody approved.
    api.post(action, {"action": "unschedule"})
    with session_scope() as session:
        pub = session.get(Publication, publication_id)
        pub.options = {
            **pub.options,
            "_approved_media": {
                **pub.options["_approved_media"],
                "video": "otro-archivo",
            },
        }
        ctx_run = engine.enqueue_job(
            session,
            workspace_id=pub.workspace_id,
            project_id=pid,
            agent="publisher",
            kind="publish",
            title="Publicar",
            payload={"publication_id": publication_id},
            engine="connector",
        )
        run_id = ctx_run.id
    worker.run_now(run_id)
    failed = publication()
    assert (
        failed["status"] == "error"
        and failed["approved"] is False
        and "Cambió el vídeo después de aprobarla" in failed["error"]
    )
    assert not any(r.method == "POST" and "upload" in r.url.path for r in seen)

    # 5. Once it has gone out it keeps the facts of what went out, and the piece is free again for other uses.
    api.post(action, {"action": "approve"})
    api.post(action, {"action": "publish", "confirm": True})
    drain()
    out = publication()
    assert out["status"] == "published" and out["changed_after_publication"] == []
    sent_seconds = out["published_media"]["duration"]
    assert sent_seconds and sent_seconds > 20
    api.patch(
        f"/api/designs/{design['id']}", {"headline": "Portada para el siguiente vídeo"}
    )
    out = publication()
    assert out["status"] == "published" and out["changed_after_publication"] == [
        "la portada"
    ]
    assert out["published_media"]["duration"] == sent_seconds
    assert (
        api.delete(f"/api/renders/{render_id}", expect=409)["code"] == "in_use"
    )  # the record of what went out stays
