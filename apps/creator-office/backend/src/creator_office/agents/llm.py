"""AI provider resolution.

Order of precedence: the provider saved for the workspace (Integraciones),
then ``LLM_MODEL`` from the environment, then the built-in demo engine. The
demo engine is not a language model and is always labelled as such.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.models import Connection
from creator_office.security import decrypt_json
from creator_office.settings import get_settings

DEMO_MODEL = "demo-local"

# Shown in Integraciones. Any other ``provider/model`` string CrewAI accepts
# can still be typed by hand.
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "prefix": "openai/",
        "example": "openai/gpt-4o-mini",
        "needs_key": True,
        "extra": "",
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "prefix": "anthropic/",
        "example": "anthropic/claude-sonnet-4-5",
        "needs_key": True,
        "extra": "anthropic",
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "prefix": "gemini/",
        "example": "gemini/gemini-2.5-flash",
        "needs_key": True,
        "extra": "google-genai",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "prefix": "openrouter/",
        "example": "openrouter/<modelo>",
        "needs_key": True,
        "extra": "",
    },
    {
        "id": "ollama",
        "name": "Ollama (local)",
        "prefix": "ollama/",
        "example": "ollama/llama3.1",
        "needs_key": False,
        "extra": "",
        "base_url": "http://localhost:11434",
    },
    {
        "id": "custom",
        "name": "Compatible con OpenAI",
        "prefix": "openai/",
        "example": "openai/<modelo>",
        "needs_key": False,
        "extra": "",
        "base_url": "https://…/v1",
    },
]


@dataclass(frozen=True)
class EngineInfo:
    mode: str  # "demo" | "provider"
    provider: str
    model: str
    source: str  # "workspace" | "env" | "builtin"
    base_url: str = ""
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None

    @property
    def is_demo(self) -> bool:
        return self.mode == "demo"

    @property
    def label(self) -> str:
        return "Motor local de demostración" if self.is_demo else self.model

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label
        return data


_DEMO = EngineInfo(mode="demo", provider=DEMO_MODEL, model=DEMO_MODEL, source="builtin")


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else "openai"


def _workspace_connection(session: Session, workspace_id: str) -> Connection | None:
    return session.scalar(
        select(Connection).where(
            Connection.workspace_id == workspace_id, Connection.provider == "llm"
        )
    )


def describe_engine(session: Session, workspace_id: str) -> EngineInfo:
    connection = _workspace_connection(session, workspace_id)
    if connection is not None and connection.config.get("model"):
        config = connection.config
        return EngineInfo(
            mode="provider",
            provider=_provider_of(config["model"]),
            model=config["model"],
            source="workspace",
            base_url=config.get("base_url", ""),
            price_input_per_mtok=config.get("price_input_per_mtok"),
            price_output_per_mtok=config.get("price_output_per_mtok"),
        )
    settings = get_settings()
    if settings.llm_model:
        return EngineInfo(
            mode="provider",
            provider=_provider_of(settings.llm_model),
            model=settings.llm_model,
            source="env",
            base_url=settings.llm_base_url,
            price_input_per_mtok=settings.llm_price_input_per_mtok,
            price_output_per_mtok=settings.llm_price_output_per_mtok,
        )
    return _DEMO


def build_llm(session: Session, workspace_id: str) -> tuple[Any, EngineInfo]:
    """Instantiate the LLM object CrewAI agents will use."""
    info = describe_engine(session, workspace_id)
    if info.is_demo:
        from creator_office.agents.demo_llm import DemoLLM

        return DemoLLM(), info

    from crewai import LLM

    kwargs: dict[str, Any] = {"model": info.model, "temperature": 0.4, "timeout": 120}
    if info.source == "workspace":
        connection = _workspace_connection(session, workspace_id)
        api_key = (
            decrypt_json(connection.secret).get("api_key", "") if connection else ""
        )
    else:
        api_key = get_settings().llm_api_key
    if api_key:
        kwargs["api_key"] = api_key
    elif info.base_url:
        kwargs["api_key"] = (
            "not-needed"  # local servers ignore it but SDKs require a value
        )
    if info.base_url:
        kwargs["base_url"] = info.base_url
    try:
        return LLM(**kwargs), info
    except ImportError as exc:
        raise RuntimeError(
            f"El proveedor «{info.provider}» necesita un paquete que no está instalado en el servidor: {exc}. "
            "Instala el extra correspondiente de CrewAI (por ejemplo: pip install 'crewai[anthropic]')."
        ) from exc


def usage_from_metrics(metrics: Any, info: EngineInfo) -> dict[str, Any]:
    """Turn CrewAI's ``UsageMetrics`` into what the UI shows, without guessing."""
    if info.is_demo:
        return {
            "engine": "demo",
            "model": DEMO_MODEL,
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_basis": "El motor de demostración se ejecuta en local y no tiene coste de IA.",
        }
    prompt = int(getattr(metrics, "prompt_tokens", 0) or 0)
    completion = int(getattr(metrics, "completion_tokens", 0) or 0)
    total = int(getattr(metrics, "total_tokens", 0) or 0) or prompt + completion
    calls = int(getattr(metrics, "successful_requests", 0) or 0)
    usage: dict[str, Any] = {
        "engine": "provider",
        "model": info.model,
        "llm_calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost_usd": None,
        "cost_basis": "",
    }
    if total == 0:
        usage["cost_basis"] = (
            "El proveedor no informó del uso de tokens en esta ejecución."
        )
    elif info.price_input_per_mtok is None or info.price_output_per_mtok is None:
        usage["cost_basis"] = (
            "Sin precios configurados: indica el precio por millón de tokens para estimar el coste."
        )
    else:
        usage["cost_usd"] = round(
            prompt / 1_000_000 * info.price_input_per_mtok
            + completion / 1_000_000 * info.price_output_per_mtok,
            6,
        )
        usage["cost_basis"] = (
            "Estimación: tokens informados por el proveedor × precios que tú configuraste."
        )
    return usage
