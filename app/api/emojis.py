"""Emojis endpoint -- return a dict of emoji name to URL mappings."""

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["emojis"])

BASE = settings.BASE_URL

# A subset of commonly used emojis
_EMOJIS = {
    "+1": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f44d.png?v8",
    "-1": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f44e.png?v8",
    "heart": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/2764.png?v8",
    "tada": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f389.png?v8",
    "rocket": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f680.png?v8",
    "eyes": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f440.png?v8",
    "laugh": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f604.png?v8",
    "confused": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f615.png?v8",
    "hooray": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f389.png?v8",
    "bug": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f41b.png?v8",
    "fire": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f525.png?v8",
    "warning": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/26a0.png?v8",
    "construction": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f6a7.png?v8",
    "memo": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f4dd.png?v8",
    "white_check_mark": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/2705.png?v8",
    "x": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/274c.png?v8",
    "star": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/2b50.png?v8",
    "sparkles": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/2728.png?v8",
    "zap": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/26a1.png?v8",
    "100": "https://gitlab.gitlabassets.com/images/icons/emoji/unicode/1f4af.png?v8",
}


@router.get("/emojis")
async def list_emojis():
    """Return emoji name to URL mappings."""
    return _EMOJIS
