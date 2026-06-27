"""CI security diagnostic tests."""

from app.services.ci_security import (
    pipeline_variable_policy,
    pipeline_security_warnings,
    strict_security_blocks,
)
from app.services.permissions import pipeline_variables_allowed_for_access_level
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


def test_ci_security_pipeline_variable_gate_uses_gitlab_access_levels():
    assert pipeline_variable_policy(
        {"ci_pipeline_variables_minimum_override_role": "owner"}
    ) == "owner"
    assert not pipeline_variables_allowed_for_access_level(
        policy="no_one_allowed", access_level=50
    )
    assert not pipeline_variables_allowed_for_access_level(
        policy="owner", access_level=40
    )
    assert pipeline_variables_allowed_for_access_level(
        policy="maintainer", access_level=40
    )
    assert pipeline_variables_allowed_for_access_level(
        policy="developer", access_level=30
    )
