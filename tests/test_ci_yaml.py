"""`.gitlab-ci.yml` parser tests."""

from app.services.ci_yaml import parse_gitlab_ci


def test_parse_gitlab_ci_orders_jobs_by_stage_and_merges_scripts():
    jobs = parse_gitlab_ci(
        """
stages:
  - build
  - test
image: alpine:3.20
variables:
  GLOBAL: one
before_script:
  - echo before
after_script:
  - echo after
cache:
  key: global-cache
  paths:
    - vendor/

unit:
  stage: test
  script:
    - echo test

compile:
  stage: build
  image: python:3.12-alpine
  variables:
    LOCAL: two
  needs: []
  tags:
    - docker
    - linux
  cache:
    key: build-cache
    paths:
      - .cache/pip
    policy: pull-push
    when: always
    fallback_keys:
      - global-cache
  script:
    - echo build
  artifacts:
    paths:
      - out/report.txt
"""
    )

    assert [job.name for job in jobs] == ["compile", "unit"]
    assert jobs[0].stage == "build"
    assert jobs[0].stage_index == 0
    assert jobs[0].image == "python:3.12-alpine"
    assert jobs[0].script == ["echo before", "echo build", "echo after"]
    assert jobs[0].variables == {"GLOBAL": "one", "LOCAL": "two"}
    assert jobs[0].needs == []
    assert jobs[0].tags == ["docker", "linux"]
    assert jobs[0].cache == [
        {
            "key": "build-cache",
            "untracked": False,
            "policy": "pull-push",
            "paths": [".cache/pip"],
            "when": "always",
            "fallback_keys": ["global-cache"],
        }
    ]
    assert jobs[0].artifacts_paths == ["out/report.txt"]
    assert jobs[0].artifacts == {
        "name": "artifacts",
        "untracked": False,
        "paths": ["out/report.txt"],
        "exclude": [],
        "when": "on_success",
        "expire_in": "",
        "artifact_type": "archive",
        "artifact_format": "zip",
    }
    assert jobs[1].stage == "test"
    assert jobs[1].stage_index == 1
    assert jobs[1].image == "alpine:3.20"
    assert jobs[1].needs is None
    assert jobs[1].tags == []
    assert jobs[1].cache == [
        {
            "key": "global-cache",
            "untracked": False,
            "policy": "pull-push",
            "paths": ["vendor/"],
            "when": "on_success",
            "fallback_keys": [],
        }
    ]
    assert jobs[1].artifacts_paths == []


def test_parse_gitlab_ci_job_variables_override_global_variables():
    jobs = parse_gitlab_ci(
        """
variables:
  SHARED: global
  GLOBAL_ONLY: global-only

test:
  variables:
    SHARED: job
    JOB_ONLY: job-only
  script:
    - echo variables
"""
    )

    assert jobs[0].variables == {
        "SHARED": "job",
        "GLOBAL_ONLY": "global-only",
        "JOB_ONLY": "job-only",
    }


def test_parse_gitlab_ci_preserves_variable_metadata():
    jobs = parse_gitlab_ci(
        """
variables:
  SIMPLE: simple
  RAW_VALUE:
    value: "$SIMPLE-literal"
    expand: false
  FILE_SECRET:
    value: secret-content
    variable_type: file
  MASKED_SECRET:
    value: hidden
    masked: true

job:
  variables:
    FILE_SECRET:
      value: job-secret
      file: true
  script: echo variables
"""
    )

    job = jobs[0]
    assert job.variables == {
        "SIMPLE": "simple",
        "RAW_VALUE": "$SIMPLE-literal",
        "FILE_SECRET": "job-secret",
        "MASKED_SECRET": "hidden",
    }
    assert job.variable_metadata["RAW_VALUE"]["raw"] is True
    assert job.variable_metadata["FILE_SECRET"]["file"] is True
    assert job.variable_metadata["MASKED_SECRET"]["masked"] is True
    assert job.variable_metadata["MASKED_SECRET"]["public"] is False


