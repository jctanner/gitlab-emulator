"""Commit endpoints -- list, get, and compare commits via bare repo."""

import asyncio
import os

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, get_repo_or_404
from app.config import settings
from app.schemas.user import _fmt_dt, _make_node_id

router = APIRouter(tags=["commits"])

BASE = settings.BASE_URL


async def _git(repo_path: str, *args: str) -> str:
    """Run a git command in the bare repo and return stdout."""
    env = {**os.environ, "GIT_DIR": repo_path}
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())
    return stdout.decode()


def _safe_int(value: str) -> int:
    return int(value) if value.isdigit() else 0


def _parse_commit_line(line: str, owner: str, repo_name: str, base_url: str) -> dict:
    """Parse a `git log --format` line into a commit dict."""
    api = f"{base_url}/api/v4"
    parts = line.split("\x1f")
    sha = parts[0] if len(parts) > 0 else ""
    author_name = parts[1] if len(parts) > 1 else ""
    author_email = parts[2] if len(parts) > 2 else ""
    author_date = parts[3] if len(parts) > 3 else ""
    committer_name = parts[4] if len(parts) > 4 else ""
    committer_email = parts[5] if len(parts) > 5 else ""
    committer_date = parts[6] if len(parts) > 6 else ""
    message = parts[7] if len(parts) > 7 else ""
    parents_raw = parts[8] if len(parts) > 8 else ""
    tree_sha = parts[9] if len(parts) > 9 else ""

    parents = [
        {"sha": p, "url": f"{api}/repos/{owner}/{repo_name}/commits/{p}",
         "html_url": f"{base_url}/{owner}/{repo_name}/commit/{p}"}
        for p in parents_raw.split() if p
    ]

    return {
        "sha": sha,
        "node_id": _make_node_id("Commit", hash(sha) % 10**8),
        "commit": {
            "author": {"name": author_name, "email": author_email, "date": author_date},
            "committer": {"name": committer_name, "email": committer_email, "date": committer_date},
            "message": message,
            "tree": {"sha": tree_sha, "url": f"{api}/repos/{owner}/{repo_name}/git/trees/{tree_sha}"},
            "url": f"{api}/repos/{owner}/{repo_name}/git/commits/{sha}",
            "comment_count": 0,
            "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None},
        },
        "url": f"{api}/repos/{owner}/{repo_name}/commits/{sha}",
        "html_url": f"{base_url}/{owner}/{repo_name}/commit/{sha}",
        "comments_url": f"{api}/repos/{owner}/{repo_name}/commits/{sha}/comments",
        "author": None,
        "committer": None,
        "parents": parents,
    }


