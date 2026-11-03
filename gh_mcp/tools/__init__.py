"""Tool assembly."""

from typing import Final

from . import actions_ci, advisories, feedback, issues, pulls, raw, repos
from ._base import GATE, Gate

__all__ = ("ALL_TOOLS", "GATE", "Gate")

ALL_TOOLS: Final = (
    *repos.TOOLS,
    *issues.TOOLS,
    *pulls.TOOLS,
    *actions_ci.TOOLS,
    *advisories.TOOLS,
    *raw.TOOLS,
    *feedback.TOOLS,
)
