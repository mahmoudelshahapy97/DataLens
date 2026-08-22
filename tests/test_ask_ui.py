"""Choosing which database a question is asked against, in the browser.

The server side of this is done and tested: a workspace can register several
databases, the choice is validated against that registry, and it is pinned to the
conversation so a later message cannot move a thread to another schema. None of
which a user can reach without a control on the page.

So this drives the real page. It serves ``frontend/public/`` exactly as the
container does and stubs only the API, which is the same arrangement
``test_permissions_ui.py`` uses -- the point being that the file under test is the
one that ships, not a copy of it.

Two things get asserted that a unit test cannot see: the picker appears only when
there is a choice to make, and the chosen database travels on the next request.
"""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public"

ROUTES = {
    "/": PUBLIC / "index.html",
    "/index.html": PUBLIC / "index.html",
    "/assets/app.js": PUBLIC / "assets/app.js",
    "/assets/app.css": PUBLIC / "assets/app.css",
    "/assets/shared/core.js": PUBLIC / "assets/shared/core.js",
    "/assets/shared/dialogs.js": PUBLIC / "assets/shared/dialogs.js",
    "/locales/en.json": PUBLIC / "locales/en.json",
    "/locales/ar.json": PUBLIC / "locales/ar.json",
    "/favicon.svg": PUBLIC / "favicon.svg",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

#: The chat element, which is a build artefact rather than a source file.
#:
#: Stubbed so the suite does not require `npm run build` to have run. It only has
#: to define the element and the two methods the page calls on it -- the chat
#: itself is not what is under test here, and a missing bundle would 404 into a
#: console error that fails every assertion for the wrong reason.
COMPONENT_STUB = """
class VannaChat extends HTMLElement {
  connectedCallback() {
    this.dispatchEvent(new CustomEvent('vanna-ready', { bubbles: true }));
  }
  setCustomHeaders(headers) { this.headers = headers; }
  newConversation() { return 'thread-' + (++VannaChat.threads); }
  sendMessage() {}
}
VannaChat.threads = 0;
customElements.define('vanna-chat', VannaChat);
"""

ME = {
    "user": {"id": "u1", "email": "ada@acme.test", "name": "Ada", "role": "analyst"},
    "tenant": {"id": "acme", "name": "Acme", "description": "", "data_source": "pg",
               "allow_byo_key": True},
    "memberships": ["acme"],
    "is_platform_admin": False,
    "is_admin": False,
    "control_plane": True,
}

ONE_DATABASE = [
    {"data_source_id": "postgresql://wh/chinook", "label": "Chinook", "is_default": True},
]
TWO_DATABASES = ONE_DATABASE + [
    {"data_source_id": "postgresql://wh/world", "label": "World statistics",
     "is_default": False},
]


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        path = self.path.split("?")[0]
        if path == "/assets/vanna-components.js":
            body = COMPONENT_STUB.encode()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES[".js"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        target = ROUTES.get(path)
        if target is None or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "text/plain"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output readable
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        instance = p.chromium.launch(args=["--disable-dev-shm-usage"])
        yield instance
        instance.close()


class Api:
    """A stateful stub, recording the headers every request carried."""

    def __init__(self, sources):
        self.sources = sources
        self.calls = []          # (path, headers)

    def install(self, page):
        def handle(route, request):
            path = request.url.split("://", 1)[-1].split("/", 1)[-1]
            self.calls.append(("/" + path.split("?")[0], dict(request.headers)))
            url = request.url

            if "/v2/me" in url:
                return self._json(route, ME)
            if "/v2/datasources" in url:
                return self._json(route, {"data_sources": self.sources})
            if "/v2/tenants" in url:
                return self._json(route, {"tenants": [{"id": "acme", "name": "Acme"}],
                                          "control_plane": True})
            if "/v2/starters" in url:
                return self._json(route, {"starters": []})
            if "/v2/conversations" in url:
                return self._json(route, {"conversations": []})
            if "/v2/cubes" in url:
                return self._json(route, {"cubes": []})
            return self._json(route, {})

        page.route("**/api/**", handle)

    @staticmethod
    def _json(route, payload):
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps(payload))

    def headers_for(self, needle):
        """Headers of the most recent request whose path contains *needle*."""
        for path, headers in reversed(self.calls):
            if needle in path:
                return headers
        return {}


def open_app(server, browser, sources, *, remembered=None):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    api = Api(sources)
    api.install(page)
    script = (
        "localStorage.setItem('vanna.identity',"
        " JSON.stringify({tenant:'acme', email:'ada@acme.test'}));"
    )
    if remembered is not None:
        script += f"localStorage.setItem('vanna.datasource', {json.dumps(remembered)});"
    page.add_init_script(script)
    page.goto(f"{server}/index.html")
    page.wait_for_selector("#app.ready", timeout=15_000)
    return page, api, errors


class TestThePickerAppearsOnlyWhenThereIsAChoice:
    def test_one_database_hides_it(self, server, browser):
        """A control offering a single option is noise.

        Every workspace had exactly one database until recently, so this is still
        the common case and must look untouched.
        """
        page, _, errors = open_app(server, browser, ONE_DATABASE)
        assert errors == []
        assert page.locator("#db-pick").is_hidden()
        page.close()

    def test_two_databases_show_it(self, server, browser):
        page, _, errors = open_app(server, browser, TWO_DATABASES)
        assert errors == []
        assert page.locator("#db-pick").is_visible()
        assert page.locator("#db-pick option").count() == 2
        page.close()

    def test_the_default_is_selected(self, server, browser):
        page, _, _ = open_app(server, browser, TWO_DATABASES)
        assert page.locator("#db-pick").input_value() == "postgresql://wh/chinook"
        page.close()

    def test_a_remembered_choice_is_restored(self, server, browser):
        page, _, _ = open_app(
            server, browser, TWO_DATABASES, remembered="postgresql://wh/world"
        )
        assert page.locator("#db-pick").input_value() == "postgresql://wh/world"
        page.close()

    def test_a_remembered_database_that_no_longer_exists_falls_back(
        self, server, browser
    ):
        """It must not silently select something else and look chosen.

        The workspace's registry is the authority; a stale localStorage entry is
        not, and quietly honouring it would tell the user they are querying a
        database they are not.
        """
        page, _, _ = open_app(
            server, browser, TWO_DATABASES, remembered="postgresql://wh/deleted"
        )
        assert page.locator("#db-pick").input_value() == "postgresql://wh/chinook"
        page.close()


class TestTheChoiceTravels:
    def test_the_chosen_database_is_sent_on_later_requests(self, server, browser):
        """A preference, not a credential -- but it has to arrive to be checked."""
        page, api, errors = open_app(server, browser, TWO_DATABASES)

        page.select_option("#db-pick", "postgresql://wh/world")
        page.wait_for_timeout(400)

        headers = api.headers_for("/v2/starters")
        assert headers.get("x-data-source-id") == "postgresql://wh/world"
        assert errors == []
        page.close()

    def test_the_choice_survives_a_reload(self, server, browser):
        page, _, _ = open_app(server, browser, TWO_DATABASES)
        page.select_option("#db-pick", "postgresql://wh/world")
        page.wait_for_timeout(300)
        page.reload()
        page.wait_for_selector("#app.ready", timeout=15_000)

        assert page.locator("#db-pick").input_value() == "postgresql://wh/world"
        page.close()

    def test_switching_says_which_database_is_now_in_use(self, server, browser):
        page, _, _ = open_app(server, browser, TWO_DATABASES)
        page.select_option("#db-pick", "postgresql://wh/world")
        page.wait_for_timeout(300)

        assert "World statistics" in page.inner_text("#ds-note")
        page.close()

    def test_switching_starts_a_new_conversation(self, server, browser):
        """The binding is per thread on the server.

        Continuing the current one would leave the model reading earlier turns
        that describe tables no longer in scope.
        """
        page, _, _ = open_app(server, browser, TWO_DATABASES)
        before = page.evaluate("window.__vannaThreads || 0")
        page.select_option("#db-pick", "postgresql://wh/world")
        page.wait_for_timeout(300)

        started = page.evaluate(
            "document.querySelector('vanna-chat').constructor.threads"
        )
        assert started > before
        page.close()
