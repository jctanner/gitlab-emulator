"""Git Smart HTTP protocol handler.

Implements the three endpoints needed for git clone/push/pull over HTTP:
- GET /{owner}/{repo}.git/info/refs?service=git-upload-pack|git-receive-pack
- POST /{owner}/{repo}.git/git-upload-pack
- POST /{owner}/{repo}.git/git-receive-pack

Also supports URLs without .git suffix.

Reference: https://git-scm.com/docs/http-protocol
"""

import asyncio
import base64
import os
import shlex
import stat

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.branch import Branch
from app.models.ci import PipelineJob
from app.models.repository import Repository
from app.models.user import User
from app.api.deps import get_current_user
from app.git.bare_repo import get_branches as get_disk_branches
from app.git.bare_repo import get_tags as get_disk_tags
from app.services.permissions import DEVELOPER, REPORTER, project_access_level

router = APIRouter()
ZERO_SHA = "0000000000000000000000000000000000000000"


class ProtectedBranchPushError(Exception):
    def __init__(self, ref: str, branch_name: str, reason: str) -> None:
        self.ref = ref
        self.branch_name = branch_name
        self.reason = reason
        self.message = (
            f"GitLab: You are not allowed to {reason} "
            f"protected branch '{branch_name}'"
        )
        super().__init__(self.message)