def test_parse_gitlab_ci_preserves_job_secret_requests():
    jobs = parse_gitlab_ci(
        """
secret_probe:
  secrets:
    DB_PASSWORD:
      gitlab_secrets_manager:
        name: DATABASE_PASSWORD
    API_TOKEN:
      gitlab_secrets_manager: GROUP_TOKEN
      file: false
  script:
    - echo secrets
"""
    )

    assert jobs[0].secrets == {
        "DB_PASSWORD": {"name": "DATABASE_PASSWORD", "file": True},
        "API_TOKEN": {"name": "GROUP_TOKEN", "file": False},
    }


def test_parse_gitlab_ci_rejects_empty_pipeline():
    try:
        parse_gitlab_ci("stages: [test]\n")
    except ValueError as exc:
        assert "does not define any runnable jobs" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_parses_needs_strings_and_job_objects():
    jobs = parse_gitlab_ci(
        """
stages: [build, test]

compile:
  stage: build
  script: echo build

unit:
  stage: test
  needs:
    - compile
    - job: lint
  script: echo test
"""
    )

    unit = next(job for job in jobs if job.name == "unit")
    assert unit.needs == [
        {"job": "compile", "optional": False, "artifacts": True},
        {"job": "lint", "optional": False, "artifacts": True},
    ]


def test_parse_gitlab_ci_preserves_optional_needs():
    jobs = parse_gitlab_ci(
        """
stages: [build, test]

unit:
  stage: test
  needs:
    - job: compile
      optional: true
      artifacts: false
  script: echo test
"""
    )

    assert jobs[0].needs == [{"job": "compile", "optional": True, "artifacts": False}]


def test_parse_gitlab_ci_parses_single_mapping_needs():
    jobs = parse_gitlab_ci(
        """
compile:
  script: echo build

unit:
  needs:
    job: compile
    artifacts: false
  script: echo test
"""
    )

    unit = next(job for job in jobs if job.name == "unit")
    assert unit.needs == [{"job": "compile", "optional": False, "artifacts": False}]


def test_parse_gitlab_ci_rejects_unsupported_needs_forms():
    for content in [
        """
unit:
  needs:
    - project: group/project
      job: build
  script: echo test
""",
        """
unit:
  needs:
    - pipeline: other
      job: build
  script: echo test
""",
        """
unit:
  needs:
    - artifacts: true
  script: echo test
""",
    ]:
        try:
            parse_gitlab_ci(content)
        except ValueError as exc:
            assert "needs" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_rejects_needs_parallel_matrix():
    content = """
compile:
  script: echo compile

unit:
  needs:
    - job: compile
      parallel:
        matrix:
          - OS: linux
  script: echo unit
"""
    try:
        parse_gitlab_ci(content)
    except ValueError as exc:
        assert "needs parallel matrix is not supported" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_rejects_unsupported_execution_keywords():
    for keyword, content in {
        "trigger": """
deploy_downstream:
  trigger:
    project: group/downstream
""",
        "parallel": """
matrix_job:
  parallel: 3
  script: echo matrix
""",
        "services": """
db_job:
  services:
    - postgres:16
  script: echo db
""",
    }.items():
        try:
            parse_gitlab_ci(content)
        except ValueError as exc:
            message = str(exc)
            assert "unsupported GitLab CI keyword" in message
            assert keyword in message
        else:
            raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_applies_common_ref_filters():
    jobs = parse_gitlab_ci(
        """
main_only:
  script: echo main
  only: [main]

skip_main:
  script: echo skip
  except: [main]

release_rule:
  script: echo release
  rules:
    - if: '$CI_COMMIT_BRANCH == "release"'
    - when: never

fallback_rule:
  script: echo fallback
  rules:
    - if: '$CI_COMMIT_REF_NAME == "dev"'
      when: never
    - when: on_success
""",
        ref="main",
    )

    assert [job.name for job in jobs] == ["fallback_rule", "main_only"]


