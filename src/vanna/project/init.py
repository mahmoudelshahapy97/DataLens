"""Scaffolding a new project.

Two modes, and the distinction matters more than it looks:

* **with placeholders** (default) -- a worked example of every file type, so a
  human opening the directory can see the shape of a model, a relationship and
  a rule without reading documentation.
* **empty** (``--empty``) -- directories and nothing else. This is what an agent
  should use, because a scaffolded ``example`` model is something it will
  faithfully carry forward into a real project and nobody will notice until the
  compiler complains about a table that does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..core.errors import ErrorCode, ErrorPhase, VannaError
from .layout import (
    KNOWLEDGE_RULES_DIR,
    KNOWLEDGE_SQL_DIR,
    MODEL_METADATA,
    ProjectPaths,
)
from .loader import Project, ProjectConfig

_GITIGNORE = """\
# Build output and derived caches: reproducible from the sources, and a
# checked-in manifest eventually disagrees with the YAML it came from.
target/
.vanna/

# Credentials never belong in the repository.
.env
"""

_RELATIONSHIPS_STUB = """\
# How models join. `condition` is SQL over model (not table) names, so it can
# express composite keys that a foreign-key scan cannot.
#
# relationships:
#   - name: orders_customer
#     models: [orders, customers]
#     join_type: MANY_TO_ONE
#     condition: orders.customer_id = customers.id

relationships: []
"""

_EXAMPLE_MODEL = """\
# A semantic model: what the business calls this data, mapped onto what the
# database actually stores.
name: example
description: Replace this with a real table. `vanna project from-catalog` writes
  one of these per scanned table, which is usually a better starting point than
  editing this by hand.
table_reference: example_table
columns:
  - name: id
    type: INTEGER
    is_primary_key: true
  - name: amount_cents
    type: INTEGER
    description: Stored in cents.
  # A calculated column: an expression over other columns of this model, given
  # a business name. This is the point of a semantic layer -- nobody should
  # have to remember the /100.
  - name: amount
    type: DOUBLE
    is_calculated: true
    expression: amount_cents / 100.0
"""

_EXAMPLE_RULE = """\
---
title: General conventions
scope: global
priority: 10
---

Rules stated here are shown to the model on every question. Keep them few and
true; a rule that is only sometimes right teaches the model to ignore rules.

- Monetary columns ending in `_cents` store integer cents. Divide by 100.0.
- Exclude rows where `is_test` is true from every business metric.
"""


def init_project(
    root: Path,
    name: str,
    *,
    dialect: str = "sqlite",
    profile: str | None = None,
    empty: bool = False,
    force: bool = False,
) -> Project:
    """Create a project directory.

    Refuses to overwrite an existing project unless ``force``: re-running
    ``init`` in a populated directory is almost always a mistake, and silently
    rewriting someone's ``vanna_project.yml`` is not a recoverable one.
    """
    paths = ProjectPaths(root)
    if paths.project_file.exists() and not force:
        raise VannaError(
            ErrorCode.INVALID_PROJECT,
            f"{paths.project_file} already exists.",
            phase=ErrorPhase.PROJECT_LOAD,
            hint="Pass --force to overwrite it.",
        )

    paths.ensure_dirs()

    project = Project(
        paths.root,
        ProjectConfig(name=name, dialect=dialect, profile=profile),
    )
    project.save()

    gitignore = paths.root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")

    if not paths.relationships_file.exists():
        paths.relationships_file.write_text(_RELATIONSHIPS_STUB, encoding="utf-8")

    if not empty:
        example_dir = paths.models_dir / "example"
        example_dir.mkdir(parents=True, exist_ok=True)
        (example_dir / MODEL_METADATA).write_text(_EXAMPLE_MODEL, encoding="utf-8")
        (paths.knowledge_dir / KNOWLEDGE_RULES_DIR / "general.md").write_text(
            _EXAMPLE_RULE, encoding="utf-8"
        )

    # Keep the empty knowledge directories in git; a project whose `sql/`
    # vanishes on clone confuses the markdown stores' first write.
    for keep in (
        paths.knowledge_dir / KNOWLEDGE_SQL_DIR / ".gitkeep",
        paths.knowledge_dir / KNOWLEDGE_RULES_DIR / ".gitkeep",
        paths.views_dir / ".gitkeep",
        paths.cubes_dir / ".gitkeep",
    ):
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.touch(exist_ok=True)

    return project


def created_paths(project: Project) -> List[str]:
    """Paths to report after ``init``, relative to the root."""
    paths = project.paths
    return [
        str(p.relative_to(paths.root))
        for p in sorted(paths.root.rglob("*"))
        if p.is_file() and ".vanna" not in p.parts
    ]
