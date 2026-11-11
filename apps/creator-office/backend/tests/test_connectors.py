"""Connectors against scripted HTTP transports: only what the platform confirms counts."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from creator_office.connectors import instagram, tiktok, youtube
from creator_office.connectors.base import (
    AuthExpiredError,
    ConnectorError,
    PublishRequest,
    RateLimitedError,
    TokenBundle,
)
from creator_office.database import utcnow

BUNDLE = TokenBundle(
    access_token="token-abc",
    refresh_token="refresh-xyz",
    expires_at=utcnow() + timedelta(hours=1),
    extra={"user_id": "178"},
)


def make_video(tmp_path: Path, size: int = 3000) -> Path:
    path = tmp_path / "pieza.mp4"
    path.write_bytes(b"\x00" * size)
    return path


def request_for(path: Path, **options: object) -> PublishRequest:
    return PublishRequest(
        video_path=path,
        title="Título de prueba",
        description="Descripción",
        tags=["uno", "dos"],
        options={"_poll_seconds": 0, **options},
    )


def test_youtube_resumable_upload_reports_what_youtube_returned(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/upload/youtube/v3/videos":
            body = json.loads(request.content)
            assert (
                body["status"]["privacyStatus"] == "public"
                and body["snippet"]["title"] == "Título de prueba"
            )
            assert request.headers["authorization"] == "Bearer token-abc"
            return httpx.Response(
                200, headers={"Location": "https://upload.example/session-1"}
            )
        if request.method == "PUT":
            assert request.headers["content-range"] == "bytes 0-2999/3000"
            # An unaudited API project: YouTube forces the upload to private.
            return httpx.Response(
                200,
                json={
                    "id": "vid123",
                    "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    progress: list[float] = []
    result = youtube.publish(
        BUNDLE,
        request_for(video, privacy="public"),
        lambda f, _label: progress.append(f),
        httpx.MockTransport(handler),
    )
    assert (result.state, result.post_id, result.url) == (
        "published",
        "vid123",
        "https://www.youtube.com/watch?v=vid123",
    )
    assert result.visibility == "private" and "en lugar de «public»" in result.note
    assert progress[-1] == 1.0 and len(seen) == 2


def test_youtube_native_schedule_and_error_mapping(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    when = utcnow() + timedelta(days=2)

    def scheduled(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            status = json.loads(request.content)["status"]
            assert status["privacyStatus"] == "private" and status[
                "publishAt"
            ].endswith("Z")
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        return httpx.Response(
            201, json={"id": "vid9", "status": {"privacyStatus": "private"}}
        )

    request = request_for(video, privacy="public", is_short=True)
    request.publish_at = when
    result = youtube.publish(BUNDLE, request, None, httpx.MockTransport(scheduled))
    assert result.state == "scheduled" and result.url.endswith("/shorts/vid9")

    with pytest.raises(AuthExpiredError):
        youtube.publish(
            BUNDLE,
            request_for(video),
            None,
            httpx.MockTransport(
                lambda r: httpx.Response(
                    401, json={"error": {"message": "Invalid Credentials"}}
                )
            ),
        )
    quota = httpx.MockTransport(
        lambda r: httpx.Response(
            403,
            json={
                "error": {
                    "message": "The request cannot be completed because you have exceeded your quota."
                }
            },
        )
    )
    with pytest.raises(RateLimitedError) as info:
        youtube.publish(BUNDLE, request_for(video), None, quota)
    assert info.value.retryable and "más tarde" in info.value.user_message
    assert "token-abc" not in info.value.user_message


def test_youtube_refresh_keeps_the_refresh_token_and_detects_revocation(
    settings_env: Callable[..., None],
) -> None:
    settings_env(YOUTUBE_CLIENT_ID="client", YOUTUBE_CLIENT_SECRET="secret")

    def ok(request: httpx.Request) -> httpx.Response:
        form = dict(pair.split("=") for pair in request.content.decode().split("&"))
        assert form["grant_type"] == "refresh_token" and form["client_id"] == "client"
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    fresh = youtube.refresh(BUNDLE, httpx.MockTransport(ok))
    assert fresh.access_token == "fresh" and fresh.refresh_token == "refresh-xyz"
    revoked = httpx.MockTransport(
        lambda r: httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
        )
    )
    with pytest.raises(ConnectorError) as info:
        youtube.refresh(BUNDLE, revoked)
    assert info.value.code == "auth_expired"


def test_tiktok_direct_post_requires_a_privacy_choice_and_confirms_by_status(
    tmp_path: Path,
) -> None:
    video = make_video(tmp_path)
    with pytest.raises(ConnectorError) as info:
        tiktok.publish(
            BUNDLE,
            request_for(video),
            None,
            httpx.MockTransport(lambda r: httpx.Response(500)),
        )
    assert info.value.code == "missing_privacy"

    calls: list[str] = []
    statuses = iter(["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/creator_info/query/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "creator_username": "ana",
                        "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                        "max_video_post_duration_sec": 600,
                    },
                    "error": {"code": "ok"},
                },
            )
        if request.url.path.endswith("/video/init/"):
            body = json.loads(request.content)
            assert body["post_info"]["privacy_level"] == "SELF_ONLY"
            assert body["source_info"] == {
                "source": "FILE_UPLOAD",
                "video_size": 3000,
                "chunk_size": 3000,
                "total_chunk_count": 1,
            }
            return httpx.Response(
                200,
                json={
                    "data": {
                        "publish_id": "p_1",
                        "upload_url": "https://upload.tiktok.example/u",
                    },
                    "error": {"code": "ok"},
                },
            )
        if request.method == "PUT":
            assert request.headers["content-range"] == "bytes 0-2999/3000"
            return httpx.Response(201)
        if request.url.path.endswith("/status/fetch/"):
            status = next(statuses)
            data = {"status": status}
            if status == "PUBLISH_COMPLETE":
                data["publicaly_available_post_id"] = [7301]
            return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})
        raise AssertionError(request.url)

    result = tiktok.publish(
        BUNDLE,
        request_for(video, privacy_level="SELF_ONLY"),
        None,
        httpx.MockTransport(handler),
    )
    assert (result.state, result.post_id) == ("published", "7301")
    assert result.url == "https://www.tiktok.com/@ana/video/7301"
    assert calls.count("/v2/post/publish/status/fetch/") == 2

    refused = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "data": {"privacy_level_options": ["SELF_ONLY"]},
                "error": {"code": "ok"},
            },
        )
    )
    with pytest.raises(ConnectorError) as info:
        tiktok.publish(
            BUNDLE,
            request_for(video, privacy_level="PUBLIC_TO_EVERYONE"),
            None,
            refused,
        )
    assert info.value.code == "privacy_not_allowed"


def test_tiktok_chunk_plan_and_unsupported_comments() -> None:
    mb = 1024 * 1024
    assert tiktok.plan_chunks(3 * mb) == (3 * mb, 1)
    assert tiktok.plan_chunks(10 * mb) == (10 * mb, 1)
    assert tiktok.plan_chunks(25 * mb) == (
        10 * mb,
        2,
    )  # the trailing 5 MB ride on the last chunk
    assert tiktok.plan_chunks(30 * mb) == (10 * mb, 3)
    assert tiktok.CAPABILITIES.comments is False
    with pytest.raises(ConnectorError) as info:
        tiktok.fetch_comments(BUNDLE, "1")
    assert (
        info.value.code == "unsupported"
        and "impórtalos a mano" in info.value.user_message
    )


def test_instagram_needs_a_public_url_and_publishes_only_a_finished_container(
    tmp_path: Path,
) -> None:
    video = make_video(tmp_path)
    with pytest.raises(ConnectorError) as info:
        instagram.publish(
            BUNDLE,
            request_for(video),
            None,
            httpx.MockTransport(lambda r: httpx.Response(500)),
        )
    assert info.value.code == "needs_public_url"

    statuses = iter(["IN_PROGRESS", "FINISHED"])
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/178/media"):
            form = request.content.decode()
            assert "media_type=REELS" in form and "video_url=https" in form
            return httpx.Response(200, json={"id": "container1"})
        if request.method == "GET" and path.endswith("/container1"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if request.method == "POST" and path.endswith("/178/media_publish"):
            posted.append(request.content.decode())
            return httpx.Response(200, json={"id": "media77"})
        if request.method == "GET" and path.endswith("/media77"):
            return httpx.Response(
                200, json={"permalink": "https://www.instagram.com/reel/abc/"}
            )
        raise AssertionError(request.url)

    request = request_for(video)
    request.public_video_url = "https://studio.example.com/api/files/x?exp=1&sig=2"
    result = instagram.publish(BUNDLE, request, None, httpx.MockTransport(handler))
    assert (result.state, result.post_id, result.url) == (
        "published",
        "media77",
        "https://www.instagram.com/reel/abc/",
    )
    assert len(posted) == 1 and "creation_id=container1" in posted[0]

    failed = httpx.MockTransport(
        lambda r: (
            httpx.Response(200, json={"id": "c2"})
            if r.method == "POST"
            else httpx.Response(
                200, json={"status_code": "ERROR", "status": "Video too short"}
            )
        )
    )
    with pytest.raises(ConnectorError) as info:
        instagram.publish(BUNDLE, request, None, failed)
    assert info.value.code == "failed" and "Video too short" in info.value.user_message

    expired = httpx.MockTransport(
        lambda r: httpx.Response(
            400,
            json={"error": {"code": 190, "message": "Error validating access token"}},
        )
    )
    with pytest.raises(AuthExpiredError):
        instagram.publish(BUNDLE, request, None, expired)