def test_parse_gitlab_ci_applies_legacy_ref_glob_filters():
    jobs = parse_gitlab_ci(
        """
release_glob:
  script: echo release glob
  only: ["release/*"]

feature_glob:
  script: echo feature glob
  only: ["feature/*"]

skip_release_glob:
  script: echo skip release glob
  except: ["release/*"]

skip_feature_glob:
  script: echo skip feature glob
  except: ["feature/*"]
""",
        ref="release/1.0",
    )

    assert [job.name for job in jobs] == ["release_glob", "skip_feature_glob"]


def test_parse_gitlab_ci_applies_tag_ref_filters_and_variables():
    jobs = parse_gitlab_ci(
        """
tag_only:
  script: echo tag
  only: [tags]

branch_only:
  script: echo branch
  only: [branches]

skip_tags:
  script: echo skip tags
  except: [tags]

tag_rule:
  script: echo tag rule
  rules:
    - if: '$CI_COMMIT_TAG == "v1.2.3" && $CI_COMMIT_BRANCH == ""'
""",
        ref="v1.2.3",
        ref_kind="tag",
    )

    assert [job.name for job in jobs] == ["tag_only", "tag_rule"]


def test_parse_gitlab_ci_applies_legacy_source_ref_filters():
    jobs = parse_gitlab_ci(
        """
api_only:
  script: echo api
  only: [api]

trigger_only:
  script: echo trigger
  only: [triggers]

not_schedules:
  script: echo not schedule
  except: [schedules]
""",
        variables={"CI_PIPELINE_SOURCE": "trigger"},
    )

    assert [job.name for job in jobs] == ["not_schedules", "trigger_only"]


def test_parse_gitlab_ci_applies_mapping_only_except_filters():
    jobs = parse_gitlab_ci(
        """
only_mapping:
  script: echo only mapping
  only:
    refs:
      - main
    variables:
      - '$RUN_DEPLOY'
    changes:
      - src/**

except_mapping:
  script: echo except mapping
  except:
    refs:
      - main
    variables:
      - '$SKIP_DEPLOY'

only_miss:
  script: echo only miss
  only:
    refs:
      - release

except_miss:
  script: echo except miss
  except:
    refs:
      - release
    variables:
      - '$SKIP_DEPLOY'
""",
        ref="main",
        variables={"RUN_DEPLOY": "1", "SKIP_DEPLOY": "1"},
        changed_paths={"src/app.py"},
    )

    assert [job.name for job in jobs] == ["except_miss", "only_mapping"]


