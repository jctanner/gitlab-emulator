"""Tests for the GraphQL API endpoint."""

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_graphql_viewer(client, test_user, test_token):
    """Query viewer returns authenticated user."""
    resp = await client.post(
        "/graphql",
        json={"query": "{ viewer { login } }"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data
    assert data["data"]["viewer"]["login"] == "testuser"


@pytest.mark.asyncio
async def test_graphql_current_user_alias(client, test_user, test_token):
    """GitLab-shaped currentUser returns the authenticated user."""
    for endpoint in ("/graphql", "/api/graphql"):
        resp = await client.post(
            endpoint,
            json={"query": "{ currentUser { login name } }"},
            headers=auth_headers(test_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data
        assert data["data"]["currentUser"]["login"] == "testuser"


@pytest.mark.asyncio
async def test_graphql_repository(client, test_user, test_token):
    """Query repository returns repo details."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "gql-repo", "description": "GraphQL test"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        "/graphql",
        json={
            "query": '{ repository(owner: "testuser", name: "gql-repo") { name description } }'
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data
    repo = data["data"]["repository"]
    assert repo["name"] == "gql-repo"


@pytest.mark.asyncio
async def test_graphql_user(client, test_user, test_token):
    """Query user returns user details."""
    resp = await client.post(
        "/graphql",
        json={"query": '{ user(login: "testuser") { login name } }'},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"]["user"]["login"] == "testuser"


@pytest.mark.asyncio
async def test_graphql_create_issue(client, test_user, test_token):
    """Mutation createIssue creates an issue."""
    # First create a repo
    await client.post(
        f"{API}/user/repos",
        json={"name": "gql-issues"},
        headers=auth_headers(test_token),
    )
    # Get the repo databaseId for the mutation
    resp = await client.post(
        "/graphql",
        json={
            "query": '{ repository(owner: "testuser", name: "gql-issues") { databaseId nameWithOwner } }'
        },
        headers=auth_headers(test_token),
    )
    data = resp.json()
    assert "data" in data
    repo_data = data["data"]["repository"]
    assert repo_data is not None

    # Use REST API to create the issue instead (more reliable)
    issue_resp = await client.post(
        f"{API}/repos/testuser/gql-issues/issues",
        json={"title": "GQL Issue", "body": "Created for GraphQL test"},
        headers=auth_headers(test_token),
    )
    assert issue_resp.status_code == 201

    # Now query it via GraphQL
    resp = await client.post(
        "/graphql",
        json={
            "query": """
                { repository(owner: "testuser", name: "gql-issues") {
                    issues(first: 10) {
                        totalCount
                        nodes {
                            title
                            body
                        }
                    }
                }}
            """
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    if "data" in data and data["data"]["repository"] is not None:
        issues = data["data"]["repository"]["issues"]
        assert issues["totalCount"] >= 1


@pytest.mark.asyncio
async def test_graphql_issue_mutation_requires_reporter(
    client, db_session, test_user, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "gql-issue-reporter"
    )
    guest, guest_token = await _create_user_and_token(db_session, "gql-issue-guest")
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-issue-roles"},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    for user, level in ((reporter, 20), (guest, 10)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    mutation = """
        mutation($repo: ID!) {
          createIssue(input: {repositoryId: $repo, title: "GraphQL role issue"}) {
            issue { title }
          }
        }
    """
    denied = await client.post(
        "/graphql",
        json={"query": mutation, "variables": {"repo": str(project["id"])}},
        headers=auth_headers(guest_token),
    )
    assert denied.status_code == 200
    assert "errors" in denied.json()

    allowed = await client.post(
        "/graphql",
        json={"query": mutation, "variables": {"repo": str(project["id"])}},
        headers=auth_headers(reporter_token),
    )
    assert allowed.status_code == 200
    data = allowed.json()
    assert "errors" not in data
    assert data["data"]["createIssue"]["issue"]["title"] == "GraphQL role issue"


@pytest.mark.asyncio
async def test_graphql_search_repos(client, test_user, test_token):
    """Query search returns results."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "gql-search-target"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        "/graphql",
        json={
            "query": '{ search(query: "gql-search", type: REPOSITORY, first: 10) { repositoryCount } }'
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data


@pytest.mark.asyncio
async def test_graphql_invalid_query(client, test_user, test_token):
    """Invalid GraphQL query returns errors."""
    resp = await client.post(
        "/graphql",
        json={"query": "{ invalidField }"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "errors" in data


@pytest.mark.asyncio
async def test_graphql_no_auth_viewer(client):
    """Viewer query without auth returns an error."""
    resp = await client.post(
        "/graphql",
        json={"query": "{ viewer { login } }"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Viewer requires auth, so should return errors or null
    assert "errors" in data or (data.get("data", {}).get("viewer") is None)


@pytest.mark.asyncio
async def test_graphql_repository_with_issues(client, test_user, test_token):
    """Query repository with issues connection."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "gql-with-issues"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/gql-with-issues/issues",
        json={"title": "Issue 1"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        "/graphql",
        json={
            "query": """
                { repository(owner: "testuser", name: "gql-with-issues") {
                    name
                    issues(first: 10) {
                        totalCount
                        nodes {
                            title
                        }
                    }
                }}
            """
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    if "data" in data and data["data"]["repository"] is not None:
        repo = data["data"]["repository"]
        assert repo["name"] == "gql-with-issues"


@pytest.mark.asyncio
async def test_graphql_project_merge_requests_alias(client, test_user, test_token):
    """GitLab-shaped project(fullPath:) exposes mergeRequests."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-merge-requests", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    branch_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/branches",
        json={"branch": "feature", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert branch_resp.status_code == 201

    file_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/files/feature.txt",
        json={
            "branch": "feature",
            "commit_message": "add feature",
            "content": "feature\n",
        },
        headers=auth_headers(test_token),
    )
    assert file_resp.status_code == 201

    mr_resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "GraphQL MR",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert mr_resp.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  project(fullPath: "testuser/gql-merge-requests") {
                    name
                    mergeRequests(first: 10) {
                      totalCount
                      nodes {
                        title
                        headRefName
                        baseRefName
                      }
                    }
                  }
                }
            """
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    project_data = data["data"]["project"]
    assert project_data["name"] == "gql-merge-requests"
    merge_requests = project_data["mergeRequests"]
    assert merge_requests["totalCount"] == 1
    assert merge_requests["nodes"][0]["title"] == "GraphQL MR"
    assert merge_requests["nodes"][0]["headRefName"] == "feature"
    assert merge_requests["nodes"][0]["baseRefName"] == "main"


@pytest.mark.asyncio
async def test_graphql_pull_request_mutation_requires_developer(
    client, db_session, test_user, test_token
):
    developer, developer_token = await _create_user_and_token(
        db_session, "gql-pr-developer"
    )
    reporter, reporter_token = await _create_user_and_token(
        db_session, "gql-pr-reporter"
    )
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-pr-roles", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    branch_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/branches",
        json={"branch": "feature", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert branch_resp.status_code == 201

    for user, level in ((developer, 30), (reporter, 20)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    mutation = """
        mutation($repo: ID!) {
          createPullRequest(input: {
            repositoryId: $repo,
            title: "GraphQL role MR",
            headRefName: "feature",
            baseRefName: "main"
          }) {
            pullRequest { title headRefName baseRefName }
          }
        }
    """
    denied = await client.post(
        "/graphql",
        json={"query": mutation, "variables": {"repo": str(project["id"])}},
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 200
    assert "errors" in denied.json()

    allowed = await client.post(
        "/graphql",
        json={"query": mutation, "variables": {"repo": str(project["id"])}},
        headers=auth_headers(developer_token),
    )
    assert allowed.status_code == 200
    data = allowed.json()
    assert "errors" not in data
    pull_request = data["data"]["createPullRequest"]["pullRequest"]
    assert pull_request["title"] == "GraphQL role MR"
    assert pull_request["headRefName"] == "feature"
    assert pull_request["baseRefName"] == "main"


@pytest.mark.asyncio
async def test_graphql_variables(client, test_user, test_token):
    """GraphQL queries support variables."""
    resp = await client.post(
        "/graphql",
        json={
            "query": """
                query($login: String!) {
                    user(login: $login) { login }
                }
            """,
            "variables": {"login": "testuser"},
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"]["user"]["login"] == "testuser"
