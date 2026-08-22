"""Project directories: layout, config, scaffolding.

A project is a directory holding what a deployment declares about its data --
semantic models, relationships, curated knowledge -- in files that live in git
and are reviewed like code.
"""

from .init import created_paths, init_project
from .layout import PROJECT_FILE, ProjectPaths
from .loader import SCHEMA_VERSION, Project, ProjectConfig

__all__ = [
    "Project",
    "ProjectConfig",
    "ProjectPaths",
    "PROJECT_FILE",
    "SCHEMA_VERSION",
    "init_project",
    "created_paths",
]