def test_parse_gitlab_ci_applies_richer_rules_if_expressions():
    jobs = parse_gitlab_ci(
        """
truthy:
  script: echo truthy
  rules:
    - if: '$RUN_TRUTHY'

equals:
  script: echo equals
  rules:
    - if: '$TARGET == "prod"'

not_equals:
  script: echo not equals
  rules:
    - if: '$TARGET != "dev"'

null_match:
  script: echo null match
  rules:
    - if: '$OPTIONAL == null'

null_not_match:
  script: echo null not match
  rules:
    - if: '$TARGET != null'

empty_match:
  script: echo empty match
  rules:
    - if: '$EMPTY_VALUE == ""'

empty_skip:
  script: echo empty skip
  rules:
    - if: '$OPTIONAL == ""'

regex:
  script: echo regex
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ /^release-/'

regex_from_variable:
  script: echo regex from variable
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ $RELEASE_PATTERN'

regex_not_match:
  script: echo regex not match
  rules:
    - if: '$CI_COMMIT_REF_NAME !~ /^main$/'

regex_variable_not_match:
  script: echo regex variable not match
  rules:
    - if: '$CI_COMMIT_REF_NAME !~ $MAIN_PATTERN'

regex_not_match_skip:
  script: echo regex not match skip
  rules:
    - if: '$CI_COMMIT_REF_NAME !~ /^release-/'

negated_missing:
  script: echo negated missing
  rules:
    - if: '!$OPTIONAL'

negated_grouped:
  script: echo negated grouped
  rules:
    - if: '!($NEVER || $OPTIONAL)'

negated_skip:
  script: echo negated skip
  rules:
    - if: '!$RUN_TRUTHY'

and_or:
  script: echo and or
  rules:
    - if: '$TARGET == "prod" && $RUN_TRUTHY || $NEVER'

grouped:
  script: echo grouped
  rules:
    - if: '($TARGET == "prod" || $TARGET == "stage") && $RUN_TRUTHY'

nested_grouped:
  script: echo nested grouped
  rules:
    - if: '$RUN_TRUTHY && ($TARGET == "prod" || ($TARGET == "stage" && $NEVER))'

grouped_skip:
  script: echo grouped skip
  rules:
    - if: '($TARGET == "dev" || $NEVER) && $RUN_TRUTHY'

skipped:
  script: echo skipped
  rules:
    - if: '$TARGET == "prod"'
      when: never
""",
        ref="release-1.0",
        variables={
            "EMPTY_VALUE": "",
            "MAIN_PATTERN": "/^main$/",
            "RELEASE_PATTERN": "/^release-/",
            "RUN_TRUTHY": "1",
            "TARGET": "prod",
        },
    )

    assert [job.name for job in jobs] == [
        "and_or",
        "empty_match",
        "equals",
        "grouped",
        "negated_grouped",
        "negated_missing",
        "nested_grouped",
        "not_equals",
        "null_match",
        "null_not_match",
        "regex",
        "regex_from_variable",
        "regex_not_match",
        "regex_variable_not_match",
        "truthy",
    ]


def test_parse_gitlab_ci_supports_regex_flags_in_rules_if_and_legacy_refs():
    jobs = parse_gitlab_ci(
        """
case_insensitive_rule:
  script: echo rule
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ /^release-/i'

variable_regex_flags:
  variables:
    RELEASE_PATTERN: "/^release-/i"
  script: echo variable regex
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ $RELEASE_PATTERN'

legacy_regex_ref:
  script: echo legacy
  only:
    - /^release-/i

case_sensitive_miss:
  script: echo miss
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ /^release-/'
""",
        ref="Release-2026",
    )

    assert [job.name for job in jobs] == [
        "case_insensitive_rule",
        "legacy_regex_ref",
        "variable_regex_flags",
    ]


def test_parse_gitlab_ci_rejects_unsupported_regex_flags():
    try:
        parse_gitlab_ci(
            """
bad_regex_flag:
  script: echo bad
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ /^release-/q'
"""
        )
    except ValueError as exc:
        assert "regex flag(s) not supported" in str(exc)
        assert "q" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_applies_workflow_rules():
    content = """
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
    - when: never

job:
  script: echo workflow
"""
    jobs = parse_gitlab_ci(content, variables={"CI_PIPELINE_SOURCE": "schedule"})
    assert [job.name for job in jobs] == ["job"]

    try:
        parse_gitlab_ci(content, variables={"CI_PIPELINE_SOURCE": "api"})
    except ValueError as exc:
        assert "workflow rules skipped pipeline" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_applies_workflow_rule_variables():
    jobs = parse_gitlab_ci(
        """
variables:
  SHARED: global
  GLOBAL_ONLY: global

workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
      variables:
        SHARED: workflow
        WORKFLOW_ONLY: workflow
    - when: never

default_job:
  script: echo default

override_job:
  variables:
    SHARED: job
  script: echo override
""",
        variables={"CI_PIPELINE_SOURCE": "schedule"},
    )

    default_job = next(job for job in jobs if job.name == "default_job")
    override_job = next(job for job in jobs if job.name == "override_job")
    assert default_job.variables == {
        "SHARED": "workflow",
        "GLOBAL_ONLY": "global",
        "WORKFLOW_ONLY": "workflow",
    }
    assert override_job.variables["SHARED"] == "job"
    assert override_job.variables["WORKFLOW_ONLY"] == "workflow"


