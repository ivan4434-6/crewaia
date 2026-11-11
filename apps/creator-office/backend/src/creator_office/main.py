"""Creator Office AI — web application entry point."""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from creator_office.settings import configure_crewai_environment, get_settings

# CrewAI reads its telemetry and storage settings when it is first imported.
configure_crewai_environment()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from creator_office import __version__, security  # noqa: E402
from creator_office.agents import bridge  # noqa: E402
from creator_office.api import (  # noqa: E402
    auth,
    insights,
    office,
    projects,
    publishing,
    studio,
)
from creator_office.database import init_db  # noqa: E402
from creator_office.pipeline import worker  # noqa: E402

logger = logging.getLogger("creator_office")

_VALIDATION_MESSAGES = {
    "missing": "Falta este dato.",
    "string_too_short": "Es demasiado corto.",
    "string_too_long": "Es demasiado largo.",
    "too_long": "Hay demasiados elementos.",
    "greater_than": "El valor es demasiado bajo.",
    "greater_than_equal": "El valor es demasiado bajo.",
    "less_than_equal": "El valor es demasiado alto.",
    "datetime_from_date_parsing": "La fecha no es válida.",
    "datetime_parsing": "La fecha no es válida.",
    "float_parsing": "Debe ser un número.",
    "int_parsing": "Debe ser un número entero.",
    "bool_parsing": "Debe ser sí o no.",
    "json_invalid": "Los datos enviados no son JSON válido.",
}


_FRAME_OPTIONS = {"'self'": "SAMEORIGIN", "'none'": "DENY"}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_db()
    security.fernet()  # fail at start-up, not on first use, if the secret key is unusable
    bridge.install()
    if get_settings().worker_autostart:
        worker.start()
    try:
        yield
    finally:
        worker.stop()


def _error(status: int, message: str, code: str = "", **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"message": message, "code": code, **extra},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


# Content types a browser checks before it runs or applies a file. They are stated
# here rather than left to the system: on Windows ``mimetypes`` copies the registry,
# where ``.js`` is often ``text/plain``, and with ``nosniff`` the page would stay blank.
_WEB_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".svg": "image/svg+xml",
}


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Creator Office AI",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if isinstance(exc.detail, dict):
            detail = dict(exc.detail)
            return _error(
                exc.status_code,
                detail.pop("message", "Error"),
                detail.pop("code", ""),
                **detail,
            )
        defaults = {
            404: "No encontrado.",
            405: "Método no permitido.",
            401: "Inicia sesión para continuar.",
        }
        return _error(
            exc.status_code,
            defaults.get(exc.status_code, str(exc.detail)),
            "http_error",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: dict[str, str] = {}
        for error in exc.errors():
            location = [
                str(part)
                for part in error.get("loc", ())
                if part not in ("body", "query", "path")
            ]
            fields[".".join(location) or "datos"] = _VALIDATION_MESSAGES.get(
                error.get("type", ""), "El valor no es válido."
            )
        first = next(iter(fields.items()), ("datos", "El valor no es válido."))
        return _error(
            422,
            f"Revisa los datos enviados ({first[0]}): {first[1]}",
            "validation",
            fields=fields,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled error on %s %s", request.method, request.url.path, exc_info=exc
        )
        return _error(
            500,
            "Error interno del servidor. El fallo ha quedado registrado; inténtalo de nuevo.",
            "internal",
        )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        ancestors = settings.allowed_frame_ancestors
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self'; connect-src 'self'; font-src 'self' data:; base-uri 'self'; form-action 'self'; frame-ancestors {ancestors}",
        )
        if (
            ancestors in _FRAME_OPTIONS
        ):  # the older header cannot express a list of origins
            response.headers.setdefault("X-Frame-Options", _FRAME_OPTIONS[ancestors])
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if (
            request.url.path.startswith("/api/")
            and "cache-control" not in response.headers
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    for module in (auth, projects, studio, publishing, insights, office):
        app.include_router(module.router)

    @app.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def api_not_found(rest: str) -> JSONResponse:
        return _error(404, "Esa ruta de la API no existe.", "not_found")

    for suffix, content_type in _WEB_TYPES.items():
        mimetypes.add_type(content_type, suffix)
    dist = settings.frontend_dist
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        """Serve the built frontend; every unknown path is a client-side route."""
        index = dist / "index.html"
        if not index.is_file():
            return JSONResponse(
                {
                    "message": "El servidor está en marcha, pero la interfaz no está compilada. "
                    "Ejecuta «npm install && npm run build» en apps/creator-office/frontend.",
                    "api": "/api/health",
                },
                status_code=503,
            )
        candidate = (dist / path).resolve() if path else index
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()  # HOST, PORT and LOG_LEVEL come from the environment or the .env file, like everything else
    uvicorn.run(
        "creator_office.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()
