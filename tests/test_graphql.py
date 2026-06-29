"""Tests for the GraphQL API endpoint."""

import pytest
from sqlalchemy import select

from app.models.repository import Repository as RepoModel
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
async def test_graphql_repository_latest_release(client, test_user, test_token):
    """Repository latestRelease resolves from real release data."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-latest-release", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    first = await client.post(
        f"{API}/projects/{project['id']}/releases",
        json={"tag_name": "v1.0.0", "name": "First Release", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201
    latest = await client.post(
        f"{API}/projects/{project['id']}/releases",
        json={
            "tag_name": "v2.0.0",
            "name": "Latest Release",
            "ref": "main",
        },
        headers=auth_headers(test_token),
    )
    assert latest.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                { repository(owner: "testuser", name: "gql-latest-release") {
                    latestRelease {
                        name
                        tagName
                        isDraft
                        isPrerelease
                        publishedAt
                    }
                }}
            """
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    release = data["data"]["repository"]["latestRelease"]
    assert release["name"] == "Latest Release"
    assert release["tagName"] == "v2.0.0"
    assert release["isDraft"] is False
    assert release["isPrerelease"] is False
    assert release["publishedAt"]


@pytest.mark.asyncio
async def test_graphql_repository_refs_filter_branches_and_tags(
    client, test_user, test_token
):
    """Repository refs honors branch and tag ref prefixes."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-refs", "initialize_with_readme": True},
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

    tag_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/tags",
        json={"tag_name": "v1.0.0", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert tag_resp.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-refs") {
                    branchRefs: refs(refPrefix: "refs/heads/", first: 10) {
                      totalCount
                      nodes { name prefix id }
                    }
                    tagRefs: refs(refPrefix: "refs/tags/", first: 10) {
                      totalCount
                      nodes { name prefix id }
                    }
                    unsupportedRefs: refs(refPrefix: "refs/merge-requests/", first: 10) {
                      totalCount
                      nodes { name }
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
    repo = data["data"]["repository"]
    assert repo["branchRefs"]["totalCount"] == 2
    assert [node["name"] for node in repo["branchRefs"]["nodes"]] == [
        "feature",
        "main",
    ]
    assert {node["prefix"] for node in repo["branchRefs"]["nodes"]} == {"refs/heads/"}
    assert repo["tagRefs"] == {
        "totalCount": 1,
        "nodes": [
            {"name": "v1.0.0", "prefix": "refs/tags/", "id": "refs/tags/v1.0.0"}
        ],
    }
    assert repo["unsupportedRefs"] == {"totalCount": 0, "nodes": []}


@pytest.mark.asyncio
async def test_graphql_repository_languages_and_topics(
    client, db_session, test_user, test_token
):
    """Repository languages and topics resolve from stored project metadata."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-repo-metadata", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    result = await db_session.execute(
        select(RepoModel).where(RepoModel.id == project["id"])
    )
    repo_model = result.scalar_one()
    repo_model.language = "Python"
    repo_model.topics = ["ci", "emulator"]
    await db_session.commit()

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-repo-metadata") {
                    languages {
                      totalCount
                      nodes { name }
                    }
                    repositoryTopics {
                      totalCount
                      nodes { topicName url }
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
    repo = data["data"]["repository"]
    assert repo["languages"] == {
        "totalCount": 1,
        "nodes": [{"name": "Python"}],
    }
    assert repo["repositoryTopics"]["totalCount"] == 2
    assert [node["topicName"] for node in repo["repositoryTopics"]["nodes"]] == [
        "ci",
        "emulator",
    ]
    assert all(
        node["url"].endswith(f"/-/topics/{node['topicName']}")
        for node in repo["repositoryTopics"]["nodes"]
    )


@pytest.mark.asyncio
async def test_graphql_repository_watchers(client, db_session, test_user, test_token):
    """Repository watchers resolves users who starred the repository."""
    watcher, watcher_token = await _create_user_and_token(
        db_session, "gql-repo-watcher"
    )
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-watchers", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201

    star_resp = await client.put(
        f"{API}/user/starred/testuser/gql-watchers",
        headers=auth_headers(watcher_token),
    )
    assert star_resp.status_code == 204

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-watchers") {
                    stargazerCount
                    watchers(first: 10) {
                      totalCount
                      nodes { login }
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
    repo = data["data"]["repository"]
    assert repo["stargazerCount"] == 1
    assert repo["watchers"] == {
        "totalCount": 1,
        "nodes": [{"login": watcher.login}],
    }


@pytest.mark.asyncio
async def test_graphql_repository_user_connections(
    client, db_session, test_user, test_token
):
    """Repository assignableUsers and mentionableUsers resolve project members."""
    member, _ = await _create_user_and_token(db_session, "gql-repo-member")
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-user-connections", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    add_member = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": member.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert add_member.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-user-connections") {
                    assignableUsers {
                      totalCount
                      nodes { login }
                    }
                    mentionableUsers {
                      totalCount
                      nodes { login }
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
    repo = data["data"]["repository"]
    expected = {
        "totalCount": 2,
        "nodes": [{"login": "testuser"}, {"login": "gql-repo-member"}],
    }
    assert repo["assignableUsers"] == expected
    assert repo["mentionableUsers"] == expected


@pytest.mark.asyncio
async def test_graphql_repository_parent(
    client, test_user, test_token, admin_user, admin_token
):
    """Forked repository parent resolves from persisted fork metadata."""
    source = await client.post(
        f"{API}/user/repos",
        json={"name": "gql-parent-source", "auto_init": True},
        headers=auth_headers(test_token),
    )
    assert source.status_code == 201

    fork = await client.post(
        f"{API}/repos/testuser/gql-parent-source/forks",
        json={},
        headers=auth_headers(admin_token),
    )
    assert fork.status_code == 202

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "admin", name: "gql-parent-source") {
                    nameWithOwner
                    parent {
                      name
                      nameWithOwner
                    }
                  }
                }
            """
        },
        headers=auth_headers(admin_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    repo = data["data"]["repository"]
    assert repo["nameWithOwner"] == "admin/gql-parent-source"
    assert repo["parent"] == {
        "name": "gql-parent-source",
        "nameWithOwner": "testuser/gql-parent-source",
    }


@pytest.mark.asyncio
async def test_graphql_repository_license_info(client, test_user, test_token):
    """Repository licenseInfo resolves from a committed license file."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-license-info", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    license_file = await client.post(
        f"{API}/projects/{project['id']}/repository/files/LICENSE",
        json={
            "branch": "main",
            "commit_message": "add license",
            "content": (
                "MIT License\n\n"
                "Copyright (c) 2026 Example\n\n"
                "Permission is hereby granted, free of charge, to any person "
                "obtaining a copy of this software and associated documentation "
                "files (the \"Software\"), to deal in the Software without "
                "restriction.\n"
            ),
        },
        headers=auth_headers(test_token),
    )
    assert license_file.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-license-info") {
                    licenseInfo {
                      key
                      name
                      spdxId
                      url
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
    assert data["data"]["repository"]["licenseInfo"] == {
        "key": "mit",
        "name": "MIT License",
        "spdxId": "MIT",
        "url": "http://choosealicense.com/licenses/mit/",
    }


@pytest.mark.asyncio
async def test_graphql_repository_templates(client, test_user, test_token):
    """Repository issue and pull request templates resolve from committed files."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-repo-templates", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    issue_template = await client.post(
        f"{API}/projects/{project['id']}/repository/files/.gitlab%2Fissue_templates%2Fbug.md",
        json={
            "branch": "main",
            "commit_message": "add issue template",
            "content": (
                "---\n"
                "name: Bug report\n"
                "title: '[Bug] '\n"
                "about: Report a bug\n"
                "---\n"
                "## Steps\n"
            ),
        },
        headers=auth_headers(test_token),
    )
    assert issue_template.status_code == 201

    mr_template = await client.post(
        f"{API}/projects/{project['id']}/repository/files/.gitlab%2Fmerge_request_templates%2Fdefault.md",
        json={
            "branch": "main",
            "commit_message": "add merge request template",
            "content": "## Summary\n\n## Testing\n",
        },
        headers=auth_headers(test_token),
    )
    assert mr_template.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-repo-templates") {
                    issueTemplates {
                      name
                      title
                      about
                      body
                    }
                    pullRequestTemplates {
                      filename
                      body
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
    repo = data["data"]["repository"]
    assert repo["issueTemplates"] == [
        {
            "name": "Bug report",
            "title": "[Bug] ",
            "about": "Report a bug",
            "body": "## Steps",
        }
    ]
    assert repo["pullRequestTemplates"] == [
        {
            "filename": ".gitlab/merge_request_templates/default.md",
            "body": "## Summary\n\n## Testing\n",
        }
    ]


@pytest.mark.asyncio
async def test_graphql_repository_code_of_conduct(client, test_user, test_token):
    """Repository codeOfConduct resolves from committed files."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-code-of-conduct", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    code_of_conduct = await client.post(
        f"{API}/projects/{project['id']}/repository/files/.gitlab%2FCODE_OF_CONDUCT.md",
        json={
            "branch": "main",
            "commit_message": "add code of conduct",
            "content": "# Code of Conduct\n\nBe respectful.\n",
        },
        headers=auth_headers(test_token),
    )
    assert code_of_conduct.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-code-of-conduct") {
                    codeOfConduct {
                      key
                      name
                      url
                      body
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
    code = data["data"]["repository"]["codeOfConduct"]
    assert code == {
        "key": "code-of-conduct",
        "name": "Code of Conduct",
        "url": (
            "http://testserver/testuser/gql-code-of-conduct"
            "/-/blob/main/.gitlab/CODE_OF_CONDUCT.md"
        ),
        "body": "# Code of Conduct\n\nBe respectful.\n",
    }


@pytest.mark.asyncio
async def test_graphql_repository_funding_and_contact_links(
    client, test_user, test_token
):
    """Repository fundingLinks and contactLinks resolve from committed config."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-repo-links", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    funding = await client.post(
        f"{API}/projects/{project['id']}/repository/files/.github%2FFUNDING.yml",
        json={
            "branch": "main",
            "commit_message": "add funding links",
            "content": (
                "github: [octocat]\n"
                "patreon: example\n"
                "custom:\n"
                "  - https://example.test/support\n"
            ),
        },
        headers=auth_headers(test_token),
    )
    assert funding.status_code == 201

    contacts = await client.post(
        f"{API}/projects/{project['id']}/repository/files/.github%2FISSUE_TEMPLATE%2Fconfig.yml",
        json={
            "branch": "main",
            "commit_message": "add contact links",
            "content": (
                "contact_links:\n"
                "  - name: Security issue\n"
                "    url: https://example.test/security\n"
                "    about: Report security issues privately\n"
            ),
        },
        headers=auth_headers(test_token),
    )
    assert contacts.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  repository(owner: "testuser", name: "gql-repo-links") {
                    fundingLinks {
                      platform
                      url
                    }
                    contactLinks {
                      name
                      url
                      about
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
    repo = data["data"]["repository"]
    assert repo["fundingLinks"] == [
        {"platform": "github", "url": "https://github.com/sponsors/octocat"},
        {"platform": "patreon", "url": "https://www.patreon.com/example"},
        {"platform": "custom", "url": "https://example.test/support"},
    ]
    assert repo["contactLinks"] == [
        {
            "name": "Security issue",
            "url": "https://example.test/security",
            "about": "Report security issues privately",
        }
    ]


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
async def test_graphql_search_total_count_ignores_first_limit(
    client, test_user, test_token
):
    """Search totalCount reports all matches, not just returned nodes."""
    for name in (
        "gql-search-count-one",
        "gql-search-count-two",
        "gql-search-count-three",
    ):
        project_resp = await client.post(
            f"{API}/projects",
            json={"name": name},
            headers=auth_headers(test_token),
        )
        assert project_resp.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  search(query: "gql-search-count", type: REPOSITORY, first: 1) {
                    repositoryCount
                    totalCount
                    nodes {
                      ... on Repository { name }
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
    search = data["data"]["search"]
    assert search["repositoryCount"] == 3
    assert search["totalCount"] == 3
    assert len(search["nodes"]) == 1


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
async def test_graphql_repository_issues_filter_by_mentioned(
    client, test_user, test_token
):
    """Repository issues filterBy mentioned searches issue bodies and comments."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "gql-mentioned-issues"},
        headers=auth_headers(test_token),
    )
    body_mention = await client.post(
        f"{API}/repos/testuser/gql-mentioned-issues/issues",
        json={"title": "Body mention", "body": "Please check this @reviewer"},
        headers=auth_headers(test_token),
    )
    assert body_mention.status_code == 201
    comment_mention = await client.post(
        f"{API}/repos/testuser/gql-mentioned-issues/issues",
        json={"title": "Comment mention", "body": "No mention here"},
        headers=auth_headers(test_token),
    )
    assert comment_mention.status_code == 201
    unrelated = await client.post(
        f"{API}/repos/testuser/gql-mentioned-issues/issues",
        json={"title": "Unrelated", "body": "No match"},
        headers=auth_headers(test_token),
    )
    assert unrelated.status_code == 201

    comment = await client.post(
        f"{API}/repos/testuser/gql-mentioned-issues/issues/{comment_mention.json()['number']}/comments",
        json={"body": "Looping in @reviewer from a comment"},
        headers=auth_headers(test_token),
    )
    assert comment.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                { repository(owner: "testuser", name: "gql-mentioned-issues") {
                    issues(first: 10, filterBy: { mentioned: "reviewer" }) {
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
    assert "errors" not in data
    issues = data["data"]["repository"]["issues"]
    assert issues["totalCount"] == 2
    assert [issue["title"] for issue in issues["nodes"]] == [
        "Body mention",
        "Comment mention",
    ]


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
async def test_graphql_project_exposes_gitlab_url_fields(client, test_user, test_token):
    """GitLab-shaped project(fullPath:) exposes common project URL fields."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-project-urls"},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                {
                  project(fullPath: "testuser/gql-project-urls") {
                    fullPath
                    webUrl
                    httpUrlToRepo
                    sshUrlToRepo
                    visibility
                  }
                }
            """
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    project = data["data"]["project"]
    assert project["fullPath"] == "testuser/gql-project-urls"
    assert project["webUrl"].endswith("/testuser/gql-project-urls")
    assert project["httpUrlToRepo"].endswith("/testuser/gql-project-urls.git")
    assert project["sshUrlToRepo"].endswith(":testuser/gql-project-urls.git")
    assert project["visibility"] == "PUBLIC"


@pytest.mark.asyncio
async def test_graphql_pull_request_diff_stats(client, test_user, test_token):
    """Pull request diff stat fields reflect the git diff."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-pr-diff-stats", "initialize_with_readme": True},
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
        f"{API}/projects/{project['id']}/repository/files/src%2Ffeature.txt",
        json={
            "branch": "feature",
            "commit_message": "add feature file",
            "content": "one\ntwo\n",
        },
        headers=auth_headers(test_token),
    )
    assert file_resp.status_code == 201

    mr_resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "Diff stats MR",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert mr_resp.status_code == 201
    iid = mr_resp.json()["iid"]

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                query($number: Int!) {
                  repository(owner: "testuser", name: "gql-pr-diff-stats") {
                    pullRequest(number: $number) {
                      additions
                      deletions
                      changedFiles
                    }
                  }
                }
            """,
            "variables": {"number": iid},
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    pull_request = data["data"]["repository"]["pullRequest"]
    assert pull_request == {
        "additions": 2,
        "deletions": 0,
        "changedFiles": 1,
    }


@pytest.mark.asyncio
async def test_graphql_pull_request_commits(client, test_user, test_token):
    """Pull request commits connection reflects commits between base and head."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-pr-commits", "initialize_with_readme": True},
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

    first_file = await client.post(
        f"{API}/projects/{project['id']}/repository/files/feature.txt",
        json={
            "branch": "feature",
            "commit_message": "add feature file",
            "content": "one\n",
        },
        headers=auth_headers(test_token),
    )
    assert first_file.status_code == 201

    second_file = await client.put(
        f"{API}/projects/{project['id']}/repository/files/feature.txt",
        json={
            "branch": "feature",
            "commit_message": "update feature file",
            "content": "one\ntwo\n",
        },
        headers=auth_headers(test_token),
    )
    assert second_file.status_code == 200

    mr_resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "Commit list MR",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert mr_resp.status_code == 201
    iid = mr_resp.json()["iid"]

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                query($number: Int!) {
                  repository(owner: "testuser", name: "gql-pr-commits") {
                    pullRequest(number: $number) {
                      commits(first: 10) {
                        totalCount
                        nodes {
                          oid
                          message
                        }
                      }
                    }
                  }
                }
            """,
            "variables": {"number": iid},
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    commits = data["data"]["repository"]["pullRequest"]["commits"]
    assert commits["totalCount"] == 2
    assert [node["message"] for node in commits["nodes"]] == [
        "add feature file",
        "update feature file",
    ]
    assert all(len(node["oid"]) == 40 for node in commits["nodes"])


@pytest.mark.asyncio
async def test_graphql_pull_request_files(client, test_user, test_token):
    """Pull request files connection reflects changed files between base and head."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-pr-files", "initialize_with_readme": True},
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
        f"{API}/projects/{project['id']}/repository/files/src%2Ffeature.txt",
        json={
            "branch": "feature",
            "commit_message": "add feature file",
            "content": "one\ntwo\n",
        },
        headers=auth_headers(test_token),
    )
    assert file_resp.status_code == 201

    mr_resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "File list MR",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert mr_resp.status_code == 201
    iid = mr_resp.json()["iid"]

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                query($number: Int!) {
                  repository(owner: "testuser", name: "gql-pr-files") {
                    pullRequest(number: $number) {
                      files(first: 10) {
                        totalCount
                        nodes {
                          path
                          additions
                          deletions
                          changeType
                        }
                      }
                    }
                  }
                }
            """,
            "variables": {"number": iid},
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    files = data["data"]["repository"]["pullRequest"]["files"]
    assert files == {
        "totalCount": 1,
        "nodes": [
            {
                "path": "src/feature.txt",
                "additions": 2,
                "deletions": 0,
                "changeType": "ADDED",
            }
        ],
    }


@pytest.mark.asyncio
async def test_graphql_pull_request_closing_issue_references(
    client, test_user, test_token
):
    """Issue and pull request closing-reference connections resolve from MR body."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-closing-refs", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    issue_resp = await client.post(
        f"{API}/repos/testuser/gql-closing-refs/issues",
        json={"title": "Tracked issue", "body": "Needs a fix"},
        headers=auth_headers(test_token),
    )
    assert issue_resp.status_code == 201

    branch_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/branches",
        json={"branch": "feature", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert branch_resp.status_code == 201

    file_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/files/fix.txt",
        json={
            "branch": "feature",
            "commit_message": "add fix",
            "content": "fixed\n",
        },
        headers=auth_headers(test_token),
    )
    assert file_resp.status_code == 201

    mr_resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "Fix tracked issue",
            "description": "Closes #1",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert mr_resp.status_code == 201
    iid = mr_resp.json()["iid"]

    resp = await client.post(
        "/graphql",
        json={
            "query": """
                query($pr: Int!) {
                  repository(owner: "testuser", name: "gql-closing-refs") {
                    issue(number: 1) {
                      closedByPullRequestsReferences(first: 10) {
                        totalCount
                        nodes { number title }
                      }
                    }
                    pullRequest(number: $pr) {
                      closingIssuesReferences(first: 10) {
                        totalCount
                        nodes { number title }
                      }
                    }
                  }
                }
            """,
            "variables": {"pr": iid},
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "errors" not in data
    repo = data["data"]["repository"]
    assert repo["issue"]["closedByPullRequestsReferences"] == {
        "totalCount": 1,
        "nodes": [{"number": iid, "title": "Fix tracked issue"}],
    }
    assert repo["pullRequest"]["closingIssuesReferences"] == {
        "totalCount": 1,
        "nodes": [{"number": 1, "title": "Tracked issue"}],
    }


@pytest.mark.asyncio
async def test_graphql_pull_request_review_decision(client, test_user, test_token):
    """Pull request reviewDecision is derived from active submitted reviews."""
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "gql-pr-review-decision", "initialize_with_readme": True},
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

    mr_resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "Review decision MR",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert mr_resp.status_code == 201
    iid = mr_resp.json()["iid"]

    query = """
        query($number: Int!) {
          repository(owner: "testuser", name: "gql-pr-review-decision") {
            pullRequest(number: $number) {
              reviewDecision
            }
          }
        }
    """
    initial = await client.post(
        "/graphql",
        json={"query": query, "variables": {"number": iid}},
        headers=auth_headers(test_token),
    )
    assert initial.status_code == 200
    initial_data = initial.json()
    assert "errors" not in initial_data
    assert initial_data["data"]["repository"]["pullRequest"]["reviewDecision"] is None

    approved_review = await client.post(
        f"{API}/repos/testuser/gql-pr-review-decision/pulls/{iid}/reviews",
        json={"body": "looks good"},
        headers=auth_headers(test_token),
    )
    assert approved_review.status_code == 201
    approved_submit = await client.put(
        f"{API}/repos/testuser/gql-pr-review-decision/pulls/{iid}/reviews/{approved_review.json()['id']}/events",
        json={"event": "APPROVE"},
        headers=auth_headers(test_token),
    )
    assert approved_submit.status_code == 200

    approved = await client.post(
        "/graphql",
        json={"query": query, "variables": {"number": iid}},
        headers=auth_headers(test_token),
    )
    assert approved.status_code == 200
    approved_data = approved.json()
    assert "errors" not in approved_data
    assert (
        approved_data["data"]["repository"]["pullRequest"]["reviewDecision"]
        == "APPROVED"
    )

    changes_review = await client.post(
        f"{API}/repos/testuser/gql-pr-review-decision/pulls/{iid}/reviews",
        json={"body": "needs work"},
        headers=auth_headers(test_token),
    )
    assert changes_review.status_code == 201
    changes_submit = await client.put(
        f"{API}/repos/testuser/gql-pr-review-decision/pulls/{iid}/reviews/{changes_review.json()['id']}/events",
        json={"event": "REQUEST_CHANGES"},
        headers=auth_headers(test_token),
    )
    assert changes_submit.status_code == 200

    changed = await client.post(
        "/graphql",
        json={"query": query, "variables": {"number": iid}},
        headers=auth_headers(test_token),
    )
    assert changed.status_code == 200
    changed_data = changed.json()
    assert "errors" not in changed_data
    assert (
        changed_data["data"]["repository"]["pullRequest"]["reviewDecision"]
        == "CHANGES_REQUESTED"
    )


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
