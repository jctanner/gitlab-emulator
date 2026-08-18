"""GitLab repository files API."""

import asyncio
import base64
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.project import Project
from app.services.branch_protection import require_branch_push_access
from app.services.permissions import DEVELOPER, require_project_access

router = APIRouter(tags=["repository-files"])


async def _git(
    repo_path: str,
    *args: str,
    input_data: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    env = {
        **os.environ,
        "GIT_DIR": repo_path,
        "GIT_AUTHOR_NAME": "GitLab Emulator",
        "GIT_AUTHOR_EMAIL": "emulator@gitlab-emulator.local",
        "GIT_COMMITTER_NAME": "GitLab Emulator",
        "GIT_COMMITTER_EMAIL": "emulator@gitlab-emulator.local",
        **(extra_env or {}),
    }
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate(input=input_data)
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())
    return stdout.decode()


async def _git_bytes(repo_path: str, *args: str) -> bytes:
    env = {**os.environ, "GIT_DIR": repo_path}
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())
    return stdout


def _decode_content(body: dict) -> bytes:
    content = body.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="content is required")
    if body.get("encoding") == "base64":
        try:
            return base64.b64decode(str(content))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid base64 content") from exc
    return str(content).encode()


def _branch_from_body(project: Project, body: dict) -> str:
    return unquote(str(body.get("branch") or project.default_branch)).strip()


def _start_ref_from_body(body: dict) -> str | None:
    value = body.get("start_branch") or body.get("start_sha") or body.get("start_ref")
    if value is None:
        return None
    decoded = unquote(str(value)).strip()
    return decoded or None