def test_parse_gitlab_ci_applies_rules_exists_and_changes():
    jobs = parse_gitlab_ci(
        """
exists_match:
  script: echo exists
  rules:
    - exists:
        - src/*.py

changes_match:
  script: echo changes
  rules:
    - changes:
        - docs/**

exists_miss:
  script: echo miss
  rules:
    - exists:
        - missing.txt
""",
        existing_paths={"src/app.py", "docs/readme.md"},
        changed_paths={"docs/readme.md"},
    )

    assert [job.name for job in jobs] == ["changes_match", "exists_match"]


def test_parse_gitlab_ci_applies_rules_exists_and_changes_path_objects():
    jobs = parse_gitlab_ci(
        """
exists_object:
  script: echo exists
  rules:
    - exists:
        paths:
          - src/*.py

changes_object:
  script: echo changes
  rules:
    - changes:
        paths:
          - docs/**

object_miss:
  script: echo miss
  rules:
    - exists:
        paths:
          - missing.txt
""",
        existing_paths={"src/app.py", "docs/readme.md"},
        changed_paths={"docs/readme.md"},
    )

    assert [job.name for job in jobs] == ["changes_object", "exists_object"]


def test_parse_gitlab_ci_rules_exists_matches_directory_patterns():
    jobs = parse_gitlab_ci(
        """
exists_directory:
  script: echo exists directory
  rules:
    - exists:
        - config/

exists_directory_miss:
  script: echo exists directory miss
  rules:
    - exists:
        - docs/
""",
        existing_paths={"config/app.yml", "src/main.py"},
    )

    assert [job.name for job in jobs] == ["exists_directory"]


def test_parse_gitlab_ci_expands_variables_in_rules_path_patterns():
    jobs = parse_gitlab_ci(
        """
exists_variable:
  script: echo exists variable
  rules:
    - exists:
        - "$SRC_GLOB"

changes_variable:
  script: echo changes variable
  rules:
    - changes:
        paths:
          - "$DOCS_GLOB"

variable_miss:
  script: echo miss
  rules:
    - changes:
        - "$MISSING_GLOB"
""",
        variables={
            "SRC_GLOB": "src/*.py",
            "DOCS_GLOB": "docs/**",
            "MISSING_GLOB": "missing/**",
        },
        existing_paths={"src/app.py", "docs/readme.md"},
        changed_paths={"docs/readme.md"},
    )

    assert [job.name for job in jobs] == ["changes_variable", "exists_variable"]


def test_parse_gitlab_ci_rejects_unsupported_rules_path_options():
    changes_content = """
changes_compare_to:
  script: echo changes
  rules:
    - changes:
        compare_to: refs/heads/main
        paths:
          - docs/**
"""
    try:
        parse_gitlab_ci(changes_content, changed_paths={"docs/readme.md"})
        assert False, "Expected unsupported rules:changes options to fail"
    except ValueError as exc:
        message = str(exc)
        assert "rules:changes option(s) not supported" in message
        assert "compare_to" in message

    exists_content = """
exists_project:
  script: echo exists
  rules:
    - exists:
        project: group/templates
        ref: main
        paths:
          - template.yml
"""
    try:
        parse_gitlab_ci(exists_content, existing_paths={"template.yml"})
        assert False, "Expected unsupported rules:exists options to fail"
    except ValueError as exc:
        message = str(exc)
        assert "rules:exists option(s) not supported" in message
        assert "project" in message
        assert "ref" in message


def test_parse_gitlab_ci_marks_manual_jobs():
    jobs = parse_gitlab_ci(
        """
manual_job:
  script: echo manual
  rules:
    - when: manual
"""
    )

    assert jobs[0].name == "manual_job"
    assert jobs[0].when == "manual"
    assert jobs[0].allow_failure is False


