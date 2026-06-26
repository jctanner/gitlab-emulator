"""API version middleware that adds GitLab-compatible version headers."""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class ApiVersionMiddleware(BaseHTTPMiddleware):
    """Middleware that adds GitLab API version headers to all responses.

    Headers added:
      - X-GitLab-Media-Type: gitlab.v4; format=json
      - X-GitLab-Api-Version: v4
      - GitLab pagination headers when `page` or `per_page` is present
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["X-GitLab-Media-Type"] = "gitlab.v4; format=json"
        response.headers["X-GitLab-Api-Version"] = "v4"
        if request.url.path.startswith("/api/v4/") and (
            "page" in request.query_params or "per_page" in request.query_params
        ):
            page = request.query_params.get("page", "1")
            per_page = request.query_params.get("per_page", "30")
            try:
                page_int = max(1, int(page))
            except ValueError:
                page_int = 1
            response.headers.setdefault("X-Page", str(page_int))
            response.headers.setdefault("X-Per-Page", per_page)
            response.headers.setdefault("X-Prev-Page", str(page_int - 1) if page_int > 1 else "")
            response.headers.setdefault("X-Next-Page", "")
            response.headers.setdefault("X-Total", "")
            response.headers.setdefault("X-Total-Pages", "")
        return response