def _form_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _repository_commit_body(request: Request) -> dict:
    """Parse JSON or GitLab's form-encoded multi-action commit payload."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object")
        return body

    if "application/x-www-form-urlencoded" not in content_type and "multipart/form-data" not in content_type:
        raise HTTPException(status_code=415, detail="Content-Type must be application/json or form encoded")

    form = await request.form()
    body: dict = {}
    action_fields: dict[str, list] = {}
    action_pattern = re.compile(r"^actions\[\]\[([^]]+)\]$")
    for key, value in form.multi_items():
        match = action_pattern.match(key)
        if match:
            if hasattr(value, "read"):
                value = (await value.read()).decode("utf-8", errors="replace")
            action_fields.setdefault(match.group(1), []).append(value)
        elif key not in body:
            body[key] = value

    action_count = max((len(values) for values in action_fields.values()), default=0)
    body["actions"] = [
        {
            field: values[index]
            for field, values in action_fields.items()
            if index < len(values)
        }
        for index in range(action_count)
    ]
    for key in ("force", "allow_empty"):
        if key in body:
            body[key] = _form_bool(body[key])
    return body


def _normalize_file_path(file_path: str) -> str:
    decoded_path = unquote(file_path).strip("/")
    if (
        not decoded_path
        or decoded_path.endswith("/")
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    ):
        raise HTTPException(status_code=400, detail="file_path is invalid")
    return decoded_path


async def _resolve_commit(project: Project, ref: str, status_code: int = 404) -> str:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        return (await _git(project.disk_path, "rev-parse", f"{ref}^{{commit}}")).strip()
    except RuntimeError as exc:
        detail = "404 Reference Not Found" if status_code == 404 else "branch not found"
        raise HTTPException(status_code=status_code, detail=detail) from exc


async def _branch_exists(project: Project, branch: str) -> bool:
    try:
        await _resolve_commit(project, branch)
    except HTTPException:
        return False
    return True


async def _read_ref_for_change(project: Project, branch: str, start_ref: str | None) -> str:
    if await _branch_exists(project, branch):
        return branch
    return start_ref or branch


async def _path_object_type(project: Project, file_path: str, ref: str) -> str:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        object_id = (await _git(project.disk_path, "rev-parse", f"{ref}:{file_path}")).strip()
        return (await _git(project.disk_path, "cat-file", "-t", object_id)).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 File Not Found") from exc


async def _file_metadata(project: Project, file_path: str, ref: str) -> dict:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        commit_id = await _resolve_commit(project, ref)
        blob_id = (await _git(project.disk_path, "rev-parse", f"{ref}:{file_path}")).strip()
        obj_type = (await _git(project.disk_path, "cat-file", "-t", blob_id)).strip()
        if obj_type != "blob":
            raise RuntimeError("not a blob")
        content = await _git_bytes(project.disk_path, "cat-file", "blob", blob_id)
        last_commit_id = (
            await _git(project.disk_path, "log", "-n", "1", "--format=%H", ref, "--", file_path)
        ).strip() or commit_id
        mode = (
            await _git(project.disk_path, "ls-tree", ref, "--", file_path)
        ).split(maxsplit=1)[0]
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 File Not Found") from exc

    return {
        "file_name": os.path.basename(file_path),
        "file_path": file_path,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "ref": ref,
        "blob_id": blob_id,
        "commit_id": commit_id,
        "last_commit_id": last_commit_id,
        "execute_filemode": mode == "100755",
    }


async def _commit_file_change(
    project: Project,
    branch: str,
    file_path: str,
    message: str,
    content: bytes | None,
    start_ref: str | None = None,
) -> tuple[str, str | None, str | None]:
    repo_path = project.disk_path
    if not repo_path or not os.path.isdir(repo_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")

    parent_sha: str | None
    try:
        parent_sha = await _resolve_commit(project, branch, status_code=400)
    except HTTPException:
        if content is None:
            raise
        if start_ref:
            parent_sha = await _resolve_commit(project, start_ref, status_code=400)
        elif branch == project.default_branch:
            parent_sha = None
        else:
            raise

    blob_sha: str | None = None
    fd, index_path = tempfile.mkstemp(prefix="glemu-index-")
    os.close(fd)
    try:
        index_env = {"GIT_INDEX_FILE": index_path}
        if parent_sha is None:
            await _git(repo_path, "read-tree", "--empty", extra_env=index_env)
        else:
            await _git(repo_path, "read-tree", parent_sha, extra_env=index_env)
        if content is None:
            try:
                await _git(repo_path, "rm", "--cached", "--quiet", "--", file_path, extra_env=index_env)
            except RuntimeError as exc:
                raise HTTPException(status_code=404, detail="404 File Not Found") from exc
        else:
            blob_sha = (
                await _git(repo_path, "hash-object", "-w", "--stdin", input_data=content)
            ).strip()
            await _git(
                repo_path,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob_sha},{file_path}",
                extra_env=index_env,
            )
        tree_sha = (await _git(repo_path, "write-tree", extra_env=index_env)).strip()
    finally:
        if os.path.exists(index_path):
            os.unlink(index_path)

    commit_args = ["commit-tree", tree_sha]
    if parent_sha is not None:
        commit_args.extend(["-p", parent_sha])
    commit_args.extend(["-m", message])
    commit_sha = (await _git(repo_path, *commit_args)).strip()
    await _git(repo_path, "update-ref", f"refs/heads/{branch}", commit_sha)
    project.pushed_at = datetime.now(timezone.utc)
    return commit_sha, blob_sha, parent_sha


async def _index_entries(repo_path: str, index_env: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Read the current temporary index as path -> (mode, blob SHA)."""
    output = await _git(repo_path, "ls-files", "--stage", extra_env=index_env)
    entries: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        metadata, separator, path = line.partition("\t")
        if not separator:
            continue
        mode, blob, _stage = metadata.split(maxsplit=2)
        entries[path] = (mode, blob)
    return entries


def _index_has_descendant(entries: dict[str, tuple[str, str]], path: str) -> bool:
    prefix = f"{path}/"
    return any(entry_path.startswith(prefix) for entry_path in entries)


def _action_path_error(entries: dict[str, tuple[str, str]], path: str) -> str:
    if path in entries:
        return "file"
    if _index_has_descendant(entries, path):
        return "directory"
    return "missing"


