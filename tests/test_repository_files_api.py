"""Tests for GitLab repository files API endpoints."""

import base64
from urllib.parse import quote

import pytest

from tests.conftest import API, auth_headers


@pytest.mark.asyncio
async def test_get_repository_file_by_project_id(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "files-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]

    resp = await client.get(
        f"{API}/projects/{project_id}/repository/files/README.md",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["file_name"] == "README.md"
    assert data["file_path"] == "README.md"
    assert data["encoding"] == "base64"
    assert base64.b64decode(data["content"]).decode() == "# files-project\n"
    assert len(data["blob_id"]) == 40
    assert len(data["commit_id"]) == 40


@pytest.mark.asyncio
async def test_get_repository_file_by_encoded_project_path(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "path-files-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    resp = await client.get(
        f"{API}/projects/testuser%2Fpath-files-project/repository/files/README.md",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json()["file_path"] == "README.md"


@pytest.mark.asyncio
async def test_repository_tree_and_raw_file(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "tree-files-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]
    file_path = quote("docs/tree.txt", safe="")

    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        json={
            "branch": "main",
            "commit_message": "create tree file",
            "content": "tree raw content\n",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201

    root_tree = await client.get(
        f"{API}/projects/{project_id}/repository/tree",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert root_tree.status_code == 200
    assert any(item["type"] == "tree" and item["path"] == "docs" for item in root_tree.json())

    docs_tree = await client.get(
        f"{API}/projects/{project_id}/repository/tree",
        params={"ref": "main", "path": "docs"},
        headers=auth_headers(test_token),
    )
    assert docs_tree.status_code == 200
    assert any(item["type"] == "blob" and item["path"] == "docs/tree.txt" for item in docs_tree.json())

    raw = await client.get(
        f"{API}/projects/{project_id}/repository/files/{file_path}/raw",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert raw.status_code == 200
    assert raw.text == "tree raw content\n"


@pytest.mark.asyncio
async def test_repository_file_commit_creates_push_pipeline(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "file-push-pipeline-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    readme = await client.get(
        f"{API}/projects/{project_id}/repository/files/README.md",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert readme.status_code == 200
    before_sha = readme.json()["commit_id"]

    ci_yaml = """
push_job:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == "main"'
  script:
    - echo file push

api_job:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api"'
  script:
    - echo api
"""
    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/.gitlab-ci.yml",
        json={
            "branch": "main",
            "commit_message": "add ci through files api",
            "content": ci_yaml,
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201

    pipelines = await client.get(
        f"{API}/projects/{project_id}/pipelines",
        headers=auth_headers(test_token),
    )
    assert pipelines.status_code == 200
    pipeline = next(item for item in pipelines.json() if item["source"] == "push")
    assert pipeline["ref"] == "main"
    assert pipeline["sha"] == create.json()["commit_id"]
    assert pipeline["before_sha"] == before_sha

    jobs = await client.get(
        f"{API}/projects/{project_id}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == ["push_job"]


@pytest.mark.asyncio
async def test_create_update_and_delete_repository_file(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "crud-files-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]
    file_path = quote("docs/hello.txt", safe="")

    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        json={
            "branch": "main",
            "commit_message": "create hello",
            "content": "hello from gitlab files api\n",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201
    assert create.json()["file_path"] == "docs/hello.txt"
    assert len(create.json()["commit_id"]) == 40

    get_created = await client.get(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert get_created.status_code == 200
    assert base64.b64decode(get_created.json()["content"]).decode() == (
        "hello from gitlab files api\n"
    )

    update = await client.put(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        json={
            "branch": "main",
            "commit_message": "update hello",
            "encoding": "base64",
            "content": base64.b64encode(b"updated\n").decode(),
        },
        headers=auth_headers(test_token),
    )
    assert update.status_code == 200
    assert update.json()["file_path"] == "docs/hello.txt"

    get_updated = await client.get(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert base64.b64decode(get_updated.json()["content"]).decode() == "updated\n"

    delete = await client.request(
        "DELETE",
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        json={"branch": "main", "commit_message": "delete hello"},
        headers=auth_headers(test_token),
    )
    assert delete.status_code == 200
    assert delete.json()["file_path"] == "docs/hello.txt"

    missing = await client.get(
        f"{API}/projects/{project_id}/repository/files/{file_path}",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_create_repository_file_in_empty_default_branch(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "empty-file-project"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/README.md",
        json={
            "branch": "main",
            "commit_message": "seed empty project",
            "content": "# empty-file-project\n",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201
    assert create.json()["file_path"] == "README.md"

    get_created = await client.get(
        f"{API}/projects/{project_id}/repository/files/README.md",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert get_created.status_code == 200
    assert base64.b64decode(get_created.json()["content"]).decode() == (
        "# empty-file-project\n"
    )


@pytest.mark.asyncio
async def test_create_repository_file_rejects_duplicate(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "duplicate-file-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/repository/files/README.md",
        json={
            "branch": "main",
            "commit_message": "duplicate readme",
            "content": "duplicate\n",
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_repository_file_create_requires_auth(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "auth-file-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/repository/files/new.txt",
        json={
            "branch": "main",
            "commit_message": "new",
            "content": "new\n",
        },
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_repository_file_writes_require_developer_access(
    client, db_session, test_token
):
    from tests.test_projects_api import _create_user_and_token

    reporter, reporter_token = await _create_user_and_token(
        db_session, "file-write-reporter"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "file-write-developer"
    )
    project = await client.post(
        f"{API}/projects",
        json={"name": "role-file-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    for user, level in ((reporter, 20), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied_create = await client.post(
        f"{API}/projects/{project_id}/repository/files/reporter.txt",
        json={
            "branch": "main",
            "commit_message": "reporter create",
            "content": "denied\n",
        },
        headers=auth_headers(reporter_token),
    )
    assert denied_create.status_code == 403

    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/developer.txt",
        json={
            "branch": "main",
            "commit_message": "developer create",
            "content": "created\n",
        },
        headers=auth_headers(developer_token),
    )
    assert create.status_code == 201

    denied_update = await client.put(
        f"{API}/projects/{project_id}/repository/files/developer.txt",
        json={
            "branch": "main",
            "commit_message": "reporter update",
            "content": "denied\n",
        },
        headers=auth_headers(reporter_token),
    )
    assert denied_update.status_code == 403

    update = await client.put(
        f"{API}/projects/{project_id}/repository/files/developer.txt",
        json={
            "branch": "main",
            "commit_message": "developer update",
            "content": "updated\n",
        },
        headers=auth_headers(developer_token),
    )
    assert update.status_code == 200

    denied_delete = await client.request(
        "DELETE",
        f"{API}/projects/{project_id}/repository/files/developer.txt",
        json={"branch": "main", "commit_message": "reporter delete"},
        headers=auth_headers(reporter_token),
    )
    assert denied_delete.status_code == 403

    delete = await client.request(
        "DELETE",
        f"{API}/projects/{project_id}/repository/files/developer.txt",
        json={"branch": "main", "commit_message": "developer delete"},
        headers=auth_headers(developer_token),
    )
    assert delete.status_code == 200


@pytest.mark.asyncio
async def test_repository_file_head_returns_gitlab_metadata_headers(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "head-file-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]

    resp = await client.head(
        f"{API}/projects/{project_id}/repository/files/README.md",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.content == b""
    assert len(resp.headers["x-gitlab-blob-id"]) == 40
    assert len(resp.headers["x-gitlab-commit-id"]) == 40
    assert resp.headers["x-gitlab-file-name"] == "README.md"
    assert resp.headers["x-gitlab-file-path"] == "README.md"
    assert resp.headers["x-gitlab-ref"] == "main"
    assert resp.headers["x-gitlab-encoding"] == "base64"
    assert resp.headers["x-gitlab-execute-filemode"] == "false"


@pytest.mark.asyncio
async def test_repository_tree_paginates_and_supports_recursive_paths(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "tree-pagination-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]
    for name in ["docs/a.txt", "docs/b.txt", "src/app.py"]:
        create = await client.post(
            f"{API}/projects/{project_id}/repository/files/{quote(name, safe='')}",
            json={
                "branch": "main",
                "commit_message": f"create {name}",
                "content": f"{name}\n",
            },
            headers=auth_headers(test_token),
        )
        assert create.status_code == 201

    first_page = await client.get(
        f"{API}/projects/{project_id}/repository/tree",
        params={"ref": "main", "recursive": "true", "page": 1, "per_page": 2},
        headers=auth_headers(test_token),
    )
    assert first_page.status_code == 200
    assert first_page.headers["x-total"] == "4"
    assert first_page.headers["x-per-page"] == "2"
    assert "rel=\"next\"" in first_page.headers["link"]
    assert len(first_page.json()) == 2

    recursive = await client.get(
        f"{API}/projects/{project_id}/repository/tree",
        params={"ref": "main", "recursive": "true", "per_page": 100},
        headers=auth_headers(test_token),
    )
    paths = {item["path"] for item in recursive.json()}
    assert {"README.md", "docs/a.txt", "docs/b.txt", "src/app.py"}.issubset(paths)


@pytest.mark.asyncio
async def test_repository_files_distinguish_missing_ref_and_directory(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "file-edge-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]
    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/{quote('docs/file.txt', safe='')}",
        json={
            "branch": "main",
            "commit_message": "create nested file",
            "content": "nested\n",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201

    missing_ref = await client.get(
        f"{API}/projects/{project_id}/repository/files/README.md",
        params={"ref": "missing-ref"},
        headers=auth_headers(test_token),
    )
    assert missing_ref.status_code == 404
    assert missing_ref.json()["message"] == "404 Reference Not Found"

    directory_as_file = await client.get(
        f"{API}/projects/{project_id}/repository/files/docs",
        params={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert directory_as_file.status_code == 404

    duplicate_directory = await client.post(
        f"{API}/projects/{project_id}/repository/files/docs",
        json={
            "branch": "main",
            "commit_message": "create file where dir exists",
            "content": "bad\n",
        },
        headers=auth_headers(test_token),
    )
    assert duplicate_directory.status_code == 400
    assert "directory" in duplicate_directory.json()["message"]


@pytest.mark.asyncio
async def test_repository_file_create_new_branch_from_start_branch(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "start-branch-file-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]

    create = await client.post(
        f"{API}/projects/{project_id}/repository/files/{quote('feature/new.txt', safe='')}",
        json={
            "branch": "feature/files",
            "start_branch": "main",
            "commit_message": "create file on feature branch",
            "content": "feature branch\n",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201
    assert create.json()["branch"] == "feature/files"
    assert create.json()["file_path"] == "feature/new.txt"

    get_feature = await client.get(
        f"{API}/projects/{project_id}/repository/files/{quote('feature/new.txt', safe='')}",
        params={"ref": quote("feature/files", safe="")},
        headers=auth_headers(test_token),
    )
    assert get_feature.status_code == 200
    assert base64.b64decode(get_feature.json()["content"]).decode() == "feature branch\n"
