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

regex:
  script: echo regex
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ /^release-/'

and_or:
  script: echo and or
  rules:
    - if: '$TARGET == "prod" && $RUN_TRUTHY || $NEVER'

skipped:
  script: echo skipped
  rules:
    - if: '$TARGET == "prod"'
      when: never
""",
        ref="release-1.0",
        variables={"RUN_TRUTHY": "1", "TARGET": "prod"},
    )

    assert [job.name for job in jobs] == [
        "and_or",
        "equals",
        "not_equals",
        "regex",
        "truthy",
    ]


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
