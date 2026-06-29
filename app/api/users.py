"""User endpoints -- `/user`, `/users/{username}`, and admin helpers."""

import hashlib
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func as sa_func
from sqlalchemy import or_, select

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.config import settings
from app.models.user import User
from app.models.token import PersonalAccessToken
from app.schemas.user import UserCreate, UserResponse, UserUpdate

router = APIRouter(tags=["users"])

BASE = settings.BASE_URL


# ---- authenticated user ---------------------------------------------------

@router.get("/user")
async def get_authenticated_user(user: AuthUser, db: DbSession):
    """Return the authenticated user's full profile."""
    return UserResponse.from_db(user, BASE)


@router.patch("/user")
async def update_authenticated_user(body: UserUpdate, user: AuthUser, db: DbSession):
    """Update the authenticated user's profile fields."""
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return UserResponse.from_db(user, BASE)


# ---- public user profile --------------------------------------------------

@router.get("/users/{username}")
async def get_user(username: str, db: DbSession):
    """Return a public user profile."""
    if username.isdigit():
        result = await db.execute(select(User).where(User.id == int(username)))
    else:
        result = await db.execute(select(User).where(User.login == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return UserResponse.from_db(user, BASE)


@router.get("/users")
async def list_users(
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    since: int = Query(0, ge=0),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    search: str | None = Query(None),
    username: str | None = Query(None),
):
    """List all users (public, or admin-only in private installs)."""
    query = select(User).where(User.id > since)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(User.login.ilike(pattern), User.name.ilike(pattern)))
    if username:
        query = query.where(User.login == username)
    query = query.order_by(User.id)
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    users = [
        UserResponse.from_db(user, BASE).model_dump()
        for user in result.scalars().all()
    ]
    return paginated_json(
        users,
        request,
        page,
        per_page,
        total,
    )


# ---- admin helpers (not part of real GitLab API) ---------------------------

def _require_site_admin(user: User) -> None:
    if not user.site_admin:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/admin/users", status_code=201)
async def admin_create_user(body: UserCreate, user: AuthUser, db: DbSession):
    """Create a new user (admin bootstrap endpoint).

    This is **not** part of the real GitLab API.  It is provided so that
    automated test-harnesses can seed users without going through the UI.
    """
    _require_site_admin(user)
    # Check uniqueness
    existing = await db.execute(select(User).where(User.login == body.login))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=422, detail="Login already exists")

    hashed = hashlib.sha256(body.password.encode()).hexdigest()

    user = User(
        login=body.login,
        hashed_password=hashed,
        name=body.name,
        email=body.email,
        site_admin=body.site_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.from_db(user, BASE)


@router.post("/admin/tokens", status_code=201)
async def admin_create_token(
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a personal-access token for a user (admin bootstrap endpoint).

    Accepts `{"login": "...", "name": "...", "scopes": [...]}`.
    Returns the **raw token** -- it cannot be retrieved again.
    """
    _require_site_admin(user)
    login = body.get("login")
    token_name = body.get("name", "default")
    scopes = body.get("scopes", [])

    if not login:
        raise HTTPException(status_code=422, detail="login is required")

    result = await db.execute(select(User).where(User.login == login))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    raw_token = f"glpat-{secrets.token_urlsafe(24)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    pat = PersonalAccessToken(
        user_id=user.id,
        name=token_name,
        token_hash=token_hash,
        token_prefix=raw_token[:8],
        scopes=scopes,
    )
    db.add(pat)
    await db.commit()
    await db.refresh(pat)

    return {
        "id": pat.id,
        "token": raw_token,
        "name": pat.name,
        "scopes": pat.scopes,
        "created_at": pat.created_at.isoformat() + "Z" if pat.created_at else None,
    }
