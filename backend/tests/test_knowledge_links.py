"""`vanna_app.knowledge_links` -- the workspace's curated knowledge, linked to tables.

Glossary terms and cube metrics become table hints for retrieval; core columns
are mapped onto the tables retrieval chose; and the glossary itself is ordered
so the domains a question touches survive the length cap.
"""

from __future__ import annotations

from types import SimpleNamespace

from vanna.capabilities.schema_catalog.models import TableMetadata

from vanna_app.domain_prompt import MAX_CHARS, DomainContextEnhancer
from vanna_app.knowledge_links import (
    MAX_HINTS,
    WorkspaceKnowledge,
    manifest_of,
    match_domains,
    tables_named_in,
)

SALES = {
    "name": "Sales",
    "description": "Invoicing",
    "is_enabled": True,
    "tables": ["chinook.invoice", "chinook.invoice_line"],
    "terminology": {
        "net revenue": "SUM(invoice_line.unit_price * invoice_line.quantity)",
        "churn": "No invoice in 90 days.",
    },
}
CATALOG = {
    "name": "Catalogue",
    "description": None,
    "is_enabled": True,
    "tables": ["chinook.track", "chinook.album"],
    "terminology": {"long track": "Over ten minutes."},
}
HIDDEN = {**CATALOG, "name": "Old", "is_enabled": False, "terminology": {"revenue": "x"}}


class _Domains:
    def __init__(self, domains):
        self.domains = domains
        self.calls = []

    async def list_domains(self, tenant_id, *, data_source_id=None):
        self.calls.append((tenant_id, data_source_id))
        return self.domains


class _CoreStore:
    async def get_core_columns_map(self, tenant_id, data_source_id, table_keys):
        assert (tenant_id, data_source_id) == ("acme", "wh")
        return {k: ["name"] for k in table_keys if k.endswith("artist")}


def _manifest():
    measure = SimpleNamespace(name="invoice_count")
    return SimpleNamespace(cubes=[
        SimpleNamespace(name="sales", base_object="invoices", measures=[measure],
                        dimensions=[SimpleNamespace(name="billing_country")],
                        time_dimensions=[]),
        SimpleNamespace(name="track_sales", base_object="invoice_lines",
                        measures=[SimpleNamespace(name="units")], dimensions=[],
                        time_dimensions=[]),
    ])


class TestMatchDomains:
    def test_terms_and_names_match_disabled_domains_do_not(self):
        matched = match_domains([SALES, CATALOG, HIDDEN], "Net revenue for the catalogue")
        assert [(d["name"], terms) for d, terms in matched] == [
            ("Sales", ["net revenue"]),
            ("Catalogue", []),  # by name
        ]

    def test_plurals_meet(self):
        assert match_domains([CATALOG], "how many long tracks are there")

    def test_nothing_matches_nothing(self):
        assert match_domains([SALES, CATALOG], "employees by hire date") == []


class TestTablesNamedIn:
    def test_dotted_identifiers_only(self):
        assert tables_named_in("SUM(invoice_line.unit_price) from the order book") == [
            "invoice_line", "invoice_line.unit_price",
        ]


class TestWorkspaceKnowledge:
    def _knowledge(self, domains=(SALES, CATALOG), manifest=None):
        return WorkspaceKnowledge(
            tenant_id="acme", data_source_id="wh", domains=_Domains(list(domains)),
            catalog_store=_CoreStore(), manifest=manifest,
        )

    async def test_term_definition_tables_come_before_domain_tables(self):
        hints = await self._knowledge().table_hints(None, "What is our net revenue?")
        assert hints[:2] == ["invoice_line", "invoice_line.unit_price"]
        assert "chinook.invoice" in hints
        assert "chinook.track" not in hints

    async def test_cube_measures_and_dimensions_point_at_their_model(self):
        knowledge = self._knowledge(domains=(), manifest=_manifest())
        assert await knowledge.table_hints(None, "invoice count by billing country") == [
            "invoices"
        ]
        assert await knowledge.table_hints(None, "units sold") == ["invoice_lines"]

    async def test_hints_are_capped(self):
        wide = {**SALES, "tables": [f"t{i}" for i in range(20)]}
        hints = await self._knowledge(domains=(wide,)).table_hints(None, "churn")
        assert len(hints) == MAX_HINTS

    async def test_a_failing_domain_store_gives_no_hints(self):
        class Broken:
            async def list_domains(self, *a, **k):
                raise RuntimeError("down")

        knowledge = WorkspaceKnowledge(tenant_id="acme", data_source_id="wh", domains=Broken())
        assert await knowledge.table_hints(None, "net revenue") == []

    async def test_core_columns_map_back_to_qualified_names(self):
        tables = [TableMetadata(table_name="Artist", schema_name="Chinook"),
                  TableMetadata(table_name="album", schema_name="chinook")]
        assert await self._knowledge().core_columns(None, tables) == {
            "Chinook.Artist": ["name"]
        }

    def test_manifest_is_found_through_wrappers(self):
        class Guard:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

        manifest = _manifest()
        assert manifest_of(Guard(SimpleNamespace(manifest=manifest))) is manifest
        assert manifest_of(object()) is None


class TestGlossaryOrdering:
    def test_touched_domain_and_used_terms_come_first(self):
        text = DomainContextEnhancer._render([CATALOG, SALES], "show churn")
        assert text.index("**Sales**") < text.index("**Catalogue**")
        sales = text[text.index("**Sales**"):]
        assert sales.index("churn:") < sales.index("net revenue:")

    def test_without_a_question_the_order_is_unchanged(self):
        text = DomainContextEnhancer._render([CATALOG, SALES])
        assert text.index("**Catalogue**") < text.index("**Sales**")

    def test_the_cap_cuts_unrelated_domains_first(self):
        filler = [
            {"name": f"Filler {i}", "is_enabled": True, "tables": [],
             "terminology": {f"term {i}": "x" * 300}}
            for i in range(10)
        ]
        text = DomainContextEnhancer._render(filler + [SALES], "net revenue")
        assert len(text) <= MAX_CHARS
        assert "**Sales**" in text
