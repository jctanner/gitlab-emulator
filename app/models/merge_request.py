"""GitLab-facing merge request model alias.

The underlying table is still named ``pull_requests`` for compatibility with
the inherited GitHub emulator routes. GitLab-facing API code should import the
model through this module so external GitLab behavior is not coupled to the
legacy model name.
"""

from app.models.pull_request import PullRequest as MergeRequest

__all__ = ["MergeRequest"]