async def _create_multi_action_commit(
    project: Project,
    branch: str,
    actions: list,
    message: str,
    start_ref: str | None,
    force: bool,
    author_name: str | None,
    author_email: str | None,
    committer_name: str | None,
    committer_email: str | None,
) -> tuple[str, str | None]:
    """Apply a GitLab actions array to one temporary index and commit once."""
    repo_path = project.disk_path
    if not repo_path or not os.path.isdir(repo_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")

    try:
        await _git(repo_path, "check-ref-format", "--branch", branch)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="branch is invalid") from exc

    branch_exists = True
    try:
        parent_sha = await _resolve_commit(project, branch, status_code=400)
    except HTTPException:
        branch_exists = False
        if start_ref:
            parent_sha = await _resolve_commit(project, start_ref, status_code=400)
        elif branch == project.default_branch:
            parent_sha = None
        else:
            raise HTTPException(
                status_code=400,
                detail="branch does not exist; provide start_branch or start_sha",
            )

    fd, index_path = tempfile.mkstemp(prefix="glemu-commit-index-")
    os.close(fd)
    index_env = {"GIT_INDEX_FILE": index_path}
    try:
        if parent_sha is None:
            await _git(repo_path, "read-tree", "--empty", extra_env=index_env)
        else:
            await _git(repo_path, "read-tree", parent_sha, extra_env=index_env)
        entries = await _index_entries(repo_path, index_env)

        for action_index, raw_action in enumerate(actions):
            if not isinstance(raw_action, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"actions[{action_index}] must be an object",
                )
            action = str(raw_action.get("action") or "").lower()
            if action not in {"create", "update", "delete", "move", "chmod"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"actions[{action_index}].action is invalid",
                )
            if not raw_action.get("file_path"):
                raise HTTPException(
                    status_code=400,
                    detail=f"actions[{action_index}].file_path is required",
                )
            file_path = _normalize_file_path(str(raw_action["file_path"]))
            path_state = _action_path_error(entries, file_path)

            if action == "create":
                if path_state == "file":
                    raise HTTPException(status_code=400, detail=f"A file with this name already exists: {file_path}")
                if path_state == "directory":
                    raise HTTPException(status_code=400, detail=f"A directory with this name already exists: {file_path}")
                content = _decode_content(raw_action)
                mode = "100755" if _form_bool(raw_action.get("execute_filemode", False)) else "100644"
                blob = (await _git(repo_path, "hash-object", "-w", "--stdin", input_data=content)).strip()
                await _git(
                    repo_path,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mode},{blob},{file_path}",
                    extra_env=index_env,
                )
                entries[file_path] = (mode, blob)
                continue

            if action in {"update", "delete", "chmod"} and path_state != "file":
                detail = "directory" if path_state == "directory" else "file"
                raise HTTPException(status_code=404, detail=f"404 {detail.title()} Not Found: {file_path}")

            if action == "delete":
                await _git(repo_path, "rm", "--cached", "--quiet", "--", file_path, extra_env=index_env)
                del entries[file_path]
                continue

            if action == "move":
                previous_path = raw_action.get("previous_path")
                if not previous_path:
                    raise HTTPException(status_code=400, detail="previous_path is required for move actions")
                previous_path = _normalize_file_path(str(previous_path))
                previous_state = _action_path_error(entries, previous_path)
                if previous_state != "file":
                    raise HTTPException(status_code=404, detail=f"404 File Not Found: {previous_path}")
                if path_state != "missing":
                    raise HTTPException(status_code=400, detail=f"A file with this name already exists: {file_path}")
                old_mode, old_blob = entries[previous_path]
                content = (
                    old_blob
                    if raw_action.get("content") is None
                    else (await _git(repo_path, "hash-object", "-w", "--stdin", input_data=_decode_content(raw_action))).strip()
                )
                await _git(repo_path, "rm", "--cached", "--quiet", "--", previous_path, extra_env=index_env)
                await _git(
                    repo_path,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{old_mode},{content},{file_path}",
                    extra_env=index_env,
                )
                del entries[previous_path]
                entries[file_path] = (old_mode, content)
                continue

            # update and chmod both operate on an existing index entry.
            old_mode, old_blob = entries[file_path]
            if action == "update":
                content = _decode_content(raw_action)
                blob = (await _git(repo_path, "hash-object", "-w", "--stdin", input_data=content)).strip()
                mode = old_mode
                if "execute_filemode" in raw_action:
                    mode = "100755" if _form_bool(raw_action["execute_filemode"]) else "100644"
            else:
                if "execute_filemode" not in raw_action:
                    raise HTTPException(status_code=400, detail="execute_filemode is required for chmod actions")
                blob = old_blob
                mode = "100755" if _form_bool(raw_action["execute_filemode"]) else "100644"
            await _git(
                repo_path,
                "update-index",
                "--add",
                "--cacheinfo",
                f"{mode},{blob},{file_path}",
                extra_env=index_env,
            )
            entries[file_path] = (mode, blob)

        tree_sha = (await _git(repo_path, "write-tree", extra_env=index_env)).strip()
    finally:
        if os.path.exists(index_path):
            os.unlink(index_path)

    commit_env = {}
    if author_name:
        commit_env["GIT_AUTHOR_NAME"] = str(author_name)
    if author_email:
        commit_env["GIT_AUTHOR_EMAIL"] = str(author_email)
    if committer_name:
        commit_env["GIT_COMMITTER_NAME"] = str(committer_name)
    if committer_email:
        commit_env["GIT_COMMITTER_EMAIL"] = str(committer_email)
    commit_args = ["commit-tree", tree_sha]
    if parent_sha:
        commit_args.extend(["-p", parent_sha])
    commit_args.extend(["-m", message])
    commit_sha = (await _git(repo_path, *commit_args, extra_env=commit_env)).strip()
    update_ref_args = ["update-ref", f"refs/heads/{branch}", commit_sha]
    if branch_exists and not force and parent_sha:
        update_ref_args.append(parent_sha)
    await _git(repo_path, *update_ref_args)
    project.pushed_at = datetime.now(timezone.utc)
    return commit_sha, parent_sha


