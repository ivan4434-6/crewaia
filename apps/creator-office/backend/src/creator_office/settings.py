"""Runtime configuration, read from environment variables or a ``.env`` file.

Nothing here has a secret default. Credentials that are missing leave the
matching integration in "pending connection" state instead of faking it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parents[3]  # apps/creator-office
PACKAGE_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(APP_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # "PORT=" in a copied .env.example means "not set", not an invalid number
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -----------------------------------------------------
    app_env: Literal["development", "production"] = "development"
    app_base_url: str = "http://localhost:8000"
    data_dir: Path = APP_ROOT / "data"
    database_url: str = ""
    app_secret_key: str = ""
    allow_registration: bool = True
    max_upload_mb: int = 2048
    worker_concurrency: int = 3
    worker_autostart: bool = True  # tests switch this off and run jobs by hand
    frontend_dist: Path = APP_ROOT / "frontend" / "dist"

    # --- Server (used by the ``creator-office`` command) -------------------
    host: str = "0.0.0.0"  # noqa: S104 - a server is meant to listen; put it behind your proxy/firewall
    port: int = 8000
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"

    # --- Session cookie --------------------------------------------------
    session_ttl_hours: int = 24 * 14
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_secure: Literal["auto", "true", "false"] = "auto"
    session_cookie_partitioned: bool = False
    allowed_frame_ancestors: str = "'self'"

    # --- AI provider (optional: without it the local demo engine is used) -
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_price_input_per_mtok: float | None = None
    llm_price_output_per_mtok: float | None = None

    # --- Transcription (optional, OpenAI-compatible /audio/transcriptions) -
    transcription_model: str = ""
    transcription_api_key: str = ""
    transcription_base_url: str = "https://api.openai.com/v1"

    # --- Media -----------------------------------------------------------
    ffmpeg_path: str = ""

    # --- Platform apps (OAuth clients registered by the operator) ---------
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    instagram_graph_version: str = Field(default="v21.0")

    @field_validator("log_level", mode="before")
    @classmethod
    def _any_case(cls, value: object) -> object:
        """``INFO`` and ``info`` are the same level."""
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("allowed_frame_ancestors")
    @classmethod
    def _quoted_keywords(cls, value: str) -> str:
        """``self`` and ``none`` are CSP keywords and must reach the header quoted.

        Env-file loaders disagree about quotes (some strip them, some keep them),
        so both spellings are accepted here and the header always gets the right one.
        """
        if any(char in value for char in ";,\r\n"):
            raise ValueError(
                "list the origins that may embed the app separated by spaces"
            )
        words = [word.strip("'\"") for word in value.split()]
        return (
            " ".join(
                f"'{word.lower()}'" if word.lower() in {"self", "none"} else word
                for word in words
                if word
            )
            or "'self'"
        )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'creator_office.db').as_posix()}"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def oauth_redirect_uri(self, provider: str) -> str:
        return f"{self.app_base_url.rstrip('/')}/api/integrations/{provider}/callback"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    return settings


def configure_crewai_environment() -> None:
    """Keep CrewAI self-contained and quiet inside a server process.

    Telemetry and hosted tracing are opt-in for this app: an operator who
    wants them sets the variables explicitly before starting the server.
    """
    settings = get_settings()
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
    os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")
    os.environ.setdefault("CREWAI_STORAGE_DIR", str(settings.data_dir / "crewai"))
