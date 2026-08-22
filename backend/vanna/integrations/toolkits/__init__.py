"""Adapters that hand this project's tools to other agent frameworks.

    from vanna.integrations.toolkits.langchain import VannaToolkit
    from vanna.integrations.toolkits.pydantic_ai import build_agent

Both are thin wrappers over ``ToolRegistry`` -- see ``bridge.py`` for why they
must be. The framework packages themselves are optional extras, so the two
adapter modules are **not** imported here: doing so would make this package fail
to import for anyone who has neither installed.
"""

from .bridge import ToolSpec, as_json_schema, describe, system_prompt, tool_specs

__all__ = [
    "ToolSpec",
    "tool_specs",
    "system_prompt",
    "as_json_schema",
    "describe",
]