def test_parse_gitlab_ci_marks_job_level_manual_as_allowed_failure_by_default():
    jobs = parse_gitlab_ci(
        """
manual_job:
  script: echo manual
  when: manual
"""
    )

    assert jobs[0].name == "manual_job"
    assert jobs[0].when == "manual"
    assert jobs[0].allow_failure is True


def test_parse_gitlab_ci_supports_delayed_jobs():
    jobs = parse_gitlab_ci(
        """
delayed_job:
  script: echo delayed
  when: delayed
  start_in: 10 minutes
rule_delayed:
  script: echo rule delayed
  rules:
    - when: delayed
      start_in: 2 hours
"""
    )

    by_name = {job.name: job for job in jobs}
    assert by_name["delayed_job"].when == "delayed"
    assert by_name["delayed_job"].start_in_seconds == 600
    assert by_name["rule_delayed"].when == "delayed"
    assert by_name["rule_delayed"].start_in_seconds == 7200


def test_parse_gitlab_ci_rejects_invalid_delayed_jobs():
    for content in [
        """
delayed_job:
  script: echo delayed
  when: delayed
""",
        """
delayed_job:
  script: echo delayed
  rules:
    - when: delayed
""",
        """
delayed_job:
  script: echo delayed
  start_in: 10 minutes
""",
        """
delayed_job:
  script: echo delayed
  when: delayed
  start_in: soon
""",
    ]:
        try:
            parse_gitlab_ci(content)
        except ValueError as exc:
            message = str(exc)
            assert "delayed" in message or "start_in" in message
        else:
            raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_rejects_unknown_when_values():
    for content, detail in [
        (
            """
invalid_when:
  script: echo invalid
  when: sometimes
""",
            "sometimes",
        ),
        (
            """
invalid_rule_when:
  script: echo invalid
  rules:
    - when: eventually
""",
            "eventually",
        ),
    ]:
        try:
            parse_gitlab_ci(content)
        except ValueError as exc:
            message = str(exc)
            assert "when value is not supported" in message
            assert detail in message
        else:
            raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_applies_rule_variables_and_allow_failure():
    jobs = parse_gitlab_ci(
        """
optional_probe:
  script: echo "$RULE_TARGET"
  rules:
    - if: '$CI_COMMIT_REF_NAME == "main"'
      allow_failure: true
      variables:
        RULE_TARGET: from-rule
"""
    )

    assert jobs[0].name == "optional_probe"
    assert jobs[0].allow_failure is True
    assert jobs[0].variables["RULE_TARGET"] == "from-rule"
    assert jobs[0].variable_metadata["RULE_TARGET"]["value"] == "from-rule"


def test_parse_gitlab_ci_rejects_allow_failure_exit_codes():
    for content in [
        """
optional_probe:
  script: echo optional
  allow_failure:
    exit_codes:
      - 137
""",
        """
optional_probe:
  script: echo optional
  rules:
    - if: '$CI_COMMIT_REF_NAME == "main"'
      allow_failure:
        exit_codes:
          - 137
""",
    ]:
        try:
            parse_gitlab_ci(content)
        except ValueError as exc:
            assert "allow_failure exit_codes is not supported" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_allows_job_to_disable_global_cache():
    jobs = parse_gitlab_ci(
        """
cache:
  key: global-cache
  paths:
    - vendor/

uncached:
  script: echo uncached
  cache: []
"""
    )

    assert jobs[0].name == "uncached"
    assert jobs[0].cache == []


def test_parse_gitlab_ci_supports_cache_key_prefix_and_files():
    jobs = parse_gitlab_ci(
        """
cache_probe:
  cache:
    key:
      prefix: deps
      files:
        - pyproject.toml
        - uv.lock
    paths:
      - .cache/uv
  script:
    - echo cache
"""
    )

    assert jobs[0].cache[0]["key"] == "deps-pyproject.toml-uv.lock"


