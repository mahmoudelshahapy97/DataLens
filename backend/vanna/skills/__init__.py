"""Agent skills: workflow guides that ship with the package.

    from vanna.skills import get_skill, list_skills

Content lives in the wheel and is served on demand, so an agent client holds a
small stub instead of a copy that drifts out of date.
"""

from .delivery import SkillSummary, get_skill, list_skills, read_reference
from .stub import render_discovery_stub

__all__ = [
    "list_skills",
    "get_skill",
    "read_reference",
    "SkillSummary",
    "render_discovery_stub",
]
