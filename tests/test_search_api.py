"""Tests for the Search REST API endpoints."""

from urllib.parse import quote

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.fixture
async def search_data(client, test_user, test_token):
    """Create repos and issues to support search tests."""
    # Create multiple repos
    for name, desc in [
        ("alpha-project", "The alpha project"),
        ("beta-project", "The beta project"),
        ("gamma-tools", "Utility tools"),
    ]:
        await client.post(
            f"{API}/user/repos",
            json={"name": name, "description": desc},
            headers=auth_headers(test_token),
        )

    # Create issues in the first repo
    for title in [
        "Login page is broken",
        "Add search feature",
        "Fix homepage layout",
    ]:
        await client.post(
            f"{API}/repos/testuser/alpha-project/issues",
            json={"title": title, "body": f"Details about: {title}"},
            headers=auth_headers(test_token),
        )

    return True


# ---------------------------------------------------------------------------
# Search repositories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_repositories_by_name(client, test_user, test_token, search_data):
    """GET /search/repositories?q=... finds repos by name."""
    resp = await client.get(
        f"{API}/search/repositories?q=alpha",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "total_count" in data
    assert "incomplete_results" in data
    assert "items" in data
    assert data["total_count"] >= 1
    names = [r["name"] for r in data["items"]]
    assert "alpha-project" in names


@pytest.mark.asyncio
async def test_search_repositories_by_description(
    client, test_user, test_token, search_data
):
    """Search repositories matches on description."""
    resp = await client.get(
        f"{API}/search/repositories?q=utility",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] >= 1
    names = [r["name"] for r in data["items"]]
    assert "gamma-tools" in names


@pytest.mark.asyncio
async def test_search_repositories_no_results(
    client, test_user, test_token, search_data
):
    """Search with no matches returns zero results."""
    resp = await client.get(
        f"{API}/search/repositories?q=zzzznonexistent",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert data["items"] == []
    assert data["incomplete_results"] is False


@pytest.mark.asyncio
async def test_search_repositories_pagination(
    client, test_user, test_token, search_data
):
    """Search repositories respects per_page parameter."""
    resp = await client.get(
        f"{API}/search/repositories?q=project&per_page=1",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) <= 1
    assert data["total_count"] >= 2


# ---------------------------------------------------------------------------
# Search issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_issues_by_title(client, test_user, test_token, search_data):
    """GET /search/issues?q=... finds issues by title."""
    resp = await client.get(
        f"{API}/search/issues?q=broken",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] >= 1
    titles = [i["title"] for i in data["items"]]
    assert "Login page is broken" in titles


@pytest.mark.asyncio
async def test_search_issues_by_body(client, test_user, test_token, search_data):
    """Search issues matches on body text."""
    resp = await client.get(
        f"{API}/search/issues?q=search+feature",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] >= 1


@pytest.mark.asyncio
async def test_search_issues_no_results(client, test_user, test_token, search_data):
    """Search issues with no matches returns zero results."""
    resp = await client.get(
        f"{API}/search/issues?q=xxxxxxxxnothing",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_search_issues_response_shape(client, test_user, test_token, search_data):
    """Verify the search issues response has the expected structure."""
    resp = await client.get(
        f"{API}/search/issues?q=Login",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "total_count" in data
    assert "incomplete_results" in data
    assert "items" in data
    if data["items"]:
        item = data["items"][0]
        assert "title" in item
        assert "number" in item
        assert "state" in item


# ---------------------------------------------------------------------------
# Search users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_users_by_login(client, test_user, test_token):
    """GET /search/users?q=... finds users by login."""
    resp = await client.get(
        f"{API}/search/users?q=testuser",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] >= 1
    logins = [u["login"] for u in data["items"]]
    assert "testuser" in logins


@pytest.mark.asyncio
async def test_search_users_by_name(client, test_user, test_token):
    """Search users matches on display name."""
    resp = await client.get(
        f"{API}/search/users?q=Test+User",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] >= 1


@pytest.mark.asyncio
async def test_search_users_no_results(client, test_user, test_token):
    """Search users with no matches returns zero results."""
    resp = await client.get(
        f"{API}/search/users?q=zzzznoone",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_search_users_response_shape(client, test_user, test_token):
    """Verify the search users response has the expected structure."""
    resp = await client.get(
        f"{API}/search/users?q=testuser",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "total_count" in data
    assert "incomplete_results" in data
    assert "items" in data
    if data["items"]:
        user = data["items"][0]
        assert "login" in user
        assert "id" in user


@pytest.mark.asyncio
async def test_gitlab_global_search_projects_and_issues(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-search-project", "description": "Needle project"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    issue = await client.post(
        f"{API}/projects/{project_id}/issues",
        json={"title": "Needle issue", "description": "Searchable issue body"},
        headers=auth_headers(test_token),
    )
    assert issue.status_code == 201

    projects = await client.get(
        f"{API}/search",
        params={"scope": "projects", "search": "Needle"},
        headers=auth_headers(test_token),
    )
    assert projects.status_code == 200
    assert any(item["id"] == project_id for item in projects.json())

    issues = await client.get(
        f"{API}/search",
        params={"scope": "issues", "search": "Needle issue"},
        headers=auth_headers(test_token),
    )
    assert issues.status_code == 200
    assert any(item["title"] == "Needle issue" for item in issues.json())


@pytest.mark.asyncio
async def test_gitlab_global_search_users(client, db_session):
    from app.models.user import User

    db_session.add_all(
        [
            User(
                login="global-search-user-a",
                name="Global Search User A",
                email="global-search-a@example.com",
                hashed_password="x",
            ),
            User(
                login="global-search-user-b",
                name="Global Search User B",
                email="global-search-b@example.com",
                hashed_password="x",
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"{API}/search",
        params={"scope": "users", "search": "global-search", "page": 1, "per_page": 1},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Total"] == "2"
    assert resp.headers["X-Total-Pages"] == "2"
    assert resp.headers["X-Next-Page"] == "2"
    assert "rel=\"next\"" in resp.headers["Link"]
    data = resp.json()
    assert len(data) == 1
    assert data[0]["username"] == "global-search-user-a"
    assert data[0]["public_email"] == "global-search-a@example.com"
    assert data[0]["web_url"].endswith("/global-search-user-a")


@pytest.mark.asyncio
async def test_gitlab_global_search_merge_requests(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-search-mr", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    branch = await client.post(
        f"{API}/projects/{project_id}/repository/branches",
        json={"branch": "feature", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert branch.status_code == 201

    file_path = quote("search-mr.txt", safe="")
    create_file = await client.post(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        json={
            "branch": "feature",
            "commit_message": "add search mr file",
            "content": "search mr\n",
        },
        headers=auth_headers(test_token),
    )
    assert create_file.status_code == 201

    merge_request = await client.post(
        f"{API}/projects/{project_id}/merge_requests",
        json={
            "title": "Needle merge request",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert merge_request.status_code == 201

    resp = await client.get(
        f"{API}/search",
        params={"scope": "merge_requests", "search": "Needle merge"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    assert any(item["title"] == "Needle merge request" for item in resp.json())


@pytest.mark.asyncio
async def test_gitlab_global_search_milestones(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-search-milestone"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    milestone = await client.post(
        f"{API}/projects/{project_id}/milestones",
        json={
            "title": "Needle milestone",
            "description": "Searchable milestone body",
            "due_on": "2026-07-01",
        },
        headers=auth_headers(test_token),
    )
    assert milestone.status_code == 201
    milestone_id = milestone.json()["id"]

    issue = await client.post(
        f"{API}/repos/testuser/gitlab-search-milestone/issues",
        json={
            "title": "Needle milestone issue",
            "body": "Searchable issue body",
            "milestone": 1,
        },
        headers=auth_headers(test_token),
    )
    assert issue.status_code == 201

    resp = await client.get(
        f"{API}/search",
        params={"scope": "milestones", "search": "Needle milestone"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == milestone_id
    assert data[0]["project_id"] == project_id
    assert data[0]["title"] == "Needle milestone"
    assert data[0]["description"] == "Searchable milestone body"
    assert data[0]["due_date"] == "2026-07-01"
    assert data[0]["open_issues"] == 1
    assert data[0]["closed_issues"] == 0
    assert data[0]["web_url"].endswith(
        "/testuser/gitlab-search-milestone/-/milestones/1"
    )


@pytest.mark.asyncio
async def test_gitlab_global_search_blobs(client, test_user, test_token, db_session):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-search-blob"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    from app.models.search_index import FileContent

    db_session.add(
        FileContent(
            repo_id=project_id,
            file_path="docs/search.md",
            blob_sha="a" * 40,
            content="blob needle content",
            language="Markdown",
            size=19,
            ref="main",
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"{API}/search",
        params={"scope": "blobs", "search": "needle"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(item["filename"] == "docs/search.md" for item in data)


async def test_gitlab_global_search_commits(client, test_user, test_token, db_session):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-search-commit"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    from app.models.search_index import CommitMetadata

    db_session.add(
        CommitMetadata(
            repo_id=project_id,
            commit_sha="d" * 40,
            author_name="Search Author",
            author_email="author@example.com",
            committer_name="Search Committer",
            committer_email="committer@example.com",
            message="searchable commit title\n\ncommit body",
            author_date="2026-01-01T00:00:00Z",
            committer_date="2026-01-02T00:00:00Z",
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"{API}/search",
        params={"scope": "commits", "search": "searchable commit"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0] == {
        "id": "d" * 40,
        "short_id": "d" * 8,
        "created_at": "2026-01-02T00:00:00Z",
        "parent_ids": [],
        "title": "searchable commit title",
        "message": "searchable commit title\n\ncommit body",
        "author_name": "Search Author",
        "author_email": "author@example.com",
        "authored_date": "2026-01-01T00:00:00Z",
        "committer_name": "Search Committer",
        "committer_email": "committer@example.com",
        "committed_date": "2026-01-02T00:00:00Z",
        "trailers": {},
        "extended_trailers": {},
        "web_url": data[0]["web_url"],
        "project_id": project_id,
    }
    assert data[0]["web_url"].endswith(
        "/testuser/gitlab-search-commit/-/commit/" + ("d" * 40)
    )


@pytest.mark.asyncio
async def test_search_hides_private_project_content_from_non_members(
    client, db_session, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "search-private-reporter"
    )
    _outsider, outsider_token = await _create_user_and_token(
        db_session, "search-private-outsider"
    )
    project = await client.post(
        f"{API}/projects",
        json={
            "name": "private-search-project",
            "description": "private-search-needle project",
            "visibility": "private",
        },
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    issue = await client.post(
        f"{API}/projects/{project_id}/issues",
        json={
            "title": "private-search-needle issue",
            "description": "private-search-needle issue body",
        },
        headers=auth_headers(test_token),
    )
    assert issue.status_code == 201

    from app.models.search_index import CommitMetadata, FileContent

    db_session.add(
        FileContent(
            repo_id=project_id,
            file_path="docs/private-search.md",
            blob_sha="b" * 40,
            content="private-search-needle blob",
            language="Markdown",
            size=26,
            ref="main",
        )
    )
    db_session.add(
        CommitMetadata(
            repo_id=project_id,
            commit_sha="c" * 40,
            author_name="Search Author",
            author_email="search@example.com",
            committer_name="Search Committer",
            committer_email="search@example.com",
            message="private-search-needle commit",
            author_date="2026-01-01T00:00:00Z",
            committer_date="2026-01-01T00:00:00Z",
        )
    )
    await db_session.commit()

    for path, params, result_key in [
        (
            f"{API}/search",
            {"scope": "projects", "search": "private-search-needle"},
            None,
        ),
        (f"{API}/search", {"scope": "issues", "search": "private-search-needle"}, None),
        (f"{API}/search", {"scope": "blobs", "search": "private-search-needle"}, None),
        (f"{API}/search", {"scope": "commits", "search": "private-search-needle"}, None),
        (f"{API}/search/repositories", {"q": "private-search-needle"}, "items"),
        (f"{API}/search/issues", {"q": "private-search-needle"}, "items"),
        (f"{API}/search/code", {"q": "private-search-needle"}, "items"),
        (f"{API}/search/commits", {"q": "private-search-needle"}, "items"),
    ]:
        denied = await client.get(
            path,
            params=params,
            headers=auth_headers(outsider_token),
        )
        assert denied.status_code == 200
        denied_data = denied.json()
        denied_items = denied_data[result_key] if result_key else denied_data
        assert denied_items == []

    member = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": reporter.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    projects = await client.get(
        f"{API}/search",
        params={"scope": "projects", "search": "private-search-needle"},
        headers=auth_headers(reporter_token),
    )
    assert projects.status_code == 200
    assert [item["id"] for item in projects.json()] == [project_id]

    issues = await client.get(
        f"{API}/search/issues",
        params={"q": "private-search-needle"},
        headers=auth_headers(reporter_token),
    )
    assert issues.status_code == 200
    assert [item["title"] for item in issues.json()["items"]] == [
        "private-search-needle issue"
    ]

    code = await client.get(
        f"{API}/search/code",
        params={"q": "private-search-needle"},
        headers=auth_headers(reporter_token),
    )
    assert code.status_code == 200
    assert [item["path"] for item in code.json()["items"]] == ["docs/private-search.md"]

    commits = await client.get(
        f"{API}/search/commits",
        params={"q": "private-search-needle"},
        headers=auth_headers(reporter_token),
    )
    assert commits.status_code == 200
    assert [item["sha"] for item in commits.json()["items"]] == ["c" * 40]

    gitlab_commits = await client.get(
        f"{API}/search",
        params={"scope": "commits", "search": "private-search-needle"},
        headers=auth_headers(reporter_token),
    )
    assert gitlab_commits.status_code == 200
    assert [item["id"] for item in gitlab_commits.json()] == ["c" * 40]
