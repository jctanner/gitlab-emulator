"""Exception handlers that return GitLab-format error JSON responses."""

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DOCS_URL = "https://docs.gitlab.com/rest"


# ── Custom exception classes ──────────────────────────────────────────


class GitLabError(Exception):
    """Base exception for GitLab API errors."""

    def __init__(
        self,
        message: str = "An error occurred",
        status_code: int = 500,
        errors: Optional[list[dict[str, Any]]] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.errors = errors
        super().__init__(self.message)


class NotFoundError(GitLabError):
    """404 Not Found error."""

    def __init__(self, message: str = "Not Found"):
        super().__init__(message=message, status_code=404)


class ValidationError(GitLabError):
    """422 Validation Failed error."""

    def __init__(
        self,
        message: str = "Validation Failed",
        errors: Optional[list[dict[str, Any]]] = None,
    ):
        super().__init__(message=message, status_code=422, errors=errors)


class AuthenticationError(GitLabError):
    """401 Requires authentication error."""

    def __init__(self, message: str = "Requires authentication"):
        super().__init__(message=message, status_code=401)


class ForbiddenError(GitLabError):
    """403 Forbidden error."""

    def __init__(self, message: str = "Forbidden"):
        super().__init__(message=message, status_code=403)


# ── Exception handlers ───────────────────────────────────────────────


def _build_error_response(
    status_code: int,
    message: str,
    errors: Optional[list[dict[str, Any]]] = None,
) -> JSONResponse:
    """Build a GitLab-format JSON error response.

    Args:
        status_code: HTTP status code.
        message: Error message.
        errors: Optional list of validation error details.

    Returns:
        A JSONResponse with the appropriate body and status code.
    """
    body: dict[str, Any] = {
        "message": message,
        "documentation_url": DOCS_URL,
    }
    if errors is not None:
        body["errors"] = errors

    return JSONResponse(status_code=status_code, content=body)


async def gitlab_error_handler(request: Request, exc: GitLabError) -> JSONResponse:
    """Handle GitLabError exceptions."""
    return _build_error_response(exc.status_code, exc.message, exc.errors)


async def http_401_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle 401 Unauthorized."""
    resp = _build_error_response(
        401,
        _http_error_message(exc, "Requires authentication"),
    )
    # Preserve WWW-Authenticate header so git clients know to send credentials
    if hasattr(exc, "headers") and exc.headers:
        for k, v in exc.headers.items():
            resp.headers[k] = v
    return resp


async def http_403_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle 403 Forbidden."""
    return _build_error_response(403, _http_error_message(exc, "Forbidden"))


async def http_404_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle 404 Not Found."""
    return _build_error_response(404, _http_error_message(exc, "Not Found"))


async def http_422_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle 422 Validation Failed."""
    return _build_error_response(422, _http_error_message(exc, "Validation Failed"))


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions."""
    return _build_error_response(500, "Internal Server Error")


def register_error_handlers(app: FastAPI) -> None:
    """Register all error handlers with a FastAPI application.

    Args:
        app: The FastAPI application instance.
    """
    from starlette.exceptions import HTTPException

    app.add_exception_handler(GitLabError, gitlab_error_handler)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        handlers = {
            401: http_401_handler,
            403: http_403_handler,
            404: http_404_handler,
            422: http_422_handler,
        }
        handler = handlers.get(exc.status_code)
        if handler:
            return await handler(request, exc)
        return _build_error_response(exc.status_code, exc.detail)


def _http_error_message(exc: Exception, fallback: str) -> str:
    detail = getattr(exc, "detail", None)
    return detail if isinstance(detail, str) and detail else fallback
