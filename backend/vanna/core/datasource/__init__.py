"""What it takes to connect to a database.

    from vanna.core.datasource import ENGINES, get_engine, engine_for_url

A single description of every supported engine's connection fields, shared by
the admin console's form, the CLI reference, and runner construction -- see
``connections.py`` for why that is one place and not three.
"""

from .connections import (
    ENGINES,
    Engine,
    Field,
    all_engines,
    describe,
    engine_for_url,
    get_engine,
)

__all__ = [
    "Engine",
    "Field",
    "ENGINES",
    "get_engine",
    "engine_for_url",
    "describe",
    "all_engines",
]
