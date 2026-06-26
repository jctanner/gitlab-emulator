"""GitLab-facing group model alias.

The underlying table is still named ``organizations`` for compatibility with
the inherited GitHub emulator routes. GitLab-facing API code should import the
model through this module.
"""

from app.models.organization import Organization as Group

__all__ = ["Group"]
