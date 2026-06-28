"""Tests for the Labels REST API endpoints."""

from urllib.parse import quote

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.fixture
async def label_repo(client, test_user, test_token):
    """Create a repo for label tests."""
    resp = await client.post(
        f"{API}/user/repos",
        json={"name": "label-repo"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def label_repo_with_issue(client, test_user, test_token, label_repo):
    """Create a repo with an issue for issue-label tests."""
    resp = await client.post(
        f"{API}/repos/testuser/label-repo/issues",
        json={"title": "Label test issue"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    return label_repo


# ---------------------------------------------------------------------------
# Repo-level label CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_project_labels_crud_and_pagination(client, test_user, test_token):
    """GitLab-shaped project labels support CRUD, search, counts, and pagination."""
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "gitlab-labels"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    first = await client.post(
        f"{API}/projects/{project_id}/labels",
        json={
            "name": "bug",
            "color": "#d73a4a",
            "description": "Something is not working",
        },
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201
    assert first.json()["name"] == "bug"
    assert first.json()["color"] == "#d73a4a"
    assert first.json()["is_project_label"] is True

    duplicate = await client.post(
        f"{API}/projects/{project_id}/labels",
        json={"name": "bug", "color": "#000000"},
        headers=auth_headers(test_token),
    )
    assert duplicate.status_code == 409

    second = await client.post(
        f"{API}/projects/{project_id}/labels",
        json={"name": "backend", "color": "0052cc"},
        headers=auth_headers(test_token),
    )
    assert second.status_code == 201

    await client.post(
        f"{API}/projects/{project_id}/issues",
        json={"title": "uses bug", "labels": "bug"},
        headers=auth_headers(test_token),
    )

    listed = await client.get(
        f"{API}/projects/{project_id}/labels",
        params={"search": "b", "with_counts": "true", "page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["x-total"] == "2"
    assert listed.headers["x-next-page"] == "2"
    assert 'rel="next"' in listed.headers["link"]
    assert len(listed.json()) == 1
    assert {"open_issues_count", "closed_issues_count"} <= set(listed.json()[0])

    encoded = quote("testuser/gitlab-labels", safe="")
    fetched = await client.get(
        f"{API}/projects/{encoded}/labels/bug",
        params={"with_counts": "true"},
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["open_issues_count"] == 1

    updated = await client.put(
        f"{API}/projects/{project_id}/labels/bug",
        json={"new_name": "defect", "color": "#ff0000", "description": "Defect"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "defect"
    assert updated.json()["color"] == "#ff0000"
    assert updated.json()["description"] == "Defect"

    deleted = await client.delete(
        f"{API}/projects/{project_id}/labels/defect",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/projects/{project_id}/labels/defect",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_create_label(client, test_user, test_token, label_repo):
    """POST /repos/{owner}/{repo}/labels creates a label."""
    resp = await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "bug"
    assert data["color"] == "d73a4a"
    assert data["description"] == "Something isn't working"
    assert "id" in data
    assert "node_id" in data
    assert "url" in data


@pytest.mark.asyncio
async def test_create_label_strips_hash(client, test_user, test_token, label_repo):
    """Creating a label with '#' prefix in color strips the hash."""
    resp = await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "enhancement", "color": "#a2eeef"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    assert resp.json()["color"] == "a2eeef"


@pytest.mark.asyncio
async def test_create_duplicate_label(client, test_user, test_token, label_repo):
    """Creating a label with the same name returns 422."""
    await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "duplicate", "color": "000000"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "duplicate", "color": "111111"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_label_definition_writes_require_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "label-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "label-developer"
    )
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "label-role-gate"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    for user, level in ((maintainer, 40), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project.json()['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied = await client.post(
        f"{API}/repos/testuser/label-role-gate/labels",
        json={"name": "developer", "color": "000000"},
        headers=auth_headers(developer_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/repos/testuser/label-role-gate/labels",
        json={"name": "maintainer", "color": "111111"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_list_labels(client, test_user, test_token, label_repo):
    """GET /repos/{owner}/{repo}/labels lists all labels."""
    for name in ["bug", "feature", "docs"]:
        await client.post(
            f"{API}/repos/testuser/label-repo/labels",
            json={"name": name, "color": "ededed"},
            headers=auth_headers(test_token),
        )
    resp = await client.get(
        f"{API}/repos/testuser/label-repo/labels",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 3
    names = [l["name"] for l in data]
    assert "bug" in names
    assert "feature" in names
    assert "docs" in names


@pytest.mark.asyncio
async def test_get_label_by_name(client, test_user, test_token, label_repo):
    """GET /repos/{owner}/{repo}/labels/{name} returns a single label."""
    await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "important", "color": "ff0000"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(
        f"{API}/repos/testuser/label-repo/labels/important",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "important"
    assert data["color"] == "ff0000"


@pytest.mark.asyncio
async def test_get_nonexistent_label(client, test_user, test_token, label_repo):
    """GET /repos/{owner}/{repo}/labels/{name} returns 404 for missing label."""
    resp = await client.get(
        f"{API}/repos/testuser/label-repo/labels/nonexistent",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_label(client, test_user, test_token, label_repo):
    """PATCH /repos/{owner}/{repo}/labels/{name} updates the label."""
    await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "old-name", "color": "aabbcc"},
        headers=auth_headers(test_token),
    )
    resp = await client.patch(
        f"{API}/repos/testuser/label-repo/labels/old-name",
        json={"new_name": "new-name", "color": "112233", "description": "Updated desc"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "new-name"
    assert data["color"] == "112233"
    assert data["description"] == "Updated desc"


@pytest.mark.asyncio
async def test_update_nonexistent_label(client, test_user, test_token, label_repo):
    """PATCH for a nonexistent label returns 404."""
    resp = await client.patch(
        f"{API}/repos/testuser/label-repo/labels/does-not-exist",
        json={"color": "ffffff"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_label(client, test_user, test_token, label_repo):
    """DELETE /repos/{owner}/{repo}/labels/{name} removes the label."""
    await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "to-delete", "color": "000000"},
        headers=auth_headers(test_token),
    )
    resp = await client.delete(
        f"{API}/repos/testuser/label-repo/labels/to-delete",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 204

    # Verify it's gone
    resp = await client.get(
        f"{API}/repos/testuser/label-repo/labels/to-delete",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_label(client, test_user, test_token, label_repo):
    """DELETE for a nonexistent label returns 404."""
    resp = await client.delete(
        f"{API}/repos/testuser/label-repo/labels/nope",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Issue-level label management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_labels_to_issue(client, test_user, test_token, label_repo_with_issue):
    """POST /repos/{owner}/{repo}/issues/{number}/labels adds labels."""
    # Create two labels on the repo first
    for name in ["bug", "urgent"]:
        await client.post(
            f"{API}/repos/testuser/label-repo/labels",
            json={"name": name, "color": "ededed"},
            headers=auth_headers(test_token),
        )

    resp = await client.post(
        f"{API}/repos/testuser/label-repo/issues/1/labels",
        json={"labels": ["bug", "urgent"]},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    names = [l["name"] for l in data]
    assert "bug" in names
    assert "urgent" in names


@pytest.mark.asyncio
async def test_issue_label_assignment_requires_developer(
    client, db_session, test_user, test_token
):
    developer, developer_token = await _create_user_and_token(
        db_session, "issue-label-developer"
    )
    reporter, reporter_token = await _create_user_and_token(
        db_session, "issue-label-reporter"
    )
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "issue-label-role-gate"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    for user, level in ((developer, 30), (reporter, 20)):
        member = await client.post(
            f"{API}/projects/{project.json()['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201
    issue = await client.post(
        f"{API}/repos/testuser/issue-label-role-gate/issues",
        json={"title": "issue label gate"},
        headers=auth_headers(test_token),
    )
    assert issue.status_code == 201
    label = await client.post(
        f"{API}/repos/testuser/issue-label-role-gate/labels",
        json={"name": "triaged", "color": "0e8a16"},
        headers=auth_headers(test_token),
    )
    assert label.status_code == 201

    denied = await client.post(
        f"{API}/repos/testuser/issue-label-role-gate/issues/1/labels",
        json={"labels": ["triaged"]},
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/repos/testuser/issue-label-role-gate/issues/1/labels",
        json={"labels": ["triaged"]},
        headers=auth_headers(developer_token),
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_list_issue_labels(client, test_user, test_token, label_repo_with_issue):
    """GET /repos/{owner}/{repo}/issues/{number}/labels lists labels on the issue."""
    # Create and add a label
    await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "listed", "color": "aaaaaa"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/label-repo/issues/1/labels",
        json={"labels": ["listed"]},
        headers=auth_headers(test_token),
    )

    resp = await client.get(
        f"{API}/repos/testuser/label-repo/issues/1/labels",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(l["name"] == "listed" for l in data)


@pytest.mark.asyncio
async def test_remove_label_from_issue(client, test_user, test_token, label_repo_with_issue):
    """DELETE /repos/{owner}/{repo}/issues/{number}/labels/{name} removes a label."""
    # Create and add a label
    await client.post(
        f"{API}/repos/testuser/label-repo/labels",
        json={"name": "removable", "color": "bbbbbb"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/label-repo/issues/1/labels",
        json={"labels": ["removable"]},
        headers=auth_headers(test_token),
    )

    resp = await client.delete(
        f"{API}/repos/testuser/label-repo/issues/1/labels/removable",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    names = [l["name"] for l in data]
    assert "removable" not in names


@pytest.mark.asyncio
async def test_remove_nonexistent_label_from_issue(client, test_user, test_token, label_repo_with_issue):
    """Removing a label that doesn't exist from an issue returns 404."""
    resp = await client.delete(
        f"{API}/repos/testuser/label-repo/issues/1/labels/nonexistent",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404
