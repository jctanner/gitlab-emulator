"""CI security diagnostic tests."""

from app.services.ci_security import (
    pipeline_security_warnings,
    pipeline_variables_allowed_for_user,
    strict_security_blocks,
)
from app.services.ci_yaml import ParsedCiJob


def test_ci_security_warnings_cover_images_includes_and_predefined_variables():
    warnings = pipeline_security_warnings(
        ci_content="""
include:
  - remote: http://example.test/ci.yml
  - remote: https://example.test/floating.yml
""",
        parsed_jobs=[
            ParsedCiJob(name="latest", image="alpine:latest"),
            ParsedCiJob(name="implicit", image="busybox"),
            ParsedCiJob(name="variable", image="$CI_IMAGE"),
        ],
        pipeline_variables=[
            type("PipelineVariable", (), {"key": "CI_COMMIT_SHA"})(),
            type("PipelineVariable", (), {"key": "CUSTOM"})(),
        ],
        settings={"ci_strict_security_mode": True},
    )

    warning_types = [warning["type"] for warning in warnings]
    assert "mutable_image_ref" in warning_types
    assert "variable_image_ref" in warning_types
    assert "unsafe_remote_include" in warning_types
    assert "unpinned_remote_include" in warning_types
    assert "predefined_variable_override" in warning_types
    assert all(warning["strict_mode"] is True for warning in warnings)


def test_ci_security_strict_blocks_only_strict_block_types():
    warnings = [
        {"type": "mutable_image_ref", "message": "mutable", "severity": "warning"},
        {
            "type": "predefined_variable_override",
            "message": "override",
            "severity": "warning",
        },
    ]

    blocks = strict_security_blocks(
        warnings,
        {"ci_strict_security_mode": True},
    )

    assert blocks == [
        {"type": "mutable_image_ref", "message": "mutable", "severity": "error"}
    ]


def test_ci_security_pipeline_variable_gate_uses_owner_admin_or_no_one():
    class User:
        def __init__(self, user_id, site_admin=False):
            self.id = user_id
            self.site_admin = site_admin

    assert not pipeline_variables_allowed_for_user(
        settings={"ci_pipeline_variables_minimum_override_role": "no_one_allowed"},
        project_owner_id=1,
        user=User(1),
    )
    assert not pipeline_variables_allowed_for_user(
        settings={"ci_pipeline_variables_minimum_override_role": "owner"},
        project_owner_id=1,
        user=None,
    )
    assert pipeline_variables_allowed_for_user(
        settings={"ci_pipeline_variables_minimum_override_role": "owner"},
        project_owner_id=1,
        user=User(1),
    )
    assert pipeline_variables_allowed_for_user(
        settings={"ci_pipeline_variables_minimum_override_role": "maintainer"},
        project_owner_id=1,
        user=User(2, site_admin=True),
    )
