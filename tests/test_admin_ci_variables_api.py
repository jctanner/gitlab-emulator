"""Tests for admin-only instance CI/CD variable API endpoints."""

import json

import pytest

from tests.conftest import auth_headers

API = "/api/v4"


@pytest.mark.asyncio
async def test_instance_variables_require_admin(client, test_user, test_token):
    unauthenticated = await client.get(f"{API}/admin/ci/variables")
    assert unauthenticated.status_code == 401

    forbidden = await client.get(
        f"{API}/admin/ci/variables",
        headers=auth_headers(test_token),
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_instance_variables_crud_and_environment_scope(client, admin_token):
    created = await client.post(
        f"{API}/admin/ci/variables",
        json={
            "key": "GLOBAL_TOKEN",
            "value": "token-one",
            "masked": True,
            "raw": True,
            "description": "shared deployment credential",
        },
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 201
    assert created.json() == {
        "key": "GLOBAL_TOKEN",
        "variable_type": "env_var",
        "value": "token-one",
        "protected": False,
        "masked": True,
        "hidden": False,
        "raw": True,
        "environment_scope": "*",
        "description": "shared deployment credential",
    }

    scoped = await client.post(
        f"{API}/admin/ci/variables",
        json={
            "key": "GLOBAL_TOKEN",
            "value": "token-prod",
            "environment_scope": "production",
            "protected": True,
            "variable_type": "file",
        },
        headers=auth_headers(admin_token),
    )
    assert scoped.status_code == 201

    duplicate = await client.post(
        f"{API}/admin/ci/variables",
        json={"key": "GLOBAL_TOKEN", "value": "again"},
        headers=auth_headers(admin_token),
    )
    assert duplicate.status_code == 400

    listed = await client.get(
        f"{API}/admin/ci/variables",
        headers=auth_headers(admin_token),
    )
    assert listed.status_code == 200
    assert [(item["key"], item["environment_scope"]) for item in listed.json()] == [
        ("GLOBAL_TOKEN", "*"),
        ("GLOBAL_TOKEN", "production"),
    ]

    filtered = await client.get(
        f"{API}/admin/ci/variables",
        params={"filter[environment_scope]": "production"},
        headers=auth_headers(admin_token),
    )
    assert filtered.status_code == 200
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["value"] == "token-prod"

    fetched = await client.get(
        f"{API}/admin/ci/variables/GLOBAL_TOKEN",
        params={"filter[environment_scope]": "production"},
        headers=auth_headers(admin_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["variable_type"] == "file"
    assert fetched.json()["protected"] is True

    updated = await client.put(
        f"{API}/admin/ci/variables/GLOBAL_TOKEN",
        json={"value": "token-two", "masked": False, "description": "updated"},
        headers=auth_headers(admin_token),
    )
    assert updated.status_code == 200
    assert updated.json()["value"] == "token-two"
    assert updated.json()["masked"] is False
    assert updated.json()["description"] == "updated"

    delete_default = await client.delete(
        f"{API}/admin/ci/variables/GLOBAL_TOKEN",
        headers=auth_headers(admin_token),
    )
    assert delete_default.status_code == 204

    missing_default = await client.get(
        f"{API}/admin/ci/variables/GLOBAL_TOKEN",
        headers=auth_headers(admin_token),
    )
    assert missing_default.status_code == 404

    still_scoped = await client.get(
        f"{API}/admin/ci/variables/GLOBAL_TOKEN",
        params={"filter[environment_scope]": "production"},
        headers=auth_headers(admin_token),
    )
    assert still_scoped.status_code == 200


@pytest.mark.asyncio
async def test_instance_variable_hidden_value_is_not_read_back(client, admin_token):
    created = await client.post(
        f"{API}/admin/ci/variables",
        json={"key": "SECRET_TOKEN", "value": "super-secret-value", "hidden": True},
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 201
    assert created.json()["value"] is None
    assert created.json()["masked"] is True
    assert created.json()["hidden"] is True

    fetched = await client.get(
        f"{API}/admin/ci/variables/SECRET_TOKEN",
        headers=auth_headers(admin_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["value"] is None
    assert "super-secret-value" not in json.dumps(fetched.json())


@pytest.mark.asyncio
async def test_instance_variable_rejects_invalid_key(client, admin_token):
    resp = await client.post(
        f"{API}/admin/ci/variables",
        json={"key": "BAD KEY", "value": "value"},
        headers=auth_headers(admin_token),
    )

    assert resp.status_code == 400
