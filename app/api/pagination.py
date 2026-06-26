"""GitLab-style REST pagination helpers."""

from math import ceil
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def pagination_headers(request: Request, page: int, per_page: int, total: int) -> dict[str, str]:
    total_pages = ceil(total / per_page) if total else 0
    prev_page = page - 1 if page > 1 and total_pages else None
    next_page = page + 1 if total_pages and page < total_pages else None
    headers = {
        "X-Total": str(total),
        "X-Total-Pages": str(total_pages),
        "X-Page": str(page),
        "X-Per-Page": str(per_page),
        "X-Prev-Page": str(prev_page or ""),
        "X-Next-Page": str(next_page or ""),
    }
    links: list[str] = []
    if next_page:
        links.append(
            f'<{request.url.include_query_params(page=next_page, per_page=per_page)}>; rel="next"'
        )
        links.append(
            f'<{request.url.include_query_params(page=total_pages, per_page=per_page)}>; rel="last"'
        )
    if prev_page:
        links.append(
            f'<{request.url.include_query_params(page=prev_page, per_page=per_page)}>; rel="prev"'
        )
        links.append(
            f'<{request.url.include_query_params(page=1, per_page=per_page)}>; rel="first"'
        )
    if links:
        headers["Link"] = ", ".join(links)
    return headers


def paginated_json(
    content: Any,
    request: Request,
    page: int,
    per_page: int,
    total: int,
) -> JSONResponse:
    return JSONResponse(
        content=content,
        headers=pagination_headers(request, page, per_page, total),
    )
