"""FastAPI application factory.

The app is built by a function rather than created at import time so that tests
can construct one against a different database without a module-reload dance, and
so that Settings is read once, explicitly, at startup.

Three cross-cutting concerns live here rather than in the routes:

* **Rate limiting**, as middleware, so a path added later is covered by default
  instead of by remembering to add a dependency.
* **Error shape**, so a 404 from a route and a 422 from validation look the same
  to the frontend.
* **Structured logging**, because the thing an operator needs at 3am is which
  request was slow and which run was last published, not a stack trace.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import EXEMPT_PATHS, client_key, get_limiter
from app.api.routes import router
from app.chat import CHAT_PATH
from app.chat.ui import CHAT_PAGE_HTML
from app.config import Settings, get_settings
from app.db import dispose_engine
from app.domain import METHODOLOGY_VERSION
from app.logging_setup import configure_logging
from app.provenance import SOURCES_VERIFIED_ON

logger = logging.getLogger("weather_outliers")

DESCRIPTION = """
A read-only API over precomputed daily weather anomaly rankings for a curated set
of North American cities.

**Everything served here was computed once by a scheduled pipeline.** No endpoint
calls a weather provider or a language model, so responses are reproducible and a
page view costs one indexed query.

**Results are statistical outliers, not records.** Values are gridded reanalysis
or operational model estimates for the grid cell nearest each city, not readings
from a station inside it, and no authoritative records archive is consulted. See
`/api/methodology` for the formulas, the reference period, and the full list of
limitations.
"""


def _error(status_code: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "detail": detail, "status_code": status_code},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "starting weather-outliers api env=%s methodology=%s sources_verified=%s",
            settings.environment,
            METHODOLOGY_VERSION,
            SOURCES_VERIFIED_ON,
        )
        yield
        dispose_engine()
        logger.info("shutdown complete")

    app = FastAPI(
        title="Weather Outliers API",
        description=DESCRIPTION,
        version=METHODOLOGY_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {"name": "rankings", "description": "Published daily anomaly boards."},
            {"name": "cities", "description": "The curated registry and per-city detail."},
            {"name": "events", "description": "A single event with its calculation trace."},
            {"name": "meta", "description": "Health and methodology."},
            {
                "name": "chat",
                "description": "Natural-language questions answered by read-only SQL "
                "(opt-in; calls a language model and costs money).",
            },
        ],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # No credentials: the API is public and unauthenticated, so there is
        # nothing for a cookie to carry and no reason to widen the CORS surface.
        # POST is allowed only so the chat endpoint (a read-only SQL gateway)
        # can be called from a browser; every other route rejects POST with 405.
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        max_age=3600,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject write methods, rate-limit, then log the outcome with a timing."""
        started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]

        # The API is read-only by design, not just by the absence of handlers.
        # Rejecting here means a future mistake surfaces as a 405, not a mutation.
        # The one exception is the chat endpoint, whose POST is a read-only SQL
        # gateway — it never mutates the database.
        allowed_method = request.method in ("GET", "HEAD", "OPTIONS")
        allowed_method = allowed_method or (
            request.method == "POST" and request.url.path == CHAT_PATH
        )
        if not allowed_method:
            return _error(
                status.HTTP_405_METHOD_NOT_ALLOWED,
                "method_not_allowed",
                "This API is read-only. Only GET, HEAD, and OPTIONS are accepted.",
            )

        path = request.url.path
        if settings.rate_limit_enabled and path not in EXEMPT_PATHS:
            limiter = get_limiter(settings)
            allowed, remaining, retry_after = limiter.check(client_key(request, settings))
            if not allowed:
                logger.warning(
                    "rate limited path=%s request_id=%s retry_after=%s",
                    path,
                    request_id,
                    retry_after,
                )
                response = _error(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "rate_limited",
                    f"Too many requests. Retry in {retry_after}s. "
                    f"The published data changes once a day, so caching is safe.",
                )
                response.headers["Retry-After"] = str(retry_after)
                response.headers["X-RateLimit-Remaining"] = "0"
                response.headers["X-Request-ID"] = request_id
                return response
        else:
            remaining = -1

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "unhandled error path=%s request_id=%s elapsed_ms=%.1f",
                path,
                request_id,
                elapsed_ms,
            )
            return _error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "internal_error",
                "An unexpected error occurred. The incident has been logged.",
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Methodology-Version"] = METHODOLOGY_VERSION
        if remaining >= 0:
            response.headers["X-RateLimit-Remaining"] = str(remaining)

        log = logger.warning if elapsed_ms > 1000 else logger.info
        log(
            "%s %s -> %s in %.1fms request_id=%s",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        names = {404: "not_found", 422: "invalid_request", 405: "method_not_allowed"}
        return _error(
            exc.status_code,
            names.get(exc.status_code, "error"),
            str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'][1:]) or 'request'}: {err['msg']}"
            for err in exc.errors()
        )
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", problems
        )

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "name": "Weather Outliers API",
            "result_type": "statistical_outliers",
            "methodology_version": METHODOLOGY_VERSION,
            "docs": "/docs",
            "latest": "/api/rankings/latest",
            "methodology": "/api/methodology",
        }

    @app.get("/chat", include_in_schema=False)
    async def chat_page() -> HTMLResponse:
        """The chat page. The endpoint it calls is off unless CHAT_ENABLED is set."""
        return HTMLResponse(CHAT_PAGE_HTML)

    return app


app = create_app()
