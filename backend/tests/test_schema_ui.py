"""Describing tables and columns from the schema screen.

``table_annotations`` and ``column_annotations`` have been merged into the model's
prompt since migration 0010, and ``value_labels`` is the single highest-value piece
of column metadata there is -- a wrong literal returns zero rows rather than an
error anybody can act on. There was no write endpoint and no editor, so the only
way to set any of it was raw SQL.

Asserted here: an admin can write both, the code book is sent as a set rather than
merged, and the editor is absent where it would not apply -- for a non-admin, and
over the semantic layer, where the names on screen are models rather than catalog
tables.
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from test_ask_ui import (  # noqa: E402 - harness reuse; tests/ is not a package
    ME,
    ONE_DATABASE,
    browser,  # noqa: F401 - pytest fixture
    server,  # noqa: F401 - pytest fixture
)

SCHEMA = {
    "dialect": "postgres",
    "data_source": "postgresql://wh/chinook",
    "semantic": False,
    "layer": "physical",
    "tables": [
        {
            "name": "public.orders",
            "schema": "public",
            "description": None,
            "row_count_estimate": 120,
            "columns": [
                {"name": "id", "data_type": "int", "nullable": False,
                 "is_primary_key": True, "foreign_key": None,
                 "categories": None, "sample_values": [1, 2], "description": None},
                {"name": "status", "data_type": "text", "nullable": True,
                 "is_primary_key": False, "foreign_key": None,
                 "categories": ["A", "C"], "sample_values": None,
                 "description": None},
            ],
        }
    ],
    "relationships": [],
}


def an_admin(is_admin=True):
    me = copy.deepcopy(ME)
    me["is_admin"] = is_admin
    me["user"]["role"] = "admin" if is_admin else "analyst"
    return me


class Api:
    """Records the PATCHes and serves the annotations back."""

    def __init__(self, *, me=None, schema=None):
        self.me = me or an_admin()
        self.schema = copy.deepcopy(schema or SCHEMA)
        self.table_annotation = {"table_key": "public.orders", "description": "",
                                 "display_name": ""}
        self.column_annotations = {}
        self.patches = []  # (path, body)

    def install(self, page):
        def handle(route, request):
            url = request.url
            if "/v2/me" in url:
                return self._json(route, self.me)
            if "/v2/datasources" in url:
                return self._json(route, {"data_sources": ONE_DATABASE})
            if "/v2/tenants" in url and "/catalog" not in url:
                return self._json(
                    route,
                    {"tenants": [{"id": "acme", "name": "Acme"}], "control_plane": True},
                )
            if "/v2/starters" in url:
                return self._json(route, {"starters": []})
            if "/v2/conversations" in url:
                return self._json(route, {"conversations": [], "persisted": True})
            if "/v2/cubes" in url:
                return self._json(route, {"cubes": []})
            if "/catalog/" in url:
                return self._catalog(route, request)
            if "/v2/schema" in url:
                return self._json(route, self.schema)
            return self._json(route, {})

        page.route("**/api/**", handle)

    def _catalog(self, route, request):
        path = request.url.split("/catalog/", 1)[-1].split("?")[0]
        if request.method == "GET":
            return self._json(
                route,
                {"annotation": self.table_annotation, "columns": self.column_annotations},
            )

        body = request.post_data_json or {}
        self.patches.append((path, body))

        if path.startswith("tables/"):
            self.table_annotation["description"] = body.get("description", "")
            self.schema["tables"][0]["description"] = body.get("description") or None
            return self._json(route, {"annotation": self.table_annotation})

        column = path.split("/")[-1].lower()
        stored = {
            "column_key": column,
            "description": body.get("description", ""),
            "value_labels": body.get("value_labels", {}),
            "display_name": "",
            "sensitivity": None,
        }
        self.column_annotations[column] = stored
        # The screen renders the folded form -- description plus the code book --
        # because that is what reaches the prompt.
        rendered = "; ".join(f"{k} = {v}" for k, v in stored["value_labels"].items())
        joined = ". ".join(part for part in (stored["description"], rendered) if part)
        for entry in self.schema["tables"][0]["columns"]:
            if entry["name"].lower() == column:
                entry["description"] = joined or None
        return self._json(route, {"annotation": stored})

    @staticmethod
    def _json(route, payload):
        return route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        )


def open_schema(server, browser, api):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    api.install(page)
    page.add_init_script(
        "localStorage.setItem('vanna.identity',"
        " JSON.stringify({tenant:'acme', email:'ada@acme.test'}));"
    )
    page.goto(f"{server}/index.html")
    page.wait_for_selector("#app.ready", timeout=15_000)
    page.click('button[data-view="schema"]')
    page.wait_for_selector("#table-detail table.data", timeout=5_000)
    return page, errors


class TestDescribingATable:
    def test_an_empty_description_says_why_it_matters(self, server, browser):
        page, errors = open_schema(server, browser, Api())
        try:
            assert "No description yet" in page.locator("#table-detail").inner_text()
            assert errors == []
        finally:
            page.close()

    def test_it_patches_and_shows_up(self, server, browser):
        api = Api()
        page, errors = open_schema(server, browser, api)
        try:
            page.click("#describe-table")
            page.wait_for_selector("#ann-desc")
            page.fill("#ann-desc", "One row per order.")
            page.click("#ann-save")
            page.wait_for_function(
                "document.getElementById('table-detail').textContent"
                ".includes('One row per order.')"
            )

            path, body = api.patches[0]
            assert path == "tables/public.orders"
            assert body == {"description": "One row per order."}
            assert errors == []
        finally:
            page.close()

    def test_an_existing_description_is_prefilled(self, server, browser):
        api = Api()
        api.schema["tables"][0]["description"] = "One row per order."
        page, errors = open_schema(server, browser, api)
        try:
            page.click("#describe-table")
            page.wait_for_selector("#ann-desc")
            assert page.locator("#ann-desc").input_value() == "One row per order."
            assert errors == []
        finally:
            page.close()


class TestDescribingAColumn:
    def test_the_code_book_is_sent_and_rendered(self, server, browser):
        api = Api()
        page, errors = open_schema(server, browser, api)
        try:
            page.click('[data-describe-column="status"]')
            page.wait_for_selector("#ann-col-desc")
            page.fill("#ann-col-desc", "Order state")
            page.fill("#ann-labels .label-code", "A")
            page.fill("#ann-labels .label-text", "Active")
            page.click("#ann-add-label")
            page.locator("#ann-labels .label-code").nth(1).fill("C")
            page.locator("#ann-labels .label-text").nth(1).fill("Cancelled")
            page.click("#ann-col-save")
            page.wait_for_function(
                "document.getElementById('table-detail').textContent"
                ".includes('A = Active')"
            )

            path, body = api.patches[0]
            assert path == "columns/public.orders/status"
            assert body["description"] == "Order state"
            assert body["value_labels"] == {"A": "Active", "C": "Cancelled"}
            assert errors == []
        finally:
            page.close()

    def test_the_values_the_scan_saw_are_offered(self, server, browser):
        """The codes are right there in the profile; making somebody retype them
        from the row behind the dialog is how a typo gets into the prompt."""
        page, errors = open_schema(server, browser, Api())
        try:
            page.click('[data-describe-column="status"]')
            page.wait_for_selector("#ann-col-desc")
            sheet = page.locator("#sheet").inner_text()
            assert "Values seen in this column" in sheet
            assert "A" in sheet and "C" in sheet
            assert errors == []
        finally:
            page.close()

    def test_a_code_with_no_meaning_is_dropped(self, server, browser):
        api = Api()
        page, errors = open_schema(server, browser, api)
        try:
            page.click('[data-describe-column="status"]')
            page.wait_for_selector("#ann-col-desc")
            page.fill("#ann-labels .label-code", "A")
            page.click("#ann-col-save")
            page.wait_for_selector("#overlay.on", state="hidden")

            assert api.patches[0][1]["value_labels"] == {}
            assert errors == []
        finally:
            page.close()

    def test_removing_a_code_removes_it(self, server, browser):
        """The book is sent whole, so an omitted code is a removed code -- merging
        would make deleting one impossible."""
        api = Api()
        api.column_annotations["status"] = {
            "column_key": "status", "description": "Order state",
            "value_labels": {"A": "Active", "X": "Typo"},
            "display_name": "", "sensitivity": None,
        }
        page, errors = open_schema(server, browser, api)
        try:
            page.click('[data-describe-column="status"]')
            page.wait_for_selector("#ann-labels .label-row")
            assert page.locator("#ann-labels .label-row").count() == 2

            page.locator("#ann-labels [data-label-remove]").nth(1).click()
            page.click("#ann-col-save")
            page.wait_for_selector("#overlay.on", state="hidden")

            assert api.patches[0][1]["value_labels"] == {"A": "Active"}
            assert errors == []
        finally:
            page.close()


class TestWhereTheEditorDoesNotBelong:
    def test_a_non_admin_is_not_offered_it(self, server, browser):
        """The route is tenant-admin; a button that is always refused is worse
        than no button."""
        page, errors = open_schema(server, browser, Api(me=an_admin(False)))
        try:
            assert page.locator("#describe-table").count() == 0
            assert page.locator("[data-describe-column]").count() == 0
            assert errors == []
        finally:
            page.close()

    def test_the_semantic_layer_is_not_offered_it(self, server, browser):
        """An annotation is keyed on a catalog table, and a model is not one."""
        api = Api()
        api.schema["semantic"] = True
        api.schema["layer"] = "active"
        page, errors = open_schema(server, browser, api)
        try:
            assert page.locator("#describe-table").count() == 0
            assert page.locator("[data-describe-column]").count() == 0
            # And the toggle to the physical layer is still there, which is how an
            # admin gets to the editor.
            assert page.locator("#layer-toggle").count() == 1
            assert errors == []
        finally:
            page.close()
