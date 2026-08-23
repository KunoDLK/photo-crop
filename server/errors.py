"""Error types and HTTP exception handling.

Defines a small :class:`AppError` hierarchy so lower layers (scanner, tiler)
can raise domain errors without knowing about HTTP, and a registration helper
that maps those errors onto JSON responses in the FastAPI app.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for expected, user-facing errors.

    Attributes:
        status_code: HTTP status to return to the client.
        message: Human-readable explanation (returned as JSON).
    """

    status_code: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(AppError):
    """Requested resource does not exist."""

    status_code = 404


class BadRequest(AppError):
    """Malformed or invalid request parameters."""

    status_code = 400


def register_error_handlers(app: FastAPI) -> None:
    """Install exception handlers mapping :class:`AppError` subclasses to JSON.

    Args:
        app: The FastAPI application to configure.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        del request  # unused; kept for signature clarity
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message},
        )
