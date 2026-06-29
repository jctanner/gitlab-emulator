"""Minimal `.gitlab-ci.yml` parser for the pipeline MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
from itertools import product
import re
from typing import Any

import yaml


RESERVED_TOP_LEVEL_KEYS = {
    "after_script",
    "before_script",
    "cache",
    "default",
    "except",
    "include",
    "image",
    "only",
    "secrets",
    "services",
    "stages",
    "variables",
    "workflow",
}

DEFAULT_INHERITABLE_KEYS = {
    "after_script",
    "artifacts",
    "before_script",
    "cache",
    "image",
    "interruptible",
    "retry",
    "services",
    "tags",
    "timeout",
}
MAX_EXTENDS_DEPTH = 11
UNSUPPORTED_JOB_KEYS = {
}
SUPPORTED_SERVICE_ENTRY_KEYS = {
    "name",
    "alias",
    "command",
    "docker",
    "entrypoint",
    "kubernetes",
    "variables",
    "pull_policy",
}
SUPPORTED_IMAGE_KEYS = {
    "name",
    "docker",
    "entrypoint",
    "kubernetes",
    "pull_policy",
}
SUPPORTED_CACHE_ENTRY_KEYS = {
    "key",
    "paths",
    "untracked",
    "policy",
    "when",
    "fallback_keys",
    "unprotect",
}
SUPPORTED_CACHE_POLICIES = {"pull", "push", "pull-push"}
SUPPORTED_CACHE_WHEN = {"on_success", "on_failure", "always"}
SUPPORTED_JOB_WHEN = {
    "on_success",
    "on_failure",
    "always",
    "manual",
    "never",
    "delayed",
}
DURATION_SECONDS_BY_UNIT = {
    "second": 1,
    "seconds": 1,
    "sec": 1,
    "secs": 1,
    "s": 1,
    "minute": 60,
    "minutes": 60,
    "min": 60,
    "mins": 60,
    "m": 60,
    "hour": 3600,
    "hours": 3600,
    "hr": 3600,
    "hrs": 3600,
    "h": 3600,
    "day": 86400,
    "days": 86400,
    "d": 86400,
    "week": 604800,
    "weeks": 604800,
    "w": 604800,
}


@dataclass
class ParsedCiJob:
    name: str
    stage: str = "test"
    stage_index: int = 0
    image: str = "alpine:3.20"
    image_config: dict = field(default_factory=dict)
    script: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    variable_metadata: dict[str, dict] = field(default_factory=dict)
    needs: list[dict] | None = None
    dependencies: list[str] | None = None
    tags: list[str] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    cache: list[dict] = field(default_factory=list)
    artifacts_paths: list[str] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    when: str = "on_success"
    start_in_seconds: int | None = None
    allow_failure: bool = False
    allow_failure_exit_codes: list[int] = field(default_factory=list)
    retry: dict = field(default_factory=dict)
    timeout_seconds: int | None = None
    interruptible: bool = False
    resource_group: str | None = None
    coverage: str | None = None
    environment: str | None = None
    environment_url: str | None = None
    environment_action: str | None = None
    secrets: dict[str, dict] = field(default_factory=dict)
    trigger: dict | None = None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _matrix_suffix(values: list[Any]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def _variables(value: Any) -> dict[str, str]:
    return {key: str(entry["value"]) for key, entry in _variable_entries(value).items()}


def _variable_entries(value: Any) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    entries: dict[str, dict] = {}
    for key, raw_value in value.items():
        variable_key = str(key)
        if isinstance(raw_value, dict):
            variable_value = raw_value.get("value", "")
            variable_type = str(
                raw_value.get("variable_type") or raw_value.get("type") or "env_var"
            )
            is_file = bool(raw_value.get("file", False)) or variable_type == "file"
            raw = bool(raw_value.get("raw", False)) or raw_value.get("expand") is False
            masked = bool(raw_value.get("masked", False))
        else:
            variable_value = raw_value
            is_file = False
            raw = False
            masked = False
        entries[variable_key] = {
            "value": str(variable_value),
            "file": is_file,
            "masked": masked,
            "raw": raw,
            "public": not masked,
        }
    return entries


def _variable_values(entries: dict[str, dict]) -> dict[str, str]:
    return {key: str(entry.get("value", "")) for key, entry in entries.items()}


def _image_name(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("name"):
        return str(value["name"])
    return fallback


def _image_config(value: Any, variables: dict[str, str]) -> dict:
    if not isinstance(value, dict):
        return {}
    unsupported = sorted(set(value) - SUPPORTED_IMAGE_KEYS)
    if unsupported:
        raise ValueError(f"image option(s) not supported: {', '.join(unsupported)}")
    config: dict[str, Any] = {}
    if value.get("entrypoint") is not None:
        config["entrypoint"] = _expand_string_list(value.get("entrypoint"), variables)
    if value.get("pull_policy") is not None:
        config["pull_policy"] = _expand_string_list(value.get("pull_policy"), variables)
    if value.get("docker") is not None:
        config["docker"] = (
            value.get("docker") if isinstance(value.get("docker"), dict) else {}
        )
    if value.get("kubernetes") is not None:
        config["kubernetes"] = (
            value.get("kubernetes") if isinstance(value.get("kubernetes"), dict) else {}
        )
    return config


def _environment_config(
    value: Any,
    variables: dict[str, str] | None = None,
) -> dict[str, str | None]:
    variables = variables or {}
    if value is None:
        return {"name": None, "url": None, "action": None}
    if isinstance(value, str):
        return {
            "name": _expand_ci_variables(value, variables),
            "url": None,
            "action": None,
        }
    if isinstance(value, dict) and value.get("name"):
        return {
            "name": _expand_ci_variables(str(value["name"]), variables),
            "url": _expand_ci_variables(str(value["url"]), variables)
            if value.get("url") is not None
            else None,
            "action": str(value["action"]) if value.get("action") is not None else None,
        }
    return {"name": None, "url": None, "action": None}


def _service_entry(raw_value: Any, variables: dict[str, str]) -> dict | None:
    if isinstance(raw_value, str):
        name = _expand_ci_variables(raw_value, variables)
        return {"name": name} if name else None
    if not isinstance(raw_value, dict):
        raise ValueError("services entries must be strings or mappings")
    unsupported = sorted(set(raw_value) - SUPPORTED_SERVICE_ENTRY_KEYS)
    if unsupported:
        raise ValueError(f"services option(s) not supported: {', '.join(unsupported)}")
    if not raw_value.get("name"):
        raise ValueError("services entries must define a name")
    service = {
        "name": _expand_ci_variables(str(raw_value["name"]), variables),
    }
    if raw_value.get("alias") is not None:
        service["alias"] = _expand_ci_variables(str(raw_value["alias"]), variables)
    if raw_value.get("command") is not None:
        service["command"] = _expand_string_list(raw_value.get("command"), variables)
    if raw_value.get("entrypoint") is not None:
        service["entrypoint"] = _expand_string_list(
            raw_value.get("entrypoint"),
            variables,
        )
    if raw_value.get("pull_policy") is not None:
        service["pull_policy"] = _expand_string_list(
            raw_value.get("pull_policy"),
            variables,
        )
    if raw_value.get("docker") is not None:
        service["docker"] = (
            raw_value.get("docker")
            if isinstance(raw_value.get("docker"), dict)
            else {}
        )
    if raw_value.get("kubernetes") is not None:
        service["kubernetes"] = (
            raw_value.get("kubernetes")
            if isinstance(raw_value.get("kubernetes"), dict)
            else {}
        )
    if raw_value.get("variables") is not None:
        service["variables"] = [
            _variable_payload_entry(key, entry, variables)
            for key, entry in _variable_entries(raw_value.get("variables")).items()
        ]
    return service


def _variable_payload_entry(
    key: str, entry: dict[str, Any], variables: dict[str, str]
) -> dict:
    masked = bool(entry.get("masked", False))
    return {
        "key": key,
        "value": _expand_ci_variables(str(entry.get("value", "")), variables),
        "public": bool(entry.get("public", not masked)),
        "file": bool(entry.get("file", False)),
        "masked": masked,
        "raw": bool(entry.get("raw", False)),
    }


def _service_entries(value: Any, variables: dict[str, str]) -> list[dict]:
    if value is None:
        return []
    raw_entries = value if isinstance(value, list) else [value]
    services: list[dict] = []
    for raw_entry in raw_entries:
        service = _service_entry(raw_entry, variables)
        if service:
            services.append(service)
    return services


def _secret_entries(value: Any) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    entries: dict[str, dict] = {}
    for key, raw_value in value.items():
        variable_key = str(key)
        if isinstance(raw_value, str):
            entries[variable_key] = {
                "name": raw_value,
                "file": True,
            }
            continue
        if not isinstance(raw_value, dict):
            continue

        provider = raw_value.get("gitlab_secrets_manager")
        provider_config: dict[str, Any]
        if isinstance(provider, str):
            provider_config = {"name": provider}
        elif isinstance(provider, dict):
            provider_config = provider
        elif raw_value.get("name"):
            provider_config = raw_value
        else:
            provider_config = {}

        entries[variable_key] = {
            "name": str(provider_config.get("name") or variable_key),
            "file": bool(raw_value.get("file", provider_config.get("file", True))),
        }
        if provider_config.get("environment_scope"):
            entries[variable_key]["environment_scope"] = str(
                provider_config["environment_scope"]
            )
        if provider_config.get("branch_scope"):
            entries[variable_key]["branch_scope"] = str(provider_config["branch_scope"])
    return entries


def _needs(
    value: Any,
    parallel_job_names: dict[str, list[str]] | None = None,
) -> list[dict] | None:
    parallel_job_names = parallel_job_names or {}
    if value is None:
        return None
    if value == []:
        return []
    if isinstance(value, str):
        return [{"job": value, "optional": False, "artifacts": True}]
    if isinstance(value, dict):
        if value.get("project"):
            return [_cross_project_need_entry(value)]
        if value.get("pipeline"):
            return [_pipeline_need_entry(value)]
        if value.get("job"):
            return _need_entries_from_mapping(value, parallel_job_names)
        raise ValueError("needs entries must define a job")
    if isinstance(value, list):
        parsed: list[dict] = []
        for item in value:
            if isinstance(item, str):
                parsed.append({"job": item, "optional": False, "artifacts": True})
                continue
            if not isinstance(item, dict):
                raise ValueError("needs entries must be strings or mappings")
            if item.get("project"):
                parsed.append(_cross_project_need_entry(item))
                continue
            if item.get("pipeline"):
                parsed.append(_pipeline_need_entry(item))
                continue
            if not item.get("job"):
                raise ValueError("needs entries must define a job")
            parsed.extend(_need_entries_from_mapping(item, parallel_job_names))
        return parsed
    raise ValueError("needs must be a string, mapping, or list")


def _cross_project_need_entry(value: dict) -> dict:
    if not value.get("job"):
        raise ValueError("needs:project entries must define a job")
    if not value.get("ref"):
        raise ValueError("needs:project entries must define a ref")
    artifacts = bool(value.get("artifacts", True))
    if not artifacts:
        raise ValueError("needs:project entries must use artifacts: true")
    return {
        "project": str(value["project"]),
        "job": str(value["job"]),
        "ref": str(value["ref"]),
        "optional": bool(value.get("optional", False)),
        "artifacts": artifacts,
    }


def _pipeline_need_entry(value: dict) -> dict:
    if not value.get("job"):
        raise ValueError("needs:pipeline entries must define a job")
    artifacts = bool(value.get("artifacts", True))
    if not artifacts:
        raise ValueError("needs:pipeline entries must use artifacts: true")
    return {
        "pipeline": str(value["pipeline"]),
        "job": str(value["job"]),
        "optional": bool(value.get("optional", False)),
        "artifacts": artifacts,
    }


def _need_entries_from_mapping(
    value: dict,
    parallel_job_names: dict[str, list[str]] | None = None,
) -> list[dict]:
    parallel_job_names = parallel_job_names or {}
    job_name = str(value["job"])
    optional = bool(value.get("optional", False))
    artifacts = bool(value.get("artifacts", True))
    parallel = value.get("parallel")
    if parallel is None:
        return [{"job": job_name, "optional": optional, "artifacts": artifacts}]
    if parallel is True:
        return [
            {"job": expanded_name, "optional": optional, "artifacts": artifacts}
            for expanded_name in parallel_job_names.get(job_name, [job_name])
        ]
    if not isinstance(parallel, dict) or set(parallel) - {"matrix"}:
        raise ValueError("needs parallel value is not supported")
    matrix = parallel.get("matrix")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("needs parallel matrix must be a non-empty list")
    entries: list[dict] = []
    for entry in matrix:
        if not isinstance(entry, dict) or not entry:
            raise ValueError("needs parallel matrix entries must be mappings")
        values_by_key = [
            values if isinstance(values, list) else [values]
            for values in entry.values()
        ]
        for values in product(*values_by_key):
            entries.append(
                {
                    "job": f"{job_name} {_matrix_suffix(list(values))}",
                    "optional": optional,
                    "artifacts": artifacts,
                }
            )
    return entries


def _dependencies(value: Any) -> list[str] | None:
    if value is None:
        return None
    if value == []:
        return []
    if not isinstance(value, list):
        raise ValueError("dependencies must be a list of job names")
    dependencies: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("dependencies entries must be job names")
        dependencies.append(item)
    return dependencies


def _expand_ci_variables(value: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        braced, plain = match.groups()
        return variables.get(braced or plain, match.group(0))

    expanded = value
    for _ in range(5):
        next_value = re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
            replace,
            expanded,
        )
        if next_value == expanded:
            break
        expanded = next_value
    return expanded


def _expand_string_list(value: Any, variables: dict[str, str]) -> list[str]:
    return [_expand_ci_variables(item, variables) for item in _string_list(value)]


def _bool_value(value: Any, variables: dict[str, str] | None = None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        expanded = _expand_ci_variables(value, variables or {}).strip().lower()
        if expanded in {"", "0", "false", "no", "off", "null", "none"}:
            return False
        if expanded in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


def _cache_key_digest(paths: list[str], key_map: dict[str, str] | None) -> str | None:
    if not paths or not key_map:
        return None
    values = [
        f"{path}\0{key_map[path]}"
        for path in paths
        if path in key_map and key_map[path]
    ]
    if not values:
        return None
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _cache_key_from_paths(paths: list[str], key_map: dict[str, str] | None) -> str:
    if key_map is None:
        return "-".join(paths) if paths else "default"
    return _cache_key_digest(paths, key_map) or "default"


def _cache_key(
    value: Any,
    variables: dict[str, str] | None = None,
    *,
    cache_key_files: dict[str, str] | None = None,
    cache_key_files_commits: dict[str, str] | None = None,
) -> str:
    variables = variables or {}
    if value is None:
        return "default"
    if isinstance(value, str):
        return _expand_ci_variables(value, variables)
    if isinstance(value, list):
        files = _expand_string_list(value, variables)
        return "-".join(files) if files else "default"
    if isinstance(value, dict):
        prefix = _expand_ci_variables(str(value.get("prefix") or "").strip(), variables)
        if value.get("key"):
            key = _expand_ci_variables(str(value["key"]), variables)
            return f"{prefix}-{key}" if prefix else key
        files = _expand_string_list(value.get("files"), variables)
        if files:
            key = _cache_key_from_paths(files, cache_key_files)
            return f"{prefix}-{key}" if prefix else key
        files_commits = _expand_string_list(value.get("files_commits"), variables)
        if files_commits:
            key = _cache_key_from_paths(files_commits, cache_key_files_commits)
            return f"{prefix}-{key}" if prefix else key
        if prefix:
            return prefix
    return "default"


def _cache_entries(
    value: Any,
    variables: dict[str, str] | None = None,
    *,
    cache_key_files: dict[str, str] | None = None,
    cache_key_files_commits: dict[str, str] | None = None,
) -> list[dict]:
    if value is None or value is False:
        return []
    variables = variables or {}
    raw_entries = value if isinstance(value, list) else [value]
    entries: list[dict] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        unsupported = sorted(set(raw_entry) - SUPPORTED_CACHE_ENTRY_KEYS)
        if unsupported:
            raise ValueError(f"cache option(s) not supported: {', '.join(unsupported)}")
        paths = _expand_string_list(raw_entry.get("paths"), variables)
        untracked = _bool_value(raw_entry.get("untracked", False), variables)
        if not paths and not untracked:
            continue
        policy = _expand_ci_variables(
            str(raw_entry.get("policy") or "pull-push"),
            variables,
        )
        if policy not in SUPPORTED_CACHE_POLICIES:
            raise ValueError(f"cache policy is not supported: {policy}")
        when = _expand_ci_variables(
            str(raw_entry.get("when") or "on_success"),
            variables,
        )
        if when not in SUPPORTED_CACHE_WHEN:
            raise ValueError(f"cache when is not supported: {when}")
        entries.append(
            {
                "key": _cache_key(
                    raw_entry.get("key"),
                    variables,
                    cache_key_files=cache_key_files,
                    cache_key_files_commits=cache_key_files_commits,
                ),
                "untracked": untracked,
                "unprotect": _bool_value(raw_entry.get("unprotect", False), variables),
                "policy": policy,
                "paths": paths,
                "when": when,
                "fallback_keys": _expand_string_list(
                    raw_entry.get("fallback_keys"),
                    variables,
                ),
            }
        )
    return entries


def _artifact_config(value: Any, variables: dict[str, str] | None = None) -> dict:
    if not isinstance(value, dict):
        return {}
    variables = variables or {}
    paths = _expand_string_list(value.get("paths"), variables)
    reports = _artifact_reports(value.get("reports"), variables)
    if not paths and not value.get("untracked") and not reports:
        return {}
    name = _expand_ci_variables(str(value.get("name") or "artifacts"), variables)
    expire_in = _expand_ci_variables(str(value.get("expire_in") or ""), variables)
    config = {
        "name": name,
        "untracked": bool(value.get("untracked", False)),
        "paths": paths,
        "exclude": _expand_string_list(value.get("exclude"), variables),
        "when": str(value.get("when") or "on_success"),
        "expire_in": expire_in,
        "artifact_type": "archive",
        "artifact_format": "zip",
    }
    if reports:
        config["reports"] = reports
    return config


def _artifact_reports(value: Any, variables: dict[str, str]) -> list[dict]:
    if not isinstance(value, dict):
        return []
    reports: list[dict] = []
    for raw_report_type, raw_config in value.items():
        report_type = str(raw_report_type)
        paths: list[str] = []
        metadata: dict[str, str] = {}
        if isinstance(raw_config, dict):
            if raw_config.get("path") is not None:
                paths.extend(_expand_string_list(raw_config.get("path"), variables))
            if raw_config.get("paths") is not None:
                paths.extend(_expand_string_list(raw_config.get("paths"), variables))
            for key in ("coverage_format", "format"):
                if raw_config.get(key) is not None:
                    metadata[key] = _expand_ci_variables(str(raw_config[key]), variables)
        else:
            paths.extend(_expand_string_list(raw_config, variables))
        if not paths:
            continue
        report = {
            "artifact_type": report_type,
            "artifact_format": "gzip",
            "paths": paths,
        }
        report.update(metadata)
        reports.append(report)
    return reports


def _ref_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _ref_matches(pattern: str, ref: str, ref_kind: str, source: str) -> bool:
    if pattern in {"branches", "refs"}:
        return ref_kind == "branch"
    if pattern == "tags":
        return ref_kind == "tag"
    if pattern in {
        "api",
        "chat",
        "external",
        "external_pull_requests",
        "merge_requests",
        "parent_pipelines",
        "pipelines",
        "pushes",
        "schedules",
        "security_orchestration_policy",
        "triggers",
        "web",
        "webide",
    }:
        expected_sources = {
            "api": {"api"},
            "chat": {"chat"},
            "external": {"external"},
            "external_pull_requests": {"external_pull_request_event"},
            "merge_requests": {"merge_request_event"},
            "parent_pipelines": {"parent_pipeline"},
            "pipelines": {"pipeline"},
            "pushes": {"push"},
            "schedules": {"schedule"},
            "security_orchestration_policy": {"security_orchestration_policy"},
            "triggers": {"trigger"},
            "web": {"web"},
            "webide": {"webide"},
        }
        return source in expected_sources.get(pattern, set())
    if pattern == ref:
        return True
    regex = _regex_literal(pattern)
    if regex is not None:
        regex_pattern, regex_flags = regex
        return re.search(regex_pattern, ref, regex_flags) is not None
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatch(ref, pattern)
    return False


@dataclass
class _RuleDecision:
    included: bool
    when: str = "on_success"
    start_in: str | None = None
    allow_failure: bool | None = None
    allow_failure_exit_codes: list[int] | None = None
    variables: dict[str, dict] = field(default_factory=dict)
    needs: Any = None
    needs_set: bool = False


def _allow_failure_config(value: Any) -> tuple[bool, list[int]]:
    if isinstance(value, dict):
        unsupported = set(value) - {"exit_codes"}
        if unsupported:
            names = ", ".join(sorted(str(item) for item in unsupported))
            raise ValueError(f"allow_failure option(s) not supported: {names}")
        raw_codes = value.get("exit_codes")
        if raw_codes is None:
            raise ValueError("allow_failure exit_codes is required")
        if isinstance(raw_codes, list):
            codes = raw_codes
        else:
            codes = [raw_codes]
        parsed_codes: list[int] = []
        for code in codes:
            try:
                parsed = int(code)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"allow_failure exit code is not supported: {code}"
                ) from exc
            if parsed < 0 or parsed > 255:
                raise ValueError(
                    f"allow_failure exit code must be between 0 and 255: {parsed}"
                )
            parsed_codes.append(parsed)
        if not parsed_codes:
            raise ValueError("allow_failure exit_codes cannot be empty")
        return False, parsed_codes
    return bool(value), []


def _allow_failure_setting(value: Any) -> bool:
    return _allow_failure_config(value)[0]


def _allow_failure_exit_codes_setting(value: Any) -> list[int]:
    return _allow_failure_config(value)[1]


def _when_setting(value: Any) -> str:
    when = str(value or "on_success")
    if when not in SUPPORTED_JOB_WHEN:
        raise ValueError(f"when value is not supported: {when}")
    return when


def _start_in_seconds(value: Any) -> int:
    if value is None:
        raise ValueError("start_in is required when when delayed is used")
    return _duration_seconds(value, "start_in")


def _duration_seconds(value: Any, keyword: str) -> int:
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"{keyword} must be greater than zero")
        return value
    raw = str(value).strip().lower()
    normalized = re.sub(r"\band\b|,", " ", raw)
    matches = list(re.finditer(r"(\d+)\s*([a-z]+)", normalized))
    if not matches:
        raise ValueError(f"{keyword} value is not supported: {value}")
    total_seconds = 0
    position = 0
    for match in matches:
        if normalized[position : match.start()].strip():
            raise ValueError(f"{keyword} value is not supported: {value}")
        amount = int(match.group(1))
        unit = match.group(2)
        multiplier = DURATION_SECONDS_BY_UNIT.get(unit)
        if multiplier is None:
            raise ValueError(f"{keyword} unit is not supported: {unit}")
        if amount <= 0:
            raise ValueError(f"{keyword} must be greater than zero")
        total_seconds += amount * multiplier
        position = match.end()
    if normalized[position:].strip():
        raise ValueError(f"{keyword} value is not supported: {value}")
    return total_seconds


def _timeout_seconds(value: Any) -> int | None:
    if value is None:
        return None
    return _duration_seconds(value, "timeout")


def _trigger_config(value: Any, ref: str) -> dict | None:
    if value is None:
        return None
    if isinstance(value, str):
        project = value.strip()
        if not project:
            raise ValueError("trigger project is required")
        return {"project": project, "ref": ref, "strategy": None}
    if not isinstance(value, dict):
        raise ValueError("trigger value is not supported")
    unsupported = set(value) - {"project", "branch", "ref", "strategy"}
    if unsupported:
        names = ", ".join(sorted(str(item) for item in unsupported))
        raise ValueError(f"trigger option(s) not supported: {names}")
    project = str(value.get("project") or "").strip()
    if not project:
        raise ValueError("trigger project is required")
    trigger_ref = str(value.get("branch") or value.get("ref") or ref).strip()
    if not trigger_ref:
        raise ValueError("trigger ref is required")
    strategy = value.get("strategy")
    if strategy is not None and str(strategy) not in {"depend"}:
        raise ValueError(f"trigger strategy is not supported: {strategy}")
    return {"project": project, "ref": trigger_ref, "strategy": strategy}


def _metadata_variable(value: Any) -> dict:
    return {
        "value": str(value),
        "file": False,
        "masked": False,
        "raw": False,
        "public": True,
    }


def _parallel_expansions(value: Any) -> list[tuple[str | None, dict[str, dict]]]:
    if value is None:
        return [(None, {})]
    if isinstance(value, bool) or isinstance(value, list):
        raise ValueError("parallel value is not supported")
    if isinstance(value, dict):
        unsupported = set(value) - {"matrix"}
        if unsupported:
            names = ", ".join(sorted(str(item) for item in unsupported))
            raise ValueError(f"parallel option(s) not supported: {names}")
        raw_matrix = value.get("matrix")
        if not isinstance(raw_matrix, list) or not raw_matrix:
            raise ValueError("parallel matrix must be a non-empty list")
        expansions: list[tuple[str | None, dict[str, dict]]] = []
        for entry in raw_matrix:
            if not isinstance(entry, dict) or not entry:
                raise ValueError("parallel matrix entries must be mappings")
            keys = [str(key) for key in entry]
            values_by_key = [
                raw_values if isinstance(raw_values, list) else [raw_values]
                for raw_values in entry.values()
            ]
            for values in product(*values_by_key):
                matrix_values = {
                    key: _metadata_variable(value)
                    for key, value in zip(keys, values, strict=True)
                }
                expansions.append((_matrix_suffix(list(values)), matrix_values))
        if len(expansions) > 200:
            raise ValueError("parallel matrix cannot expand beyond 200 jobs")
        return expansions

    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"parallel value is not supported: {value}") from exc
    if count < 1 or count > 200:
        raise ValueError("parallel must be between 1 and 200")
    return [
        (
            f"{parallel_index}/{count}" if count > 1 else None,
            {
                "CI_NODE_INDEX": _metadata_variable(parallel_index),
                "CI_NODE_TOTAL": _metadata_variable(count),
            }
            if count > 1
            else {},
        )
        for parallel_index in range(1, count + 1)
    ]


def _parallel_job_names(parsed: dict) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    resolved_configs: dict[str, dict] = {}
    default = parsed.get("default") if isinstance(parsed.get("default"), dict) else {}
    for name, raw_config in parsed.items():
        if name in RESERVED_TOP_LEVEL_KEYS or str(name).startswith("."):
            continue
        if not isinstance(raw_config, dict):
            continue
        config = _apply_default_config(
            _resolve_job_config(str(name), parsed, resolved=resolved_configs),
            default,
        )
        if "script" not in config and config.get("trigger") is None:
            continue
        expanded_names = [
            f"{name} {suffix}" if suffix else str(name)
            for suffix, _variables in _parallel_expansions(config.get("parallel"))
        ]
        names[str(name)] = expanded_names or [str(name)]
    return names


def _exit_codes(value: Any, keyword: str) -> list[int]:
    if value is None:
        return []
    raw_codes = value if isinstance(value, list) else [value]
    parsed_codes: list[int] = []
    for code in raw_codes:
        try:
            parsed = int(code)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{keyword} exit code is not supported: {code}") from exc
        if parsed < 0 or parsed > 255:
            raise ValueError(f"{keyword} exit code must be between 0 and 255: {parsed}")
        parsed_codes.append(parsed)
    return parsed_codes


def _retry_config(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, int):
        max_attempts = value
        when: list[str] = []
        exit_codes: list[int] = []
    elif isinstance(value, dict):
        unsupported = set(value) - {"max", "when", "exit_codes"}
        if unsupported:
            names = ", ".join(sorted(str(item) for item in unsupported))
            raise ValueError(f"retry option(s) not supported: {names}")
        raw_max = value.get("max", 0)
        try:
            max_attempts = int(raw_max)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"retry max value is not supported: {raw_max}") from exc
        when = _string_list(value.get("when"))
        exit_codes = _exit_codes(value.get("exit_codes"), "retry")
    else:
        raise ValueError("retry must be an integer or mapping")
    if max_attempts < 0 or max_attempts > 2:
        raise ValueError("retry max must be between 0 and 2")
    return {"max": max_attempts, "when": when, "exit_codes": exit_codes}


def _delayed_start_in_seconds(config: dict, decision: _RuleDecision) -> int | None:
    start_in = decision.start_in if decision.start_in is not None else config.get("start_in")
    if decision.when == "delayed":
        return _start_in_seconds(start_in)
    if start_in is not None:
        raise ValueError("start_in is only supported with when delayed")
    return None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _expression_value(value: str, variables: dict[str, str]) -> str:
    value = value.strip()
    if value.startswith("$"):
        return variables.get(value[1:], "")
    return _unquote(value)


def _regex_literal(value: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"/(.+)/([a-zA-Z]*)", value)
    if not match:
        return None
    pattern, raw_flags = match.groups()
    flags = 0
    unsupported = set(raw_flags) - {"i", "m", "s", "x"}
    if unsupported:
        raise ValueError(f"regex flag(s) not supported: {''.join(sorted(unsupported))}")
    if "i" in raw_flags:
        flags |= re.IGNORECASE
    if "m" in raw_flags:
        flags |= re.MULTILINE
    if "s" in raw_flags:
        flags |= re.DOTALL
    if "x" in raw_flags:
        flags |= re.VERBOSE
    return pattern, flags


def _regex_pattern_value(value: str, variables: dict[str, str]) -> tuple[str, int]:
    value = value.strip()
    if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", value):
        value = variables.get(value[1:], "")
    regex = _regex_literal(value)
    if regex is not None:
        return regex
    return _unquote(value), 0


def _is_null_literal(value: str) -> bool:
    return value.strip() == "null"


def _is_empty_string_literal(value: str) -> bool:
    return value.strip() in {'""', "''"}


def _if_atom_matches(expression: str, variables: dict[str, str]) -> bool:
    expression = expression.strip()
    if not expression:
        return False
    if expression.startswith("!"):
        return not _if_expression_matches(expression[1:].strip(), variables)

    match = re.fullmatch(
        r"(\$[A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|=~|!~)\s*(.+)", expression
    )
    if match:
        left, operator, right = match.groups()
        left_key = left[1:]
        left_value = _expression_value(left, variables)
        if operator in {"=~", "!~"}:
            pattern, flags = _regex_pattern_value(right, variables)
            matches = bool(pattern) and re.search(pattern, left_value, flags) is not None
            return matches if operator == "=~" else not matches
        if _is_null_literal(right):
            matches = left_key not in variables
            return matches if operator == "==" else not matches
        if _is_empty_string_literal(right):
            matches = left_key in variables and left_value == ""
            return matches if operator == "==" else not matches
        right_value = _expression_value(right, variables)
        if operator == "==":
            return left_value == right_value
        return left_value != right_value

    if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", expression):
        return bool(variables.get(expression[1:], ""))

    return False


def _split_top_level_expression(expression: str, operator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    parts.append(expression[start:].strip())
    return parts


def _strip_wrapping_parentheses(expression: str) -> str:
    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        wraps_entire_expression = True
        for index, char in enumerate(expression):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wraps_entire_expression = False
                    break
        if not wraps_entire_expression:
            break
        expression = expression[1:-1].strip()
    return expression


def _if_expression_matches(expression: str, variables: dict[str, str]) -> bool:
    expression = _strip_wrapping_parentheses(expression)
    if not expression:
        return False

    or_terms = _split_top_level_expression(expression, "||")
    if len(or_terms) > 1:
        return any(_if_expression_matches(term, variables) for term in or_terms)

    and_terms = _split_top_level_expression(expression, "&&")
    if len(and_terms) > 1:
        return all(_if_expression_matches(term, variables) for term in and_terms)

    return _if_atom_matches(expression, variables)


def _if_matches(expression: Any, ref: str, variables: dict[str, str]) -> bool:
    if not isinstance(expression, str):
        return True
    context = {
        "CI_COMMIT_REF_NAME": ref,
        **variables,
    }
    return _if_expression_matches(expression, context)


def _path_matches(pattern: str, paths: set[str]) -> bool:
    normalized = pattern.strip().lstrip("/")
    if not normalized:
        return False
    if normalized.endswith("/"):
        return any(path.startswith(normalized) for path in paths)
    return any(
        path == normalized
        or path.startswith(f"{normalized}/")
        or fnmatch.fnmatch(path, normalized)
        for path in paths
    )


def _rule_path_patterns(
    value: Any,
    variables: dict[str, str],
    rule_name: str,
) -> list[str]:
    if isinstance(value, dict):
        supported_keys = (
            {"paths", "compare_to"}
            if rule_name == "changes"
            else {"paths", "project", "ref"}
        )
        unsupported_keys = set(value) - supported_keys
        if unsupported_keys:
            unsupported = ", ".join(sorted(unsupported_keys))
            raise ValueError(f"rules:{rule_name} option(s) not supported: {unsupported}")
        return _expand_string_list(value.get("paths"), variables)
    return _expand_string_list(value, variables)


def _rule_paths_match(
    value: Any,
    paths: set[str],
    variables: dict[str, str],
    rule_name: str,
    *,
    current_ref: str,
    existing_path_sets: dict[tuple[str, str], set[str]] | None = None,
    changed_path_sets: dict[str, set[str]] | None = None,
) -> bool:
    if rule_name == "exists" and isinstance(value, dict):
        project = value.get("project")
        ref = value.get("ref")
        if project or ref:
            path_sets = existing_path_sets or {}
            project_name = (
                _expand_ci_variables(str(project), variables) if project else ""
            )
            ref_name = _expand_ci_variables(str(ref), variables) if ref else current_ref
            paths = path_sets.get((project_name, ref_name), set())
    if rule_name == "changes" and isinstance(value, dict) and value.get("compare_to"):
        compare_ref = _expand_ci_variables(str(value["compare_to"]), variables)
        paths = (changed_path_sets or {}).get(compare_ref, paths)
    patterns = _rule_path_patterns(value, variables, rule_name)
    if not patterns:
        return False
    return any(_path_matches(pattern, paths) for pattern in patterns)


def _variable_filters_match(value: Any, ref: str, variables: dict[str, str]) -> bool:
    expressions = _string_list(value)
    if not expressions:
        return True
    return any(_if_matches(expression, ref, variables) for expression in expressions)


def _legacy_filter_matches(
    value: Any,
    ref: str,
    ref_kind: str,
    source: str,
    variables: dict[str, str],
    changed_paths: set[str],
) -> bool:
    if not isinstance(value, dict):
        refs = _ref_values(value)
        return bool(refs) and any(
            _ref_matches(pattern, ref, ref_kind, source) for pattern in refs
        )

    has_condition = False
    refs = _ref_values(value.get("refs"))
    if refs:
        has_condition = True
        if not any(_ref_matches(pattern, ref, ref_kind, source) for pattern in refs):
            return False

    if "variables" in value:
        has_condition = True
        if not _variable_filters_match(value.get("variables"), ref, variables):
            return False

    if "changes" in value:
        has_condition = True
        if not _rule_paths_match(
            value.get("changes"),
            changed_paths,
            variables,
            "changes",
            current_ref=ref,
        ):
            return False

    return has_condition


def _job_rule_decision(
    config: dict,
    ref: str,
    ref_kind: str,
    source: str,
    variables: dict[str, str],
    existing_paths: set[str],
    changed_paths: set[str],
    existing_path_sets: dict[tuple[str, str], set[str]] | None = None,
    changed_path_sets: dict[str, set[str]] | None = None,
) -> _RuleDecision:
    rules = config.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if "if" in rule and not _if_matches(rule.get("if"), ref, variables):
                continue
            if "exists" in rule and not _rule_paths_match(
                rule.get("exists"),
                existing_paths,
                variables,
                "exists",
                current_ref=ref,
                existing_path_sets=existing_path_sets,
            ):
                continue
            if "changes" in rule and not _rule_paths_match(
                rule.get("changes"),
                changed_paths,
                variables,
                "changes",
                current_ref=ref,
                changed_path_sets=changed_path_sets,
            ):
                continue
            when = _when_setting(rule.get("when"))
            rule_variables = _variable_entries(rule.get("variables"))
            allow_failure = rule.get("allow_failure")
            allow_failure_enabled = None
            allow_failure_exit_codes = None
            if allow_failure is not None:
                allow_failure_enabled, allow_failure_exit_codes = _allow_failure_config(
                    allow_failure
                )
            return _RuleDecision(
                included=when != "never",
                when=when,
                start_in=rule.get("start_in"),
                allow_failure=allow_failure_enabled,
                allow_failure_exit_codes=allow_failure_exit_codes,
                variables=rule_variables,
                needs=rule.get("needs"),
                needs_set="needs" in rule,
            )
        return _RuleDecision(included=False)

    only = config.get("only")
    if only is not None and not _legacy_filter_matches(
        only, ref, ref_kind, source, variables, changed_paths
    ):
        return _RuleDecision(included=False)

    except_filter = config.get("except")
    if except_filter is not None and _legacy_filter_matches(
        except_filter, ref, ref_kind, source, variables, changed_paths
    ):
        return _RuleDecision(included=False)

    when = _when_setting(config.get("when"))
    return _RuleDecision(
        included=when != "never",
        when=when,
        start_in=config.get("start_in"),
    )


def _allow_failure(config: dict, decision: _RuleDecision) -> bool:
    if decision.allow_failure is not None:
        return decision.allow_failure
    if "allow_failure" in config:
        return _allow_failure_setting(config["allow_failure"])
    return "rules" not in config and decision.when == "manual"


def _allow_failure_exit_codes(config: dict, decision: _RuleDecision) -> list[int]:
    if decision.allow_failure_exit_codes is not None:
        return decision.allow_failure_exit_codes
    if "allow_failure" in config:
        return _allow_failure_exit_codes_setting(config["allow_failure"])
    return []


def _workflow_rule_decision(
    parsed: dict,
    ref: str,
    ref_kind: str,
    source: str,
    variables: dict[str, str],
    existing_paths: set[str],
    changed_paths: set[str],
    existing_path_sets: dict[tuple[str, str], set[str]] | None = None,
    changed_path_sets: dict[str, set[str]] | None = None,
) -> _RuleDecision:
    workflow = parsed.get("workflow")
    if not isinstance(workflow, dict) or "rules" not in workflow:
        return _RuleDecision(included=True)
    return _job_rule_decision(
        {"rules": workflow.get("rules")},
        ref,
        ref_kind,
        source,
        variables,
        existing_paths,
        changed_paths,
        existing_path_sets,
        changed_path_sets,
    )


def _workflow_context(
    ref: str,
    ref_kind: str,
    pipeline_variables: dict[str, str],
    global_variables: dict[str, dict],
) -> dict[str, str]:
    return {
        "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
        "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
        "CI_COMMIT_REF_NAME": ref,
        **pipeline_variables,
        **_variable_values(global_variables),
    }


def parse_gitlab_ci_workflow_name(
    content: str,
    ref: str = "main",
    ref_kind: str = "branch",
    variables: dict[str, str] | None = None,
    existing_paths: set[str] | None = None,
    changed_paths: set[str] | None = None,
    existing_path_sets: dict[tuple[str, str], set[str]] | None = None,
    changed_path_sets: dict[str, set[str]] | None = None,
) -> str | None:
    """Return the expanded `workflow:name` value for a parsed CI config."""
    parsed = yaml.safe_load(content) or {}
    if not isinstance(parsed, dict):
        raise ValueError(".gitlab-ci.yml must contain a mapping")

    workflow = parsed.get("workflow")
    if not isinstance(workflow, dict):
        return None
    raw_name = workflow.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None

    global_variables = _variable_entries(parsed.get("variables"))
    pipeline_variables = variables or {}
    repository_paths = existing_paths or set()
    commit_changed_paths = changed_paths or set()
    context = _workflow_context(ref, ref_kind, pipeline_variables, global_variables)
    decision = _workflow_rule_decision(
        parsed,
        ref,
        ref_kind,
        pipeline_variables.get("CI_PIPELINE_SOURCE", "api"),
        context,
        repository_paths,
        commit_changed_paths,
        existing_path_sets,
        changed_path_sets,
    )
    if not decision.included:
        raise ValueError(".gitlab-ci.yml workflow rules skipped pipeline")
    if decision.variables:
        context = {
            **context,
            **_variable_values(decision.variables),
        }
    name = _expand_ci_variables(raw_name, context).strip()
    return name or None


def _deep_merge(parent: dict, child: dict) -> dict:
    merged = dict(parent)
    for key, value in child.items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
            and key in {"artifacts", "cache", "variables"}
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _extends_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("extends entries must be job names")
            names.append(item)
        return names
    raise ValueError("extends must be a string or list of strings")


def _inherit_setting(config: dict, key: str) -> Any:
    inherit = config.get("inherit")
    if not isinstance(inherit, dict):
        return None
    return inherit.get(key)


def _default_key_allowed(config: dict, key: str) -> bool:
    setting = _inherit_setting(config, "default")
    if setting is False:
        return False
    if isinstance(setting, list):
        return key in {str(item) for item in setting}
    return True


def _global_variables_for_job(
    config: dict, global_variables: dict[str, dict]
) -> dict[str, dict]:
    setting = _inherit_setting(config, "variables")
    if setting is False:
        return {}
    if isinstance(setting, list):
        allowed = {str(item) for item in setting}
        return {key: value for key, value in global_variables.items() if key in allowed}
    return global_variables


def _apply_default_config(config: dict, default: dict) -> dict:
    if not default:
        return config
    merged = dict(config)
    for key, value in default.items():
        if key not in DEFAULT_INHERITABLE_KEYS:
            continue
        if key not in merged and _default_key_allowed(config, key):
            merged[key] = value
    return merged


def _resolve_job_config(
    name: str,
    parsed: dict,
    resolving: set[str] | None = None,
    resolved: dict[str, dict] | None = None,
    depth: int = 0,
) -> dict:
    resolving = resolving or set()
    resolved = resolved or {}
    if name in resolved:
        return resolved[name]
    if name in resolving:
        raise ValueError(f"Job {name} has circular extends")
    if depth > MAX_EXTENDS_DEPTH:
        raise ValueError(f"Job {name} exceeds extends depth limit")

    raw_config = parsed.get(name)
    if not isinstance(raw_config, dict):
        return {}

    resolving.add(name)
    config: dict = {}
    for parent_name in _extends_names(raw_config.get("extends")):
        parent = parsed.get(parent_name)
        if not isinstance(parent, dict):
            raise ValueError(f"Job {name} extends unknown job {parent_name}")
        config = _deep_merge(
            config,
            _resolve_job_config(parent_name, parsed, resolving, resolved, depth + 1),
        )

    child = {key: value for key, value in raw_config.items() if key != "extends"}
    config = _deep_merge(config, child)
    resolving.remove(name)
    resolved[name] = config
    return config


def _unsupported_job_keys(name: str, config: dict) -> None:
    unsupported = [
        f"{key} ({UNSUPPORTED_JOB_KEYS[key]})"
        for key in sorted(UNSUPPORTED_JOB_KEYS)
        if key in config
    ]
    if unsupported:
        raise ValueError(
            f"Job {name} uses unsupported GitLab CI keyword(s): "
            + ", ".join(unsupported)
        )


def parse_gitlab_ci(
    content: str,
    ref: str = "main",
    ref_kind: str = "branch",
    variables: dict[str, str] | None = None,
    existing_paths: set[str] | None = None,
    changed_paths: set[str] | None = None,
    existing_path_sets: dict[tuple[str, str], set[str]] | None = None,
    changed_path_sets: dict[str, set[str]] | None = None,
    cache_key_files: dict[str, str] | None = None,
    cache_key_files_commits: dict[str, str] | None = None,
) -> list[ParsedCiJob]:
    """Parse a small, runner-executable subset of `.gitlab-ci.yml`.

    Supported keys:
    - global `stages`
    - global/default/job `image`
    - global/job `variables`
    - global/job `before_script`
    - job `script`
    - global/job `after_script`
    - job `stage`
    - job `needs`
    - job `dependencies`
    - global/default/job `services`
    - global/job `cache`
    - job `artifacts.paths`
    - common job `rules`, `only`, and `except` filters
    - common job `extends` inheritance from local template jobs
    """
    parsed = yaml.safe_load(content) or {}
    if not isinstance(parsed, dict):
        raise ValueError(".gitlab-ci.yml must contain a mapping")

    stages = _string_list(parsed.get("stages")) or ["test"]
    default = parsed.get("default") if isinstance(parsed.get("default"), dict) else {}
    global_image_config = parsed.get("image")
    global_image = _image_name(global_image_config, "alpine:3.20")
    global_variables = _variable_entries(parsed.get("variables"))
    global_before = _string_list(parsed.get("before_script"))
    global_after = _string_list(parsed.get("after_script"))
    global_services_config = parsed.get("services")
    global_cache_config = parsed.get("cache")
    global_tags: list[str] = []
    stage_order = {stage_name: index for index, stage_name in enumerate(stages)}
    pipeline_variables = variables or {}
    pipeline_source = pipeline_variables.get("CI_PIPELINE_SOURCE", "api")
    repository_paths = existing_paths or set()
    commit_changed_paths = changed_paths or set()
    workflow_context = _workflow_context(
        ref,
        ref_kind,
        pipeline_variables,
        global_variables,
    )
    workflow_decision = _workflow_rule_decision(
        parsed,
        ref,
        ref_kind,
        pipeline_source,
        workflow_context,
        repository_paths,
        commit_changed_paths,
        existing_path_sets,
        changed_path_sets,
    )
    if not workflow_decision.included:
        raise ValueError(".gitlab-ci.yml workflow rules skipped pipeline")
    if workflow_decision.variables:
        global_variables = {
            **global_variables,
            **workflow_decision.variables,
        }

    jobs: list[ParsedCiJob] = []
    parallel_job_names = _parallel_job_names(parsed)
    resolved_configs: dict[str, dict] = {}
    for name, raw_config in parsed.items():
        if name in RESERVED_TOP_LEVEL_KEYS or str(name).startswith("."):
            continue
        if not isinstance(raw_config, dict):
            continue
        config = _apply_default_config(
            _resolve_job_config(str(name), parsed, resolved=resolved_configs),
            default,
        )
        _unsupported_job_keys(str(name), config)
        trigger = _trigger_config(config.get("trigger"), ref)
        if "script" not in config and trigger is None:
            continue
        inherited_global_variables = _global_variables_for_job(config, global_variables)
        job_variable_entries = _variable_entries(config.get("variables"))
        merged_variable_entries = {
            **inherited_global_variables,
            **job_variable_entries,
        }
        rule_variables = {
            "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
            "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
            "CI_COMMIT_REF_NAME": ref,
            **pipeline_variables,
            **_variable_values(merged_variable_entries),
        }
        decision = _job_rule_decision(
            config,
            ref,
            ref_kind,
            pipeline_source,
            rule_variables,
            repository_paths,
            commit_changed_paths,
            existing_path_sets,
            changed_path_sets,
        )
        if not decision.included:
            continue
        if decision.variables:
            merged_variable_entries = {
                **merged_variable_entries,
                **decision.variables,
            }

        stage = str(config.get("stage") or (stages[0] if stages else "test"))
        variables = _variable_values(merged_variable_entries)
        image_variables = {
            "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
            "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
            "CI_COMMIT_REF_NAME": ref,
            **pipeline_variables,
            **variables,
        }
        raw_image = config.get("image") if "image" in config else global_image_config
        image = _image_name(raw_image, global_image)
        image_config = _image_config(raw_image, image_variables)
        cache_variables = {
            "CI_COMMIT_BRANCH": ref,
            "CI_COMMIT_REF_NAME": ref,
            **variables,
        }
        service_variables = {
            "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
            "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
            "CI_COMMIT_REF_NAME": ref,
            **pipeline_variables,
            **variables,
        }
        artifact_variables = {
            "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
            "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
            "CI_COMMIT_REF_NAME": ref,
            **pipeline_variables,
            **variables,
        }
        environment_variables = {
            "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
            "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
            "CI_COMMIT_REF_NAME": ref,
            **pipeline_variables,
            **variables,
        }
        environment_config = _environment_config(
            config.get("environment"),
            environment_variables,
        )
        before = _string_list(config.get("before_script", global_before))
        script = _string_list(config.get("script"))
        after = _string_list(config.get("after_script", global_after))
        artifact_config = _artifact_config(config.get("artifacts"), artifact_variables)
        artifact_paths = artifact_config.get("paths", [])
        raw_cache = config.get("cache") if "cache" in config else global_cache_config
        cache = (
            _cache_entries(
                raw_cache,
                cache_variables,
                cache_key_files=cache_key_files,
                cache_key_files_commits=cache_key_files_commits,
            )
            if raw_cache is not None
            else []
        )
        raw_services = (
            config.get("services") if "services" in config else global_services_config
        )
        services = (
            _service_entries(raw_services, service_variables)
            if raw_services is not None
            else []
        )
        parallel_expansions = _parallel_expansions(config.get("parallel"))
        raw_needs = decision.needs if decision.needs_set else config.get("needs")
        for parallel_suffix, parallel_variables in parallel_expansions:
            expanded_name = f"{name} {parallel_suffix}" if parallel_suffix else str(name)
            expanded_variables = variables
            expanded_variable_metadata = merged_variable_entries
            if parallel_variables:
                expanded_variable_metadata = {
                    **merged_variable_entries,
                    **parallel_variables,
                }
                expanded_variables = _variable_values(expanded_variable_metadata)

            jobs.append(
                ParsedCiJob(
                    name=expanded_name,
                    stage=stage,
                    stage_index=stage_order.get(stage, len(stage_order)),
                    image=image,
                    image_config=image_config,
                script=before + script + after,
                    variables=expanded_variables,
                    variable_metadata=expanded_variable_metadata,
                    needs=_needs(raw_needs, parallel_job_names),
                    dependencies=_dependencies(config.get("dependencies")),
                    tags=_string_list(config.get("tags", global_tags)),
                    services=services,
                    cache=cache,
                    artifacts_paths=artifact_paths,
                    artifacts=artifact_config,
                    when=decision.when,
                    start_in_seconds=_delayed_start_in_seconds(config, decision),
                    allow_failure=_allow_failure(config, decision),
                    allow_failure_exit_codes=_allow_failure_exit_codes(config, decision),
                    retry=_retry_config(config.get("retry")),
                    timeout_seconds=_timeout_seconds(config.get("timeout")),
                    interruptible=bool(config.get("interruptible", False)),
                    resource_group=str(config["resource_group"])
                    if config.get("resource_group") is not None
                    else None,
                    coverage=str(config["coverage"])
                    if config.get("coverage") is not None
                    else None,
                    environment=environment_config["name"],
                    environment_url=environment_config["url"],
                    environment_action=environment_config["action"],
                    secrets=_secret_entries(config.get("secrets")),
                    trigger=trigger,
                )
            )

    if not jobs:
        raise ValueError(".gitlab-ci.yml does not define any runnable jobs")

    jobs.sort(key=lambda job: (stage_order.get(job.stage, len(stage_order)), job.name))
    return jobs
