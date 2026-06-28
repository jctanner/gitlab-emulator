"""CI security diagnostics for pipeline creation."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import yaml

from app.services.ci_yaml import ParsedCiJob


DEFAULT_CI_SECURITY_SETTINGS = {
    "ci_pipeline_variables_minimum_override_role": "developer",
    "ci_strict_security_mode": False,
}
PREDEFINED_PREFIXES = ("CI_", "GITLAB_")
PINNED_DIGEST_RE = re.compile(r"@sha256:[0-9a-fA-F]{64}$")
SHA_HINT_RE = re.compile(r"([?&](sha|checksum|integrity)=|[0-9a-fA-F]{40}|sha256:[0-9a-fA-F]{64})")
STRICT_BLOCK_TYPES = {
    "mutable_image_ref",
    "variable_image_ref",
    "unsafe_remote_include",
    "unpinned_remote_include",
}


def normalize_ci_security_settings(settings: dict | None) -> dict:
    normalized = dict(DEFAULT_CI_SECURITY_SETTINGS)
    if isinstance(settings, dict):
        normalized.update(
            {
                key: value
                for key, value in settings.items()
                if key in DEFAULT_CI_SECURITY_SETTINGS
            }
        )
    role = normalized.get("ci_pipeline_variables_minimum_override_role")
    if role not in {"developer", "maintainer", "owner", "no_one_allowed"}:
        normalized["ci_pipeline_variables_minimum_override_role"] = "developer"
    normalized["ci_strict_security_mode"] = bool(
        normalized.get("ci_strict_security_mode", False)
    )
    return normalized


def _warning(
    warning_type: str,
    message: str,
    *,
    job: str | None = None,
    value: str | None = None,
    strict: bool = False,
) -> dict:
    warning = {
        "type": warning_type,
        "severity": "warning",
        "message": message,
        "strict_mode": strict,
    }
    if job:
        warning["job"] = job
    if value:
        warning["value"] = value
    return warning


def _image_ref_is_mutable(image: str) -> bool:
    if PINNED_DIGEST_RE.search(image):
        return False
    name = image.rsplit("/", 1)[-1]
    if ":" not in name:
        return True
    return name.rsplit(":", 1)[-1] == "latest"


def _remote_include_urls(ci_content: str) -> list[str]:
    try:
        parsed = yaml.safe_load(ci_content) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict):
        return []
    raw_include = parsed.get("include")
    if raw_include is None:
        return []
    include_items = raw_include if isinstance(raw_include, list) else [raw_include]
    urls: list[str] = []
    for item in include_items:
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            urls.append(item)
        elif isinstance(item, dict) and item.get("remote"):
            raw_remote = item["remote"]
            remote_values = raw_remote if isinstance(raw_remote, list) else [raw_remote]
            urls.extend(str(url) for url in remote_values)
    return urls


def pipeline_security_warnings(
    *,
    ci_content: str,
    parsed_jobs: list[ParsedCiJob],
    pipeline_variables: list[Any],
    settings: dict | None = None,
) -> list[dict]:
    security_settings = normalize_ci_security_settings(settings)
    strict = bool(security_settings["ci_strict_security_mode"])
    warnings: list[dict] = []

    for job in parsed_jobs:
        if "$" in job.image:
            warnings.append(
                _warning(
                    "variable_image_ref",
                    f"Job {job.name} uses variable interpolation in its image.",
                    job=job.name,
                    value=job.image,
                    strict=strict,
                )
            )
        if _image_ref_is_mutable(job.image):
            warnings.append(
                _warning(
                    "mutable_image_ref",
                    f"Job {job.name} uses a mutable image reference.",
                    job=job.name,
                    value=job.image,
                    strict=strict,
                )
            )

    for url in _remote_include_urls(ci_content):
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https":
            warnings.append(
                _warning(
                    "unsafe_remote_include",
                    "Remote CI include does not use HTTPS.",
                    value=url,
                    strict=strict,
                )
            )
        if not SHA_HINT_RE.search(url):
            warnings.append(
                _warning(
                    "unpinned_remote_include",
                    "Remote CI include is not pinned to an immutable reference.",
                    value=url,
                    strict=strict,
                )
            )

    for variable in pipeline_variables:
        key = getattr(variable, "key", None)
        if key and str(key).startswith(PREDEFINED_PREFIXES):
            warnings.append(
                _warning(
                    "predefined_variable_override",
                    f"Pipeline variable {key} overrides a predefined CI variable.",
                    value=str(key),
                    strict=strict,
                )
            )
    return warnings


def strict_security_blocks(warnings: list[dict], settings: dict | None = None) -> list[dict]:
    security_settings = normalize_ci_security_settings(settings)
    if not security_settings["ci_strict_security_mode"]:
        return []
    return [
        {**warning, "severity": "error"}
        for warning in warnings
        if warning.get("type") in STRICT_BLOCK_TYPES
    ]


def pipeline_variable_policy(settings: dict | None) -> str:
    role = normalize_ci_security_settings(settings)[
        "ci_pipeline_variables_minimum_override_role"
    ]
    return str(role)
