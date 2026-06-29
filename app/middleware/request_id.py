"""Request ID headers for GitLab-compatible API tracing."""

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Propagate or generate request IDs on every response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = (
            request.headers.get("X-Request-Id")
            or request.headers.get("X-GitLab-Request-Id")
            or uuid4().hex
        )
        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("X-GitLab-Request-Id", request_id)
        return response
