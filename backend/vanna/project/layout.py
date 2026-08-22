"""Where things live in a Vanna project.

One module holding every path convention, so that a rename is a single edit and
no other module hard-codes a directory name.

The layout is chosen to be *already compatible* with what Vanna reads today:
``knowledge/`` is exactly the tree ``MarkdownExampleStore`` and
``MarkdownInstructionStore`` expect, so a project is not a new knowledge format,
it is a directory those stores can already be pointed at.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

#: Marks a directory as a project root.
PROJECT_FILE = "vanna_project.yml"

#: Declared semantic models: one directory each, holding `metadata.yml` and
#: optionally `ref_sql.sql`. A directory per model rather than one big file so
#: that two people editing two models do not conflict.
MODELS_DIR = "models"
MODEL_METADATA = "metadata.yml"
MODEL_REF_SQL = "ref_sql.sql"

VIEWS_DIR = "views"
CUBES_DIR = "cubes"
RELATIONSHIPS_FILE = "relationships.yml"

#: Curated knowledge, in the format the markdown stores already read.
KNOWLEDGE_DIR = "knowledge"
KNOWLEDGE_SQL_DIR = "sql"       # question -> SQL examples
KNOWLEDGE_RULES_DIR = "rules"   # business rules / instructions

#: Build output and derived caches. Gitignored: everything here is reproducible
#: from the sources above, and checking in a compiled manifest guarantees it
#: eventually disagrees with them.
TARGET_DIR = "target"
MANIFEST_FILE = "mdl.json"
CATALOG_FILE = "catalog.json"

#: Runtime state that is neither source nor build output: search indexes,
#: scratch files.
STATE_DIR = ".vanna"


class ProjectPaths:
    """Resolved paths for one project root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    # -- sources -------------------------------------------------------

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def models_dir(self) -> Path:
        return self.root / MODELS_DIR

    @property
    def views_dir(self) -> Path:
        return self.root / VIEWS_DIR

    @property
    def cubes_dir(self) -> Path:
        return self.root / CUBES_DIR

    @property
    def relationships_file(self) -> Path:
        return self.root / RELATIONSHIPS_FILE

    @property
    def knowledge_dir(self) -> Path:
        return self.root / KNOWLEDGE_DIR

    # -- derived -------------------------------------------------------

    @property
    def target_dir(self) -> Path:
        return self.root / TARGET_DIR

    @property
    def manifest_file(self) -> Path:
        return self.target_dir / MANIFEST_FILE

    @property
    def catalog_file(self) -> Path:
        return self.target_dir / CATALOG_FILE

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    # -- enumeration ---------------------------------------------------

    def model_dirs(self) -> List[Path]:
        """Every directory under ``models/`` that carries a metadata file."""
        if not self.models_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.models_dir.iterdir()
            if path.is_dir() and (path / MODEL_METADATA).is_file()
        )

    def cube_files(self) -> List[Path]:
        if not self.cubes_dir.is_dir():
            return []
        return sorted(
            p for p in self.cubes_dir.iterdir()
            if p.is_file() and p.suffix in {".yml", ".yaml"}
        )

    def view_files(self) -> List[Path]:
        if not self.views_dir.is_dir():
            return []
        return sorted(p for p in self.views_dir.iterdir() if p.suffix == ".sql")

    def ensure_dirs(self) -> None:
        """Create the directories a project needs, leaving existing ones alone."""
        for path in (
            self.models_dir,
            self.views_dir,
            self.cubes_dir,
            self.knowledge_dir / KNOWLEDGE_SQL_DIR,
            self.knowledge_dir / KNOWLEDGE_RULES_DIR,
            self.target_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
