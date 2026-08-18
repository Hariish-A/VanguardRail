"""FastAPI application factory.

The same `app` object is served two ways with no behavioural difference:

* on AWS Lambda, wrapped by Mangum in `handler.py`
* in a container or locally, by uvicorn

Keeping a single app means the thing verified locally is the thing deployed, and it
keeps the control plane portable to ECS/EKS/on-prem rather than Lambda-locked.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from guardrail_service import __version__
from guardrail_service.config import get_settings
from guardrail_service.observability import logger
from guardrail_service.routers import audit, evaluate, health

REQUEST_ID_HEADER = "x-request-id"


class ErrorResponse(BaseModel):
    """A uniform error shape.

    Every failure the API can produce uses this envelope so SDK clients have exactly
    one error contract to parse. `request_id` is the value to quote when asking why
    a particular decision came out the way it did.
    """

    error: str
    detail: str
    request_id: str


def create_app() -> FastAPI:
    """Build the application. Called at import time by both entrypoints."""
    settings = get_settings()

    app = FastAPI(
        title="Guardrail",
        summary="Action-layer governance for AI agents.",
        description=(
            "Evaluates a tool call against a declarative policy bundle *before* it is "
            "dispatched, returning one of: allow, log_and_allow, require_hitl, block."
        ),
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[JSONResponse]],
    ) -> JSONResponse:
        """Attach a request id and emit one structured access log per request.

        The id is honoured from the caller when supplied, so a trace can be followed
        across the agent, the SDK, and this service — which matters when explaining
        after the fact why a specific action was blocked.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        logger.append_keys(request_id=request_id)
        started = time.perf_counter()

        # The key is removed only after the access log is written. Clearing it in a
        # `finally` that runs before the log call would strip request_id from the very
        # line it exists to correlate.
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Log with a stack trace, then re-raise into the handler below so the
                # client still receives the uniform error envelope.
                logger.exception(
                    "unhandled_error",
                    extra={"path": request.url.path, "method": request.method},
                )
                raise

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "request",
                extra={
                    "path": request.url.path,
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            return response
        finally:
            # Lambda reuses a warm container across invocations, so a leaked key would
            # attach one request's id to the next request's logs.
            logger.remove_keys(["request_id"])

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Return the uniform error envelope instead of a bare 500.

        The exception text is deliberately not echoed to the caller: this service
        sees tool-call arguments, which routinely contain data the caller should not
        have handed us in the first place. The stack trace goes to CloudWatch.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER, "unknown")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="internal_error",
                detail="An unexpected error occurred. Quote the request_id when reporting this.",
                request_id=request_id,
            ).model_dump(),
        )

    app.include_router(health.router)
    app.include_router(evaluate.router)
    app.include_router(audit.router)

    logger.info(
        "app_initialised",
        extra={"stage": settings.stage, "version": settings.version},
    )
    return app


app = create_app()