def test_parse_gitlab_ci_supports_cache_key_file_list():
    jobs = parse_gitlab_ci(
        """
cache_probe:
  variables:
    LOCKFILE: uv.lock
  cache:
    key:
      - pyproject.toml
      - "$LOCKFILE"
    paths:
      - .cache/uv
  script:
    - echo cache
"""
    )

    assert jobs[0].cache[0]["key"] == "pyproject.toml-uv.lock"


def test_parse_gitlab_ci_supports_cache_key_files_commits():
    jobs = parse_gitlab_ci(
        """
cache_probe:
  variables:
    LOCKFILE: uv.lock
  cache:
    key:
      prefix: commits
      files_commits:
        - pyproject.toml
        - "$LOCKFILE"
    paths:
      - .cache/uv
  script:
    - echo cache
"""
    )

    assert jobs[0].cache[0]["key"] == "commits-pyproject.toml-uv.lock"


def test_parse_gitlab_ci_expands_variables_in_cache_metadata():
    jobs = parse_gitlab_ci(
        """
cache:
  key:
    prefix: "$CI_COMMIT_REF_NAME"
    files:
      - "$LOCKFILE"
  paths:
    - "$CACHE_DIR/"
  policy: "$CACHE_POLICY"
  when: "$CACHE_WHEN"
  fallback_keys:
    - "$CI_COMMIT_REF_NAME-fallback"

cache_probe:
  variables:
    CACHE_DIR: .cache
    CACHE_WHEN: always
    LOCKFILE: uv.lock
  rules:
    - variables:
        CACHE_POLICY: pull
  script:
    - echo cache
""",
        ref="feature/cache",
    )

    assert jobs[0].cache == [
        {
            "key": "feature/cache-uv.lock",
            "untracked": False,
            "policy": "pull",
            "paths": [".cache/"],
            "when": "always",
            "fallback_keys": ["feature/cache-fallback"],
        }
    ]


def test_parse_gitlab_ci_rejects_unsupported_cache_options():
    cases = [
        (
            """
cache_probe:
  cache:
    paths:
      - .cache/
    unprotect: true
  script:
    - echo cache
""",
            "cache option(s) not supported",
            "unprotect",
        ),
        (
            """
cache_probe:
  cache:
    paths:
      - .cache/
    policy: invalid
  script:
    - echo cache
""",
            "cache policy is not supported",
            "invalid",
        ),
        (
            """
cache_probe:
  cache:
    paths:
      - .cache/
    when: delayed
  script:
    - echo cache
""",
            "cache when is not supported",
            "delayed",
        ),
    ]

    for content, message, detail in cases:
        try:
            parse_gitlab_ci(content)
        except ValueError as exc:
            error = str(exc)
            assert message in error
            assert detail in error
        else:
            raise AssertionError(f"expected ValueError for {detail}")


def test_parse_gitlab_ci_supports_extends_from_hidden_template():
    jobs = parse_gitlab_ci(
        """
stages: [build, test]

.base:
  image: python:3.12-alpine
  stage: build
  variables:
    BASE: one
    OVERRIDE: parent
  before_script:
    - echo parent-before
  script:
    - echo parent-script
  tags:
    - docker
  cache:
    key: base-cache
    paths:
      - .cache/
  artifacts:
    paths:
      - base.txt

child:
  extends: .base
  variables:
    LOCAL: two
    OVERRIDE: child
  script:
    - echo child-script
"""
    )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "child"
    assert job.stage == "build"
    assert job.image == "python:3.12-alpine"
    assert job.variables == {"BASE": "one", "LOCAL": "two", "OVERRIDE": "child"}
    assert job.script == ["echo parent-before", "echo child-script"]
    assert job.tags == ["docker"]
    assert job.cache[0]["key"] == "base-cache"
    assert job.artifacts_paths == ["base.txt"]


