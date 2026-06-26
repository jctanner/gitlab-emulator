"""GitLab-facing project model alias.

The underlying table is still named ``repositories`` for compatibility with
the inherited GitHub emulator routes. GitLab-facing API code should import the
model through this module.
"""

from app.models.repository import Repository as Project

__all__ = ["Project"]