@router.get("/repos/{owner}/{repo}/commits")
async def list_commits(
    owner: str,
    repo: str,
    db: DbSession,
    current_user: CurrentUser,
    sha: str | None = Query(None),
    path: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List commits for a repository (reads from bare repo via git log)."""
    repository = await get_repo_or_404(owner, repo, db)

    if not repository.disk_path or not os.path.isdir(repository.disk_path):
        return []

    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1f%P%x1f%T"
    args = ["log", f"--format={fmt}", f"--skip={( page - 1) * per_page}", f"-{per_page}"]

    if sha:
        args.append(sha)

    if path:
        args.extend(["--", path])

    try:
        out = await _git(repository.disk_path, *args)
    except RuntimeError:
        return []

    commits = []
    for line in out.strip().splitlines():
        if line:
            commits.append(_parse_commit_line(line, owner, repo, BASE))

    return commits


@router.get("/repos/{owner}/{repo}/commits/{sha}")
async def get_commit(
    owner: str, repo: str, sha: str, db: DbSession, current_user: CurrentUser
):
    """Get a single commit."""
    repository = await get_repo_or_404(owner, repo, db)

    if not repository.disk_path or not os.path.isdir(repository.disk_path):
        raise HTTPException(status_code=404, detail="Not Found")

    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1f%P%x1f%T"
    try:
        out = await _git(repository.disk_path, "log", f"--format={fmt}", "-1", sha)
    except RuntimeError:
        raise HTTPException(status_code=404, detail="Not Found")

    line = out.strip()
    if not line:
        raise HTTPException(status_code=404, detail="Not Found")

    return _parse_commit_line(line, owner, repo, BASE)


@router.get("/repos/{owner}/{repo}/compare/{basehead}")
async def compare_commits(
    owner: str, repo: str, basehead: str, db: DbSession, current_user: CurrentUser
):
    """Compare two commits or refs."""
    repository = await get_repo_or_404(owner, repo, db)

    if "..." not in basehead:
        raise HTTPException(status_code=422, detail="basehead must be base...head")

    base_ref, _, head_ref = basehead.partition("...")
    api = f"{BASE}/api/v4"

    if not repository.disk_path or not os.path.isdir(repository.disk_path):
        raise HTTPException(status_code=404, detail="Not Found")

    try:
        base_sha = (await _git(repository.disk_path, "rev-parse", base_ref)).strip()
        head_sha = (await _git(repository.disk_path, "rev-parse", head_ref)).strip()
        merge_base_sha = (
            await _git(repository.disk_path, "merge-base", base_sha, head_sha)
        ).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc

    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1f%P%x1f%T"

    async def read_commit(sha: str) -> dict:
        out = await _git(repository.disk_path, "log", f"--format={fmt}", "-1", sha)
        line = out.strip()
        if not line:
            raise RuntimeError("commit not found")
        return _parse_commit_line(line, owner, repo, BASE)

    base_commit = await read_commit(base_sha)
    merge_base_commit = await read_commit(merge_base_sha)
    commit_lines = await _git(
        repository.disk_path,
        "log",
        "--reverse",
        f"--format={fmt}",
        f"{base_sha}..{head_sha}",
    )
    commits = [
        _parse_commit_line(line, owner, repo, BASE)
        for line in commit_lines.strip().splitlines()
        if line
    ]

    ahead_behind = (
        await _git(
            repository.disk_path,
            "rev-list",
            "--left-right",
            "--count",
            f"{base_sha}...{head_sha}",
        )
    ).strip()
    behind_raw, _, ahead_raw = ahead_behind.partition("\t")
    if not ahead_raw:
        behind_raw, _, ahead_raw = ahead_behind.partition(" ")

    status_by_path: dict[str, tuple[str, str | None]] = {}
    try:
        status_output = await _git(
            repository.disk_path,
            "diff",
            "--name-status",
            "-M",
            base_sha,
            head_sha,
        )
    except RuntimeError:
        status_output = ""
    for line in status_output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            status_by_path[parts[2]] = ("renamed", parts[1])
        elif status == "A":
            status_by_path[parts[1]] = ("added", None)
        elif status == "D":
            status_by_path[parts[1]] = ("removed", None)
        else:
            status_by_path[parts[1]] = ("modified", None)

    files = []
    try:
        numstat = await _git(repository.disk_path, "diff", "--numstat", base_sha, head_sha)
    except RuntimeError:
        numstat = ""
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        filename = parts[-1]
        additions = _safe_int(parts[0])
        deletions = _safe_int(parts[1])
        status, previous_filename = status_by_path.get(filename, ("modified", None))
        file_json = {
            "sha": head_sha,
            "filename": filename,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
            "blob_url": f"{BASE}/{owner}/{repo}/blob/{head_sha}/{filename}",
            "raw_url": f"{BASE}/{owner}/{repo}/raw/{head_sha}/{filename}",
            "contents_url": f"{api}/repos/{owner}/{repo}/contents/{filename}?ref={head_sha}",
        }
        if previous_filename:
            file_json["previous_filename"] = previous_filename
        files.append(file_json)

    return {
        "url": f"{api}/repos/{owner}/{repo}/compare/{basehead}",
        "html_url": f"{BASE}/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        "permalink_url": f"{BASE}/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        "diff_url": f"{BASE}/{owner}/{repo}/compare/{base_sha}...{head_sha}.diff",
        "patch_url": f"{BASE}/{owner}/{repo}/compare/{base_sha}...{head_sha}.patch",
        "base_commit": base_commit,
        "merge_base_commit": merge_base_commit,
        "status": "identical" if base_sha == head_sha else "ahead",
        "ahead_by": _safe_int(ahead_raw),
        "behind_by": _safe_int(behind_raw),
        "total_commits": len(commits),
        "commits": commits,
        "files": files,
    }