def test_parse_gitlab_ci_supports_multiple_extends_in_order():
    jobs = parse_gitlab_ci(
        """
.image:
  image: python:3.12-alpine
  variables:
    A: one

.script:
  script:
    - echo inherited
  variables:
    B: two

combined:
  extends:
    - .image
    - .script
  variables:
    C: three
"""
    )

    assert jobs[0].image == "python:3.12-alpine"
    assert jobs[0].script == ["echo inherited"]
    assert jobs[0].variables == {"A": "one", "B": "two", "C": "three"}


def test_parse_gitlab_ci_applies_default_inheritance_after_extends():
    jobs = parse_gitlab_ci(
        """
default:
  image: python:3.12-alpine
  before_script:
    - echo default-before
  after_script:
    - echo default-after
  tags:
    - docker
  cache:
    key: default-cache
    paths:
      - vendor/
  artifacts:
    paths:
      - default.txt

.base:
  variables:
    BASE: one

child:
  extends: .base
  script:
    - echo child
"""
    )

    job = jobs[0]
    assert job.image == "python:3.12-alpine"
    assert job.script == ["echo default-before", "echo child", "echo default-after"]
    assert job.tags == ["docker"]
    assert job.cache[0]["key"] == "default-cache"
    assert job.artifacts_paths == ["default.txt"]
    assert job.variables == {"BASE": "one"}


def test_parse_gitlab_ci_respects_inherit_default_false():
    jobs = parse_gitlab_ci(
        """
default:
  image: alpine:3.20
  before_script:
    - echo default-before
  tags:
    - docker

child:
  inherit:
    default: false
  script:
    - echo child
"""
    )

    job = jobs[0]
    assert job.image == "alpine:3.20"
    assert job.script == ["echo child"]
    assert job.tags == []


def test_parse_gitlab_ci_respects_inherit_default_key_list():
    jobs = parse_gitlab_ci(
        """
default:
  image: python:3.12-alpine
  before_script:
    - echo default-before
  tags:
    - docker

child:
  inherit:
    default:
      - image
  script:
    - echo child
"""
    )

    job = jobs[0]
    assert job.image == "python:3.12-alpine"
    assert job.script == ["echo child"]
    assert job.tags == []


def test_parse_gitlab_ci_respects_inherit_variables_false_and_list():
    jobs = parse_gitlab_ci(
        """
variables:
  KEEP: keep
  DROP: drop

no_globals:
  inherit:
    variables: false
  variables:
    LOCAL: local
  script: echo no globals

keep_one:
  inherit:
    variables:
      - KEEP
  variables:
    LOCAL: local
  script: echo keep one
"""
    )

    by_name = {job.name: job for job in jobs}
    assert by_name["no_globals"].variables == {"LOCAL": "local"}
    assert by_name["keep_one"].variables == {"KEEP": "keep", "LOCAL": "local"}


def test_parse_gitlab_ci_rejects_unknown_extends_parent():
    try:
        parse_gitlab_ci(
            """
job:
  extends: .missing
  script: echo test
"""
        )
    except ValueError as exc:
        assert "extends unknown job .missing" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_rejects_invalid_extends_shape():
    try:
        parse_gitlab_ci(
            """
job:
  extends:
    name: .base
  script: echo test
"""
        )
    except ValueError as exc:
        assert "extends must be a string or list of strings" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_rejects_circular_extends():
    try:
        parse_gitlab_ci(
            """
.a:
  extends: .b
  script: echo a

.b:
  extends: .a
  script: echo b

job:
  extends: .a
"""
        )
    except ValueError as exc:
        assert "circular extends" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_gitlab_ci_rejects_extends_depth_limit():
    content = "\n".join(
        [
            ".p0:",
            "  script: echo p0",
            *[
                f".p{index}:\n  extends: .p{index - 1}\n  script: echo p{index}"
                for index in range(1, 13)
            ],
            "job:",
            "  extends: .p12",
        ]
    )
    try:
        parse_gitlab_ci(content)
    except ValueError as exc:
        assert "extends depth limit" in str(exc)
    else:
        raise AssertionError("expected ValueError")
