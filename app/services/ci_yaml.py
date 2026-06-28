"""Minimal `.gitlab-ci.yml` parser for the pipeline MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
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
    "tags",
}
MAX_EXTENDS_DEPTH = 11
UNSUPPORTED_JOB_KEYS = {
    "parallel": "parallel job expansion is not supported",
    "services": "service containers are not supported",
    "start_in": "delayed jobs are not supported",
    "trigger": "bridge/downstream pipeline trigger jobs are not supported",
}


@dataclass
class ParsedCiJob:
    name: str
    stage: str = "test"
    stage_index: int = 0
    image: str = "alpine:3.20"
    script: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    variable_metadata: dict[str, dict] = field(default_factory=dict)
    needs: list[dict] | None = None
    tags: list[str] = field(default_factory=list)
    cache: list[dict] = field(default_factory=list)
    artifacts_paths: list[str] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    when: str = "on_success"
    allow_failure: bool = False
    environment: str | None = None
    secrets: dict[str, dict] = field(default_factory=dict)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


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


def _environment_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("name"):
        return str(value["name"])
    return None


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


def _needs(value: Any) -> list[dict] | None:
    if value is None:
        return None
    if value == []:
        return []
    if isinstance(value, str):
        return [{"job": value, "optional": False, "artifacts": True}]
    if isinstance(value, dict):
        if value.get("project") or value.get("pipeline"):
            raise ValueError("Cross-project and pipeline needs are not supported")
        if value.get("parallel"):
            raise ValueError("needs parallel matrix is not supported")
        if value.get("job"):
            return [
                {
                    "job": str(value["job"]),
                    "optional": bool(value.get("optional", False)),
                    "artifacts": bool(value.get("artifacts", True)),
                }
            ]
        raise ValueError("needs entries must define a job")
    if isinstance(value, list):
        parsed: list[dict] = []
        for item in value:
            if isinstance(item, str):
                parsed.append({"job": item, "optional": False, "artifacts": True})
                continue
            if not isinstance(item, dict):
                raise ValueError("needs entries must be strings or mappings")
            if item.get("project") or item.get("pipeline"):
                raise ValueError("Cross-project and pipeline needs are not supported")
            if item.get("parallel"):
                raise ValueError("needs parallel matrix is not supported")
            if not item.get("job"):
                raise ValueError("needs entries must define a job")
            parsed.append(
                {
                    "job": str(item["job"]),
                    "optional": bool(item.get("optional", False)),
                    "artifacts": bool(item.get("artifacts", True)),
                }
            )
        return parsed
    raise ValueError("needs must be a string, mapping, or list")


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


def _cache_key(value: Any, variables: dict[str, str] | None = None) -> str:
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
            key = "-".join(files)
            return f"{prefix}-{key}" if prefix else key
        files_commits = _expand_string_list(value.get("files_commits"), variables)
        if files_commits:
            key = "-".join(files_commits)
            return f"{prefix}-{key}" if prefix else key
        if prefix:
            return prefix
    return "default"


def _cache_entries(value: Any, variables: dict[str, str] | None = None) -> list[dict]:
    if value is None or value is False:
        return []
    variables = variables or {}
    raw_entries = value if isinstance(value, list) else [value]
    entries: list[dict] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        paths = _expand_string_list(raw_entry.get("paths"), variables)
        if not paths and not raw_entry.get("untracked"):
            continue
        entries.append(
            {
                "key": _cache_key(raw_entry.get("key"), variables),
                "untracked": bool(raw_entry.get("untracked", False)),
                "policy": _expand_ci_variables(
                    str(raw_entry.get("policy") or "pull-push"),
                    variables,
                ),
                "paths": paths,
                "when": _expand_ci_variables(
                    str(raw_entry.get("when") or "on_success"),
                    variables,
                ),
                "fallback_keys": _expand_string_list(
                    raw_entry.get("fallback_keys"),
                    variables,
                ),
            }
        )
    return entries


def _artifact_config(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    paths = _string_list(value.get("paths"))
    if not paths and not value.get("untracked"):
        return {}
    return {
        "name": str(value.get("name") or "artifacts"),
        "untracked": bool(value.get("untracked", False)),
        "paths": paths,
        "exclude": _string_list(value.get("exclude")),
        "when": str(value.get("when") or "on_success"),
        "expire_in": str(value.get("expire_in") or ""),
        "artifact_type": "archive",
        "artifact_format": "zip",
    }


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
        "external",
        "merge_requests",
        "pipelines",
        "pushes",
        "schedules",
        "triggers",
        "web",
    }:
        expected_sources = {
            "api": {"api"},
            "external": {"external"},
            "merge_requests": {"merge_request_event"},
            "pipelines": {"pipeline"},
            "pushes": {"push"},
            "schedules": {"schedule"},
            "triggers": {"trigger"},
            "web": {"web"},
        }
        return source in expected_sources.get(pattern, set())
    if pattern == ref:
        return True
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
        import re

        return re.search(pattern[1:-1], ref) is not None
    return False


@dataclass
class _RuleDecision:
    included: bool
    when: str = "on_success"
    allow_failure: bool | None = None
    variables: dict[str, dict] = field(default_factory=dict)


def _allow_failure_setting(value: Any) -> bool:
    if isinstance(value, dict):
        raise ValueError("allow_failure exit_codes is not supported")
    return bool(value)


def _when_setting(value: Any) -> str:
    when = str(value or "on_success")
    if when == "delayed":
        raise ValueError("when delayed is not supported")
    return when


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
            pattern = right.strip()
            if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 1:
                pattern = pattern[1:-1]
            else:
                pattern = _unquote(pattern)
            matches = re.search(pattern, left_value) is not None
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
    return any(
        path == normalized or fnmatch.fnmatch(path, normalized) for path in paths
    )


def _rule_path_patterns(value: Any, variables: dict[str, str]) -> list[str]:
    if isinstance(value, dict):
        return _expand_string_list(value.get("paths"), variables)
    return _expand_string_list(value, variables)


def _rule_paths_match(value: Any, paths: set[str], variables: dict[str, str]) -> bool:
    patterns = _rule_path_patterns(value, variables)
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
        if not _rule_paths_match(value.get("changes"), changed_paths, variables):
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
) -> _RuleDecision:
    rules = config.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if "if" in rule and not _if_matches(rule.get("if"), ref, variables):
                continue
            if "exists" in rule and not _rule_paths_match(
                rule.get("exists"), existing_paths, variables
            ):
                continue
            if "changes" in rule and not _rule_paths_match(
                rule.get("changes"), changed_paths, variables
            ):
                continue
            when = _when_setting(rule.get("when"))
            rule_variables = _variable_entries(rule.get("variables"))
            allow_failure = rule.get("allow_failure")
            return _RuleDecision(
                included=when != "never",
                when=when,
                allow_failure=_allow_failure_setting(allow_failure)
                if allow_failure is not None
                else None,
                variables=rule_variables,
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
    return _RuleDecision(included=when != "never", when=when)


def _allow_failure(config: dict, decision: _RuleDecision) -> bool:
    if decision.allow_failure is not None:
        return decision.allow_failure
    if "allow_failure" in config:
        return _allow_failure_setting(config["allow_failure"])
    return "rules" not in config and decision.when == "manual"


def _workflow_rule_decision(
    parsed: dict,
    ref: str,
    ref_kind: str,
    source: str,
    variables: dict[str, str],
    existing_paths: set[str],
    changed_paths: set[str],
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
    )


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
    global_image = _image_name(parsed.get("image"), "alpine:3.20")
    global_variables = _variable_entries(parsed.get("variables"))
    global_before = _string_list(parsed.get("before_script"))
    global_after = _string_list(parsed.get("after_script"))
    global_cache_config = parsed.get("cache")
    global_tags: list[str] = []
    stage_order = {stage_name: index for index, stage_name in enumerate(stages)}
    pipeline_variables = variables or {}
    pipeline_source = pipeline_variables.get("CI_PIPELINE_SOURCE", "api")
    repository_paths = existing_paths or set()
    commit_changed_paths = changed_paths or set()
    workflow_context = {
        "CI_COMMIT_BRANCH": ref if ref_kind == "branch" else "",
        "CI_COMMIT_TAG": ref if ref_kind == "tag" else "",
        "CI_COMMIT_REF_NAME": ref,
        **pipeline_variables,
        **_variable_values(global_variables),
    }
    workflow_decision = _workflow_rule_decision(
        parsed,
        ref,
        ref_kind,
        pipeline_source,
        workflow_context,
        repository_paths,
        commit_changed_paths,
    )
    if not workflow_decision.included:
        raise ValueError(".gitlab-ci.yml workflow rules skipped pipeline")
    if workflow_decision.variables:
        global_variables = {
            **global_variables,
            **workflow_decision.variables,
        }

    jobs: list[ParsedCiJob] = []
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
        if "script" not in config:
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
        )
        if not decision.included:
            continue
        if decision.variables:
            merged_variable_entries = {
                **merged_variable_entries,
                **decision.variables,
            }

        stage = str(config.get("stage") or (stages[0] if stages else "test"))
        image = _image_name(config.get("image"), global_image)
        variables = _variable_values(merged_variable_entries)
        cache_variables = {
            "CI_COMMIT_BRANCH": ref,
            "CI_COMMIT_REF_NAME": ref,
            **variables,
        }
        before = _string_list(config.get("before_script", global_before))
        script = _string_list(config.get("script"))
        after = _string_list(config.get("after_script", global_after))
        artifact_config = _artifact_config(config.get("artifacts"))
        artifact_paths = artifact_config.get("paths", [])
        raw_cache = config.get("cache") if "cache" in config else global_cache_config
        cache = (
            _cache_entries(raw_cache, cache_variables) if raw_cache is not None else []
        )

        jobs.append(
            ParsedCiJob(
                name=str(name),
                stage=stage,
                stage_index=stage_order.get(stage, len(stage_order)),
                image=image,
                script=before + script + after,
                variables=variables,
                variable_metadata=merged_variable_entries,
                needs=_needs(config.get("needs")),
                tags=_string_list(config.get("tags", global_tags)),
                cache=cache,
                artifacts_paths=artifact_paths,
                artifacts=artifact_config,
                when=decision.when,
                allow_failure=_allow_failure(config, decision),
                environment=_environment_name(config.get("environment")),
                secrets=_secret_entries(config.get("secrets")),
            )
        )

    if not jobs:
        raise ValueError(".gitlab-ci.yml does not define any runnable jobs")

    jobs.sort(key=lambda job: (stage_order.get(job.stage, len(stage_order)), job.name))
    return jobs
