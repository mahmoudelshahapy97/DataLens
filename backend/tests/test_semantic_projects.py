"""The semantic projects under ``projects/`` describe the databases they are bound to.

A semantic manifest *replaces* the physical catalog. Whatever it does not model
does not exist as far as the agent is concerned -- that is the point of a semantic
layer, and it is also the only way one fails silently.

It failed that way here. ``projects/chinook`` was a byte-for-byte copy of a
two-model stub -- ``invoices`` and ``tracks`` -- over an eleven-table database. Nine
tables became invisible, and the workspace answered its own starter question --
*"Which artists earn the most revenue?"* -- with:

    I can't answer that with the tables currently available. This workspace only
    has: invoices, tracks.

Nothing raised, nothing logged at warning level, the health check stayed green, and
no generation was ever recorded because no SQL was ever written. The agent was
right; the manifest was wrong.

Two tests here run without a database, which is the point -- they fail in CI rather
than in a browser twenty seconds into an LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Set

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = sorted(
    p
    for p in (ROOT / "backend" / "projects").iterdir()
    if (p / "vanna_project.yml").is_file()
)

pytestmark = pytest.mark.skipif(not PROJECTS, reason="no semantic projects in this checkout")


def _project(directory: Path):
    from vanna.project import Project

    return Project.load(directory)


def _fresh_manifest(directory: Path):
    from vanna.semantic.project import load_manifest_from_project

    project = _project(directory)
    return load_manifest_from_project(project.paths, dialect=project.config.dialect or "")


@pytest.mark.parametrize("directory", PROJECTS, ids=[p.name for p in PROJECTS])
class TestTheBuiltManifestMatchesItsSources:
    """``target/mdl.json`` is what runs; the YAML tree is what people edit.

    The project layout calls ``target/`` reproducible build output. It is also
    committed, so the two can disagree -- and when they do, the file nobody reads
    is the one in production.
    """

    def test_the_manifest_is_a_current_build(self, directory: Path):
        built = json.loads((directory / "target" / "mdl.json").read_text(encoding="utf-8"))
        assert _fresh_manifest(directory).to_json_dict() == built, (
            f"{directory.name}: target/mdl.json is stale. Run "
            f"`vanna project build --path projects/{directory.name}`."
        )

    def test_every_model_names_a_source(self, directory: Path):
        for model in _fresh_manifest(directory).models:
            assert model.table_reference or model.ref_sql, (
                f"{directory.name}.{model.name} selects from nothing"
            )

    def test_relationships_join_models_that_exist(self, directory: Path):
        """A condition over a model that is not in the manifest compiles to nothing."""
        manifest = _fresh_manifest(directory)
        names = {m.name.lower() for m in manifest.models}
        for relationship in manifest.relationships:
            unknown = [m for m in relationship.models if m.lower() not in names]
            assert not unknown, (
                f"{directory.name}.{relationship.name} joins {unknown}, which "
                f"{directory.name} does not model"
            )

    def test_cubes_are_built_on_models_that_exist(self, directory: Path):
        manifest = _fresh_manifest(directory)
        names = {m.name.lower() for m in manifest.models}
        for cube in manifest.cubes:
            assert cube.base_object.lower() in names, (
                f"{directory.name}.{cube.name} aggregates {cube.base_object!r}, "
                "which is not a model"
            )


class TestChinookCoversItsDatabase:
    """The specific manifest that was wrong, pinned against the specific database.

    ``domains.yml`` states chinook's business rules in terms of physical tables --
    "the chain is invoice_line -> track -> album -> artist". If the manifest does
    not model those tables, the rules describe a schema the agent cannot see, which
    is worse than having no rules at all.
    """

    #: Every table in the chinook sample database. Not derived from the manifest,
    #: or the test would agree with whatever the manifest happens to say.
    CHINOOK_TABLES: Set[str] = {
        "album", "artist", "customer", "employee", "genre", "invoice",
        "invoice_line", "media_type", "playlist", "playlist_track", "track",
    }

    @pytest.fixture
    def manifest(self):
        directory = ROOT / "backend" / "projects" / "chinook"
        if not directory.is_dir():
            pytest.skip("no chinook project")
        return _fresh_manifest(directory)

    def _tables(self, manifest) -> Set[str]:
        return {
            (m.table_reference or "").split(".")[-1].strip('"').lower()
            for m in manifest.models
            if m.table_reference
        }

    def test_every_table_is_modelled(self, manifest):
        missing = self.CHINOOK_TABLES - self._tables(manifest)
        assert not missing, (
            f"the agent cannot see {sorted(missing)}. A table absent from the "
            "manifest does not exist to it."
        )

    def test_it_models_nothing_that_is_not_there(self, manifest):
        """The other direction: a model over a missing table fails at query time."""
        strangers = self._tables(manifest) - self.CHINOOK_TABLES
        assert not strangers, f"models over tables chinook does not have: {sorted(strangers)}"

    def test_the_sale_to_artist_chain_is_joinable(self, manifest):
        """The join path the domain rules promise, asserted as edges.

        There is no track.artist_id. Without every link in
        invoice_lines -> tracks -> albums -> artists, "which artists earn the
        most" has no answer, and the agent says so rather than guessing.
        """
        edges: Dict[str, List[str]] = {}
        for relationship in manifest.relationships:
            left, right = (m.lower() for m in relationship.models)
            edges.setdefault(left, []).append(right)
            edges.setdefault(right, []).append(left)

        for a, b in (
            ("invoice_lines", "tracks"),
            ("tracks", "albums"),
            ("albums", "artists"),
            ("invoices", "invoice_lines"),
            ("customers", "invoices"),
        ):
            assert b in edges.get(a, []), f"no relationship joins {a} to {b}"

    def test_revenue_below_invoice_level_is_a_declared_measure(self, manifest):
        """Revenue per artist is SUM(unit_price * quantity), not SUM(invoice.total).

        A cube exists so this decision is made once by a person instead of
        re-derived per question -- summing invoices.total across invoice lines
        counts each invoice once per line it contains.
        """
        by_base = {c.base_object.lower(): c for c in manifest.cubes}
        cube = by_base.get("invoice_lines")
        assert cube is not None, (
            "no cube aggregates invoice_lines, so per-track revenue has no "
            "declared definition"
        )
        measure = cube.measure("revenue")
        assert measure is not None, f"{cube.name} declares no revenue measure"
        assert "quantity" in measure.expression.lower(), (
            f"{cube.name}.revenue is {measure.expression!r}; line revenue is "
            "unit_price * quantity"
        )

    def test_length_and_size_are_offered_in_units_people_use(self, manifest):
        """milliseconds and bytes are storage details; the rules say so."""
        tracks = next((m for m in manifest.models if m.name == "tracks"), None)
        assert tracks is not None
        visible = {c.name for c in tracks.visible_columns}
        assert "minutes" in visible and "megabytes" in visible
        assert "milliseconds" not in visible, "raw milliseconds should stay hidden"
