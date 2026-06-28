"""Helpers for resolving issue and merge request closing references."""

import re


_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?P<number>\d+)\b",
    re.IGNORECASE,
)


def closing_issue_numbers(body: str | None) -> list[int]:
    """Extract same-repository issue numbers from common closing keywords."""
    numbers: list[int] = []
    seen: set[int] = set()
    for match in _CLOSING_RE.finditer(body or ""):
        number = int(match.group("number"))
        if number not in seen:
            seen.add(number)
            numbers.append(number)
    return numbers
