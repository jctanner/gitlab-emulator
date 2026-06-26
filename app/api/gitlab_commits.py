"""GitLab repository commits API."""

import asyncio
import os
import re
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.deps import CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.project import Project

router = APIRouter(tags=["gitlab-commits"])

_COMMIT_FORMAT = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%P%x1f%B%x1e"
_SHORTSTAT_RE = re.compile(
    r"(?:(?P<files>\d+) files? changed)?"
    r"(?:,\s*)?(?:(?P<insertions>\d+) insertions?\(\+\))?"
    r"(?:,\s*)?(?:(?P<deletions>\d+) deletions?\(-\))?"
)


async def _git(repo_path: str, *args: str) -> str:
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
    return stdout.decode()


def _ensure_repo(project: Project) -> str:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    return project.disk_path


def _commit_json(project: Project, record: str) -> dict:
    parts = record.strip("\n\x1e").split("\x1f", 9)
    if len(parts) < 10:
        raise ValueError("invalid commit record")

    sha = parts[0]
    message = parts[9].strip()
    title = message.splitlines()[0] if message else ""
    parent_ids = [parent for parent in parts[8].split() if parent]

    return {
        "id": sha,
        "short_id": sha[:8],
        "created_at": parts[7],
        "parent_ids": parent_ids,
        "title": title,
        "message": message,
        "author_name": parts[2],
        "author_email": parts[3],
        "authored_date": parts[4],
        "committer_name": parts[5],
        "committer_email": parts[6],
        "committed_date": parts[7],
        "trailers": {},
        "extended_trailers": {},
        "web_url": f"{settings.BASE_URL}/{project.full_name}/-/commit/{sha}",
    }


async def _commit_stats(repo_path: str, sha: str) -> dict:
    output = await _git(
        repo_path,
        "diff-tree",
        "--root",
        "--shortstat",
        "--no-commit-id",
        sha,
    )
    match = _SHORTSTAT_RE.search(output.strip())
    if not match:
        return {"additions": 0, "deletions": 0, "total": 0}
    additions = int(match.group("insertions") or 0)
    deletions = int(match.group("deletions") or 0)
    return {
        "additions": additions,
        "deletions": deletions,
        "total": additions + deletions,
    }


async def _resolve_commit(repo_path: str, ref: str) -> str:
    try:
        return (await _git(repo_path, "rev-parse", f"{ref}^{{commit}}")).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 Commit Not Found") from exc


async def _read_commit(project: Project, ref: str, include_stats: bool = False) -> dict:
    repo_path = _ensure_repo(project)
    commit_sha = await _resolve_commit(repo_path, ref)
    try:
        output = await _git(repo_path, "show", "-s", f"--format={_COMMIT_FORMAT}", commit_sha)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 Commit Not Found") from exc

    records = [record for record in output.split("\x1e") if record.strip()]
    if not records:
        raise HTTPException(status_code=404, detail="404 Commit Not Found")
    commit = _commit_json(project, records[0])
    if include_stats:
        commit["stats"] = await _commit_stats(repo_path, commit_sha)
    return commit


def _append_git_log_filters(
    args: list[str],
    *,
    since: str | None,
    until: str | None,
) -> None:
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")


def _diff_entry_from_raw(line: str) -> dict | None:
    if not line.startswith(":"):
        return None
    meta, *paths = line.split("\t")
    meta_parts = meta.split()
    if len(meta_parts) < 5 or not paths:
        return None

    old_mode = meta_parts[0].removeprefix(":")
    new_mode = meta_parts[1]
    status = meta_parts[4]
    if status.startswith("R") and len(paths) >= 2:
        old_path = paths[0]
        new_path = paths[1]
        renamed = True
    else:
        old_path = paths[0]
        new_path = old_path
        renamed = False

    return {
        "old_path": old_path,
        "new_path": new_path,
        "a_mode": old_mode,
        "b_mode": new_mode,
        "diff": "",
        "new_file": status == "A",
        "renamed_file": renamed,
        "deleted_file": status == "D",
    }


@router.get("/projects/{project_ref:path}/repository/commits")
async def list_repository_commits(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    ref_name: str | None = Query(None),
    ref: str | None = Query(None),
    path: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    with_stats: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List repository commits for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    repo_path = _ensure_repo(project)
    target_ref = unquote(ref_name or ref or project.default_branch or "HEAD")
    args = [
        "log",
        f"--format={_COMMIT_FORMAT}",
        f"--skip={(page - 1) * per_page}",
        f"-{per_page}",
    ]
    _append_git_log_filters(args, since=since, until=until)
    args.append(target_ref)
    if path:
        args.extend(["--", unquote(path)])

    try:
        count_args = ["rev-list", "--count"]
        _append_git_log_filters(count_args, since=since, until=until)
        count_args.append(target_ref)
        if path:
            count_args.extend(["--", unquote(path)])
        total = int((await _git(repo_path, *count_args)).strip() or "0")
        output = await _git(repo_path, *args)
    except RuntimeError:
        return paginated_json([], request, page, per_page, 0)

    commits = []
    for record in output.split("\x1e"):
        if not record.strip():
            continue
        try:
            commit = _commit_json(project, record)
            if with_stats:
                commit["stats"] = await _commit_stats(repo_path, commit["id"])
            commits.append(commit)
        except ValueError:
            continue
    return paginated_json(commits, request, page, per_page, total)


@router.get("/projects/{project_ref:path}/repository/commits/{sha}")
async def get_repository_commit(
    project_ref: str,
    sha: str,
    db: DbSession,
    current_user: CurrentUser,
    stats: bool = Query(False),
):
    """Get one repository commit for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    return await _read_commit(project, unquote(sha), include_stats=stats)


@router.get("/projects/{project_ref:path}/repository/commits/{sha}/diff")
async def get_repository_commit_diff(
    project_ref: str,
    sha: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get a minimal GitLab-shaped commit diff."""
    project = await _get_project_or_404(project_ref, db, current_user)
    repo_path = _ensure_repo(project)
    commit_sha = await _resolve_commit(repo_path, unquote(sha))
    try:
        output = await _git(
            repo_path,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--raw",
            "-r",
            "-M",
            commit_sha,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 Commit Not Found") from exc

    diffs = []
    for line in output.splitlines():
        if not line.strip():
            continue
        entry = _diff_entry_from_raw(line)
        if entry is not None:
            diffs.append(entry)
    return diffs