async def _create_push_pipeline_for_file_commit(
    project: Project,
    branch: str,
    commit_sha: str,
    before_sha: str | None,
    db: DbSession,
    *,
    actor=None,
) -> None:
    from app.api.pipelines import CreatePipelineRequest, _create_pipeline

    try:
        await _create_pipeline(
            project.id,
            CreatePipelineRequest(ref=branch, sha=commit_sha),
            db,
            source="push",
            actor=actor,
            before_sha=before_sha or "0000000000000000000000000000000000000000",
        )
    except Exception:
        # GitLab accepts repository file commits even when CI config is absent,
        # skipped by workflow rules, or invalid.
        await db.rollback()


def _commit_response(commit_sha: str, message: str) -> dict:
    return {
        "id": commit_sha,
        "short_id": commit_sha[:8],
        "title": message.splitlines()[0] if message else "",
        "message": message,
        "author_name": "GitLab Emulator",
        "author_email": "emulator@gitlab-emulator.local",
        "committer_name": "GitLab Emulator",
        "committer_email": "emulator@gitlab-emulator.local",
    }


def _file_change_response(
    file_path: str,
    branch: str,
    commit_sha: str,
    message: str,
    blob_sha: str | None = None,
) -> dict:
    payload = {
        "file_path": file_path,
        "branch": branch,
        "commit_id": commit_sha,
        "commit": _commit_response(commit_sha, message),
    }
    if blob_sha:
        payload["blob_id"] = blob_sha
    return payload


def _file_headers(metadata: dict) -> dict[str, str]:
    return {
        "X-Gitlab-Blob-Id": metadata["blob_id"],
        "X-Gitlab-Commit-Id": metadata["commit_id"],
        "X-Gitlab-Content-Sha256": metadata["content_sha256"],
        "X-Gitlab-Encoding": metadata["encoding"],
        "X-Gitlab-Execute-Filemode": str(bool(metadata["execute_filemode"])).lower(),
        "X-Gitlab-File-Name": metadata["file_name"],
        "X-Gitlab-File-Path": metadata["file_path"],
        "X-Gitlab-Last-Commit-Id": metadata["last_commit_id"],
        "X-Gitlab-Ref": metadata["ref"],
        "X-Gitlab-Size": str(metadata["size"]),
    }


