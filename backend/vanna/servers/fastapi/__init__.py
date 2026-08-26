"""
FastAPI route registrars for Vanna Agents.

Routes only: they are registered onto an application built elsewhere. There is no
server factory here -- `vanna_app.wiring` owns application construction, and a
second one that built its own app from the environment was never used.
"""

from .admin_routes import (
    DEFAULT_ADMIN_GROUPS,
    FeedbackPayload,
    InstructionPayload,
    register_admin_routes,
)

__all__ = [
    "register_admin_routes",
    "FeedbackPayload",
    "InstructionPayload",
    "DEFAULT_ADMIN_GROUPS",
]