def pkt_line(data: str) -> bytes:
    """Encode a string as a pkt-line (4-byte hex length prefix + data)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    length = len(data) + 4
    return f"{length:04x}".encode("ascii") + data


def pkt_flush() -> bytes:
    """Return a flush packet (0000)."""
    return b"0000"


async def _resolve_repo(
    db: AsyncSession, owner: str, repo: str
) -> Repository:
    """Resolve owner/repo to a Repository, stripping .git suffix if present."""
    if repo.endswith(".git"):
        repo = repo[:-4]
    result = await db.execute(
        select(Repository).where(Repository.full_name == f"{owner}/{repo}")
    )
    repository = result.scalar_one_or_none()
    if repository is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


async def _check_read_access(
    db: AsyncSession,
    repository: Repository,
    user: User | None,
    request: Request,
) -> None:
    """Check if user has read access to the repository."""
    if not repository.private:
        return
    token = _auth_token_from_request(request)
    if await _job_token_can_read(db, repository, request):
        return
    if user is not None and await project_access_level(repository, user, db) >= REPORTER:
        return
    if user is None and token is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="GitLab Emulator"'},
        )
    raise HTTPException(status_code=404, detail="Repository not found")


async def _check_write_access(
    db: AsyncSession,
    repository: Repository,
    user: User | None,
) -> None:
    """Check if user has write access to the repository."""
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="GitLab Emulator"'},
        )
    if await project_access_level(repository, user, db) < DEVELOPER:
        raise HTTPException(status_code=403, detail="Permission denied")


def _auth_token_from_request(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None

    parts = auth_header.split(" ", 1)
    if len(parts) != 2:
        return None

    scheme, credentials = parts[0].lower(), parts[1]
    if scheme in {"token", "bearer"}:
        return credentials

    if scheme == "basic":
        try:
            decoded = base64.b64decode(credentials).decode("utf-8")
        except Exception:
            return None
        _username, separator, password = decoded.partition(":")
        return password if separator else None

    return None


async def _job_token_can_read(
    db: AsyncSession,
    repository: Repository,
    request: Request,
) -> bool:
    token = _auth_token_from_request(request)
    if not token:
        return False
    result = await db.execute(
        select(PipelineJob.id)
        .where(
            PipelineJob.project_id == repository.id,
            PipelineJob.job_token == token,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _run_git_command(
    args: list[str],
    repo_path: str,
    input_data: bytes | None = None,
) -> tuple[bytes, bytes]:
    """Run a git command and return (stdout, stderr)."""
    env = os.environ.copy()
    env["GIT_DIR"] = repo_path

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate(input=input_data)
    return stdout, stderr


async def _run_git_command_with_status(
    args: list[str],
    repo_path: str,
    input_data: bytes | None = None,
) -> tuple[bytes, bytes, int]:
    env = os.environ.copy()
    env["GIT_DIR"] = repo_path

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate(input=input_data)
    return stdout, stderr, int(proc.returncode or 0)


def _parse_receive_pack_commands(input_data: bytes) -> list[tuple[str, str, str]]:
    """Return `(old_sha, new_sha, ref)` commands from a receive-pack request."""
    commands: list[tuple[str, str, str]] = []
    offset = 0
    while offset + 4 <= len(input_data):
        header = input_data[offset : offset + 4]
        offset += 4
        if header == b"0000":
            break
        try:
            pkt_len = int(header.decode("ascii"), 16)
        except ValueError:
            break
        if pkt_len < 4:
            break
        payload_len = pkt_len - 4
        payload = input_data[offset : offset + payload_len]
        offset += payload_len
        command = payload.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
        parts = command.split()
        if len(parts) >= 3:
            commands.append((parts[0], parts[1], parts[2]))
    return commands


def _minimum_push_access_level(branch: Branch) -> int:
    restrictions = branch.protection.restrictions if branch.protection else {}
    entries = (restrictions or {}).get("push_access_levels") or [{"access_level": 40}]
    levels = [
        int(entry.get("access_level", 40))
        for entry in entries
        if isinstance(entry, dict) and int(entry.get("access_level", 40)) > 0
    ]
    return min(levels, default=40)


def _protected_branch_rejection(
    ref: str,
    branch_name: str,
    reason: str,
) -> ProtectedBranchPushError:
    return ProtectedBranchPushError(ref, branch_name, reason)


def _receive_pack_rejection(error: ProtectedBranchPushError) -> bytes:
    status_report = (
        pkt_line("unpack ok\n")
        + pkt_line(f"ng {error.ref} {error.message}\n")
        + pkt_flush()
    )
    return pkt_line(b"\1" + status_report) + pkt_flush()


async def _check_protected_branch_updates(
    db: AsyncSession,
    repository: Repository,
    user: User,
    input_data: bytes,
) -> None:
    """Reject receive-pack commands that mutate protected branches illegally."""
    commands = [
        (old_sha, new_sha, ref)
        for old_sha, new_sha, ref in _parse_receive_pack_commands(input_data)
        if ref.startswith("refs/heads/")
    ]
    if not commands:
        return

    branch_names = {ref.removeprefix("refs/heads/") for _old, _new, ref in commands}
    result = await db.execute(
        select(Branch)
        .options(selectinload(Branch.protection))
        .where(
            Branch.repo_id == repository.id,
            Branch.name.in_(branch_names),
            Branch.protected.is_(True),
        )
    )
    protected = {branch.name: branch for branch in result.scalars().all()}
    if not protected:
        return

    access_level = await project_access_level(repository, user, db)
    for old_sha, new_sha, ref in commands:
        branch_name = ref.removeprefix("refs/heads/")
        branch = protected.get(branch_name)
        if branch is None:
            continue
        if access_level < _minimum_push_access_level(branch):
            raise _protected_branch_rejection(ref, branch_name, "push to")
        if new_sha == ZERO_SHA:
            raise _protected_branch_rejection(ref, branch_name, "delete")


async def _protected_branch_hook_script(
    db: AsyncSession,
    repository: Repository,
    user: User,
) -> str | None:
    result = await db.execute(
        select(Branch)
        .options(selectinload(Branch.protection))
        .where(Branch.repo_id == repository.id, Branch.protected.is_(True))
    )
    branches = result.scalars().all()
    if not branches:
        return None
    access_level = await project_access_level(repository, user, db)
    lines = [
        "#!/bin/sh",
        "zero='0000000000000000000000000000000000000000'",
        "while read old new ref; do",
        "  case \"$ref\" in",
    ]
    for branch in branches:
        ref = f"refs/heads/{branch.name}"
        minimum = _minimum_push_access_level(branch)
        restrictions = branch.protection.restrictions if branch.protection else {}
        allow_force = "1" if bool((restrictions or {}).get("allow_force_push")) else "0"
        lines.extend(
            [
                f"    {shlex.quote(ref)})",
                f"      if [ {access_level} -lt {minimum} ]; then",
                "        echo "
                + shlex.quote(
                    f"GitLab: You are not allowed to push to protected branch "
                    f"'{branch.name}'"
                )
                + " >&2",
                "        exit 1",
                "      fi",
                '      if [ "$new" = "$zero" ]; then',
                "        echo "
                + shlex.quote(
                    f"GitLab: You are not allowed to delete protected branch "
                    f"'{branch.name}'"
                )
                + " >&2",
                "        exit 1",
                "      fi",
                f"      if [ {allow_force} -ne 1 ] "
                + '&& [ "$old" != "$zero" ] '
                + '&& ! git merge-base --is-ancestor "$old" "$new"; then',
                "        echo "
                + shlex.quote(
                    f"GitLab: You are not allowed to force push to protected branch "
                    f"'{branch.name}'"
                )
                + " >&2",
                "        exit 1",
                "      fi",
                "      ;;",
            ]
        )
    lines.extend(["  esac", "done", "exit 0", ""])
    return "\n".join(lines)


async def _install_request_pre_receive_hook(
    db: AsyncSession,
    repository: Repository,
    user: User,
) -> tuple[str, bytes | None, int | None] | None:
    script = await _protected_branch_hook_script(db, repository, user)
    if script is None:
        return None
    hook_path = os.path.join(repository.disk_path, "hooks", "pre-receive")
    previous_content: bytes | None = None
    previous_mode: int | None = None
    if os.path.exists(hook_path):
        with open(hook_path, "rb") as existing:
            previous_content = existing.read()
        previous_mode = stat.S_IMODE(os.stat(hook_path).st_mode)
    with open(hook_path, "w", encoding="utf-8") as hook:
        hook.write(script)
    os.chmod(hook_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
    return hook_path, previous_content, previous_mode


def _restore_pre_receive_hook(
    hook_state: tuple[str, bytes | None, int | None] | None,
) -> None:
    if hook_state is None:
        return
    hook_path, previous_content, previous_mode = hook_state
    if previous_content is None:
        try:
            os.remove(hook_path)
        except FileNotFoundError:
            pass
        return
    with open(hook_path, "wb") as hook:
        hook.write(previous_content)
    if previous_mode is not None:
        os.chmod(hook_path, previous_mode)


async def _sync_branches_to_db(
    db: AsyncSession, repository: Repository
) -> None:
    """Sync branch refs from the on-disk bare repo into the branches table."""
    disk_branches = await get_disk_branches(repository.disk_path)
    disk_map = {b["name"]: b["sha"] for b in disk_branches}

    # Fetch existing DB branches for this repo
    result = await db.execute(
        select(Branch).where(Branch.repo_id == repository.id)
    )
    existing = {b.name: b for b in result.scalars().all()}

    # Update or insert branches that exist on disk
    for name, sha in disk_map.items():
        if name in existing:
            if existing[name].sha != sha:
                existing[name].sha = sha
        else:
            db.add(Branch(repo_id=repository.id, name=name, sha=sha))

    # Delete branches that no longer exist on disk
    for name, branch in existing.items():
        if name not in disk_map:
            await db.delete(branch)

    await db.commit()


async def _create_push_pipelines(
    db: AsyncSession,
    repository: Repository,
    user: User | None,
    before_refs: dict[str, dict[str, str]],
) -> None:
    """Create source=push pipelines for refs changed by a successful push."""
    from app.api.pipelines import CreatePipelineRequest, _create_pipeline

    before_branches = before_refs.get("branches", {})
    after_branches = await get_disk_branches(repository.disk_path)
    after_map = {branch["name"]: branch["sha"] for branch in after_branches}
    changed_branches = [
        (name, sha)
        for name, sha in sorted(after_map.items())
        if before_branches.get(name) != sha
    ]
    for branch_name, sha in changed_branches:
        try:
            await _create_pipeline(
                repository.id,
                CreatePipelineRequest(ref=branch_name, sha=sha),
                db,
                source="push",
                actor=user,
                before_sha=before_branches.get(
                    branch_name,
                    "0000000000000000000000000000000000000000",
                ),
            )
        except Exception:
            # GitLab accepts pushes even when no pipeline is created because
            # CI config is absent, workflow rules skip, or CI syntax is invalid.
            await db.rollback()

    before_tags = before_refs.get("tags", {})
    after_tags = await get_disk_tags(repository.disk_path)
    after_tag_map = {tag["name"]: tag["sha"] for tag in after_tags}
    changed_tags = [
        tag_name
        for tag_name, sha in sorted(after_tag_map.items())
        if before_tags.get(tag_name) != sha
    ]
    for tag_name in changed_tags:
        try:
            await _create_pipeline(
                repository.id,
                CreatePipelineRequest(ref=tag_name),
                db,
                source="push",
                actor=user,
                before_sha=before_tags.get(tag_name, ZERO_SHA),
            )
        except Exception:
            # CI config may be absent or skip tag pipelines; the push still
            # succeeds, matching GitLab's behavior.
            await db.rollback()


async def _push_ref_snapshot(repo_path: str) -> dict[str, dict[str, str]]:
    branches = await get_disk_branches(repo_path)
    tags = await get_disk_tags(repo_path)
    return {
        "branches": {branch["name"]: branch["sha"] for branch in branches},
        "tags": {tag["name"]: tag["sha"] for tag in tags},
    }


async def _stream_git_command(
    args: list[str],
    repo_path: str,
    input_data: bytes,
):
    """Run a git command and stream its output."""
    env = os.environ.copy()
    env["GIT_DIR"] = repo_path

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    # Write input and close stdin
    proc.stdin.write(input_data)
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()

    # Stream stdout
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        yield chunk

    await proc.wait()


@router.get("/{owner}/{repo_name}/info/refs")
@router.get("/{owner}/{repo_name}.git/info/refs")
async def info_refs(
    owner: str,
    repo_name: str,
    service: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Git Smart HTTP reference discovery.

    Returns the list of refs in the repository for clone/fetch/push operations.
    """
    if service not in ("git-upload-pack", "git-receive-pack"):
        raise HTTPException(status_code=403, detail="Invalid service")

    repository = await _resolve_repo(db, owner, repo_name)

    # Check access
    if service == "git-receive-pack":
        await _check_write_access(db, repository, user)
    else:
        await _check_read_access(db, repository, user, request)

    repo_path = repository.disk_path
    if not os.path.isdir(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found on disk")

    # Run git service with --advertise-refs
    stdout, stderr = await _run_git_command(
        [service, "--stateless-rpc", "--advertise-refs", repo_path],
        repo_path,
    )

    # Build response: service announcement + refs
    body = pkt_line(f"# service={service}\n") + pkt_flush() + stdout

    return Response(
        content=body,
        media_type=f"application/x-{service}-advertisement",
        headers={
            "Cache-Control": "no-cache",
            "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
            "Pragma": "no-cache",
        },
    )


@router.post("/{owner}/{repo_name}/git-upload-pack")
@router.post("/{owner}/{repo_name}.git/git-upload-pack")
async def git_upload_pack(
    owner: str,
    repo_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Git Smart HTTP upload-pack (fetch/clone).

    Pipes request body to git-upload-pack and streams the response.
    """
    repository = await _resolve_repo(db, owner, repo_name)
    await _check_read_access(db, repository, user, request)

    repo_path = repository.disk_path
    if not os.path.isdir(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found on disk")

    input_data = await request.body()

    return StreamingResponse(
        _stream_git_command(
            ["git-upload-pack", "--stateless-rpc", repo_path],
            repo_path,
            input_data,
        ),
        media_type="application/x-git-upload-pack-result",
        headers={
            "Cache-Control": "no-cache",
            "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
            "Pragma": "no-cache",
        },
    )


@router.post("/{owner}/{repo_name}/git-receive-pack")
@router.post("/{owner}/{repo_name}.git/git-receive-pack")
async def git_receive_pack(
    owner: str,
    repo_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Git Smart HTTP receive-pack (push).

    Pipes request body to git-receive-pack and streams the response.
    Requires authentication with write access.
    """
    repository = await _resolve_repo(db, owner, repo_name)
    await _check_write_access(db, repository, user)

    repo_path = repository.disk_path
    if not os.path.isdir(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found on disk")

    input_data = await request.body()
    try:
        await _check_protected_branch_updates(db, repository, user, input_data)
    except ProtectedBranchPushError as exc:
        return Response(
            content=_receive_pack_rejection(exc),
            media_type="application/x-git-receive-pack-result",
            headers={
                "Cache-Control": "no-cache",
                "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
                "Pragma": "no-cache",
            },
        )
    before_map = await _push_ref_snapshot(repo_path)

    hook_state = await _install_request_pre_receive_hook(db, repository, user)
    try:
        stdout, stderr, return_code = await _run_git_command_with_status(
            ["git-receive-pack", "--stateless-rpc", repo_path],
            repo_path,
            input_data,
        )
    finally:
        _restore_pre_receive_hook(hook_state)

    if return_code != 0:
        return Response(
            content=stdout,
            media_type="application/x-git-receive-pack-result",
            headers={
                "Cache-Control": "no-cache",
                "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
                "Pragma": "no-cache",
            },
        )

    # Update pushed_at timestamp
    from datetime import datetime, timezone

    repository.pushed_at = datetime.now(timezone.utc)
    await db.commit()

    # Sync branch refs from disk into the database
    try:
        await _sync_branches_to_db(db, repository)
    except Exception:
        pass  # Don't fail the push if branch sync fails

    try:
        await _create_push_pipelines(db, repository, user, before_map)
    except Exception:
        pass  # Don't fail the push if pipeline creation fails

    # Trigger search indexing in the background
    try:
        from app.services.index_service import index_repository
        await index_repository(db, repository)
    except Exception:
        pass  # Don't fail the push if indexing fails

    return Response(
        content=stdout,
        media_type="application/x-git-receive-pack-result",
        headers={
            "Cache-Control": "no-cache",
            "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
            "Pragma": "no-cache",
        },
    )