@router.post("/projects/{project_ref:path}/repository/commits", status_code=201)
async def create_repository_commit(
    project_ref: str,
    request: Request,
    user: AuthUser,
    db: DbSession,
):
    """Create one commit containing GitLab's ordered file actions batch."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    body = await _repository_commit_body(request)

    if not body.get("branch"):
        raise HTTPException(status_code=400, detail="branch is required")
    if not body.get("commit_message"):
        raise HTTPException(status_code=400, detail="commit_message is required")
    actions = body.get("actions", [])
    if not isinstance(actions, list):
        raise HTTPException(status_code=400, detail="actions must be an array")
    allow_empty = _form_bool(body.get("allow_empty", False))
    if not actions and not allow_empty:
        raise HTTPException(status_code=400, detail="actions must not be empty")

    branch = unquote(str(body["branch"])).strip()
    message = str(body["commit_message"])
    await require_branch_push_access(project, branch, user, db)
    start_ref = _start_ref_from_body(body)
    commit_sha, before_sha = await _create_multi_action_commit(
        project,
        branch,
        actions,
        message,
        start_ref,
        _form_bool(body.get("force", False)),
        body.get("author_name"),
        body.get("author_email"),
        body.get("committer_name"),
        body.get("committer_email"),
    )
    # Read the commit while the SQLAlchemy project instance is still live;
    # committing the session expires its scalar attributes.
    from app.api.gitlab_commits import _read_commit

    commit_response = await _read_commit(project, commit_sha, include_stats=True)
    await db.commit()
    await _create_push_pipeline_for_file_commit(
        project,
        branch,
        commit_sha,
        before_sha,
        db,
        actor=user,
    )
    return commit_response


@router.get("/projects/{project_ref:path}/repository/tree")
async def list_repository_tree(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    path: str | None = Query(None),
    ref: str | None = Query(None),
    recursive: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List repository tree entries for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    repo_path = project.disk_path
    if not repo_path or not os.path.isdir(repo_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    target_ref = ref or project.default_branch
    decoded_path = unquote(path or "").strip("/")
    await _resolve_commit(project, target_ref)
    target = f"{target_ref}:{decoded_path}" if decoded_path else target_ref
    args = ["ls-tree", "-z"]
    if recursive:
        args.append("-r")
    args.append(target)
    try:
        output = await _git_bytes(repo_path, *args)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 Tree Not Found") from exc

    entries = []
    for raw in output.split(b"\x00"):
        if not raw:
            continue
        meta, _, name_bytes = raw.partition(b"\t")
        parts = meta.decode().split()
        if len(parts) < 3:
            continue
        mode, obj_type, sha = parts[:3]
        name = name_bytes.decode()
        entry_path = f"{decoded_path}/{name}" if decoded_path else name
        entries.append(
            {
                "id": sha,
                "name": os.path.basename(name),
                "type": "tree" if obj_type == "tree" else "blob",
                "path": entry_path,
                "mode": mode,
            }
        )
    start = (page - 1) * per_page
    return paginated_json(
        entries[start:start + per_page],
        request,
        page,
        per_page,
        len(entries),
    )


@router.get("/projects/{project_ref:path}/repository/files/{file_path:path}/raw")
async def get_repository_file_raw(
    project_ref: str,
    file_path: str,
    db: DbSession,
    current_user: CurrentUser,
    ref: str = Query(...),
):
    """Return raw repository file content."""
    project = await _get_project_or_404(project_ref, db, current_user)
    decoded_path = _normalize_file_path(file_path)
    metadata = await _file_metadata(project, decoded_path, unquote(ref))
    content = base64.b64decode(metadata["content"])
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=_file_headers(metadata),
    )


@router.head("/projects/{project_ref:path}/repository/files/{file_path:path}")
async def head_repository_file(
    project_ref: str,
    file_path: str,
    db: DbSession,
    current_user: CurrentUser,
    ref: str = Query(...),
):
    """Return GitLab file metadata headers without the file body."""
    project = await _get_project_or_404(project_ref, db, current_user)
    metadata = await _file_metadata(project, _normalize_file_path(file_path), unquote(ref))
    return Response(status_code=200, headers=_file_headers(metadata))


@router.get("/projects/{project_ref:path}/repository/files/{file_path:path}")
async def get_repository_file(
    project_ref: str,
    file_path: str,
    db: DbSession,
    current_user: CurrentUser,
    ref: str = Query(...),
):
    """Get a repository file by path."""
    project = await _get_project_or_404(project_ref, db, current_user)
    return await _file_metadata(project, _normalize_file_path(file_path), unquote(ref))


@router.post("/projects/{project_ref:path}/repository/files/{file_path:path}", status_code=201)
async def create_repository_file(
    project_ref: str,
    file_path: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a repository file."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    decoded_path = _normalize_file_path(file_path)
    branch = _branch_from_body(project, body)
    await require_branch_push_access(project, branch, user, db)
    start_ref = _start_ref_from_body(body)
    read_ref = await _read_ref_for_change(project, branch, start_ref)
    message = str(body.get("commit_message") or f"Create {decoded_path}")
    try:
        existing_type = await _path_object_type(project, decoded_path, read_ref)
    except HTTPException:
        pass
    else:
        detail = (
            "A directory with this name already exists"
            if existing_type == "tree"
            else "A file with this name already exists"
        )
        raise HTTPException(status_code=400, detail=detail)

    commit_sha, blob_sha, before_sha = await _commit_file_change(
        project, branch, decoded_path, message, _decode_content(body), start_ref
    )
    await db.commit()
    await _create_push_pipeline_for_file_commit(
        project, branch, commit_sha, before_sha, db, actor=user
    )
    payload = _file_change_response(decoded_path, branch, commit_sha, message, blob_sha)
    return JSONResponse(content=payload, status_code=201)


@router.put("/projects/{project_ref:path}/repository/files/{file_path:path}")
async def update_repository_file(
    project_ref: str,
    file_path: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Update a repository file."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    decoded_path = _normalize_file_path(file_path)
    branch = _branch_from_body(project, body)
    await require_branch_push_access(project, branch, user, db)
    start_ref = _start_ref_from_body(body)
    read_ref = await _read_ref_for_change(project, branch, start_ref)
    message = str(body.get("commit_message") or f"Update {decoded_path}")
    await _file_metadata(project, decoded_path, read_ref)
    commit_sha, blob_sha, before_sha = await _commit_file_change(
        project, branch, decoded_path, message, _decode_content(body), start_ref
    )
    await db.commit()
    await _create_push_pipeline_for_file_commit(
        project, branch, commit_sha, before_sha, db, actor=user
    )
    return _file_change_response(decoded_path, branch, commit_sha, message, blob_sha)


@router.delete("/projects/{project_ref:path}/repository/files/{file_path:path}")
async def delete_repository_file(
    project_ref: str,
    file_path: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Delete a repository file."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    decoded_path = _normalize_file_path(file_path)
    branch = _branch_from_body(project, body)
    await require_branch_push_access(project, branch, user, db)
    start_ref = _start_ref_from_body(body)
    read_ref = await _read_ref_for_change(project, branch, start_ref)
    message = str(body.get("commit_message") or f"Delete {decoded_path}")
    await _file_metadata(project, decoded_path, read_ref)
    commit_sha, _, before_sha = await _commit_file_change(
        project,
        branch,
        decoded_path,
        message,
        None,
        start_ref,
    )
    await db.commit()
    await _create_push_pipeline_for_file_commit(
        project, branch, commit_sha, before_sha, db, actor=user
    )
    return _file_change_response(decoded_path, branch, commit_sha, message)
