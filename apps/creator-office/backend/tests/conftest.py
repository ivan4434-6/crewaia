"""Shared fixtures: an isolated data directory per test and jobs run by hand."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="creator-office-tests-"))
os.environ.setdefault("CREWAI_STORAGE_DIR", str(_SESSION_TMP / "crewai"))
os.environ["DATA_DIR"] = str(_SESSION_TMP / "bootstrap")
os.environ["WORKER_AUTOSTART"] = "false"
for _name in (
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "APP_SECRET_KEY",
    "DATABASE_URL",
    "TRANSCRIPTION_MODEL",
):
    os.environ.pop(_name, None)

from creator_office.settings import (  # noqa: E402
    Settings,
    configure_crewai_environment,
    get_settings,
)

Settings.model_config["env_file"] = (
    None  # a developer's .env, with real keys, must never reach the tests
)
configure_crewai_environment()

from fastapi.testclient import TestClient  # noqa: E402

from creator_office import database, security  # noqa: E402
from creator_office.media import ffmpeg  # noqa: E402
from tests.helpers import Api  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fresh database, storage and secret key for every test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    for name in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
        "INSTAGRAM_APP_ID",
        "INSTAGRAM_APP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    database.reset_engine_for_tests()
    security.reset_for_tests()
    security.login_throttle._failures.clear()
    database.init_db()
    yield tmp_path
    database.reset_engine_for_tests()
    get_settings.cache_clear()


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Change settings mid-test: ``settings_env(YOUTUBE_CLIENT_ID="x")``."""

    def apply(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    return apply


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    from creator_office.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def api(app_client: TestClient) -> Api:
    return Api(app_client).register()


@pytest.fixture
def other_browser() -> Iterator[Callable[[], Api]]:
    """Factory for extra, independent browsers against the same server."""
    from creator_office.main import create_app

    clients: list[TestClient] = []

    def make() -> Api:
        client = TestClient(create_app())
        client.__enter__()
        clients.append(client)
        return Api(client)

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture(scope="session")
def sample_video() -> Path:
    """24 s synthetic clip: three 6 s tone bursts separated by 2 s of silence, with hard picture cuts."""
    tool = ffmpeg.locate()
    if tool is None:
        pytest.skip("FFmpeg is not available")
    path = _SESSION_TMP / "sample.mp4"
    if not path.exists():
        started = time.time()
        subprocess.run(  # noqa: S603 - fixed arguments
            [
                tool.path,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=24:duration=8",
                "-f",
                "lavfi",
                "-i",
                "smptebars=size=640x360:rate=24:duration=8",
                "-f",
                "lavfi",
                "-i",
                "rgbtestsrc=size=640x360:rate=24:duration=8",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100:duration=24",
                "-filter_complex",
                "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v];[3:a]volume='if(lt(mod(t,8),6),1,0)':eval=frame[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            check=True,
            timeout=120,
        )
        assert time.time() - started < 120
    return path
