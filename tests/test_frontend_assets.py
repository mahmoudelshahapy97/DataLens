"""Static checks on the front-end files.

These exist because of a specific, embarrassing failure. Splitting the inline
``<script>`` out of the HTML and moving the shared helpers into a module left
``app.js`` importing ``applyTheme`` while still defining its own. That is a
redeclaration, and an ES module that fails to parse does not half-run -- *nothing*
executes. The page rendered as a single "Skip to main content" link and every
server-side test still passed, because the server was fine.

A browser catches it. A browser is also slow, needs a running stack, and is not what
anybody runs before a commit. These are the same checks as text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The pages and their assets are served verbatim from `frontend/public/`, at the
# URLs they reference: the Vite build copies this directory into dist/ without
# touching it, so what is asserted here is what the browser gets.
PUBLIC = ROOT / "frontend" / "public"
ASSETS = PUBLIC / "assets"

PAGES = [
    (PUBLIC / "index.html", ASSETS / "app.js", ASSETS / "app.css"),
    (PUBLIC / "admin" / "index.html", ASSETS / "console.js", ASSETS / "console.css"),
]

SCRIPTS = [js for _, js, _ in PAGES]

CORE = ASSETS / "shared" / "core.js"

#: A `t('some.key')` call with a literal key.
#:
#: Written once, here, rather than inline: the first version of this went in
#: with a literal backspace where the ``\b`` was meant to be, which is
#: invisible in a terminal and matched nothing -- so the check silently passed
#: everything it was added to catch.
KEY_CALL = re.compile(r"""\bt\(\s*['"]([A-Za-z0-9_.]+)['"]\s*[),]""")

#: area -> the directory that area's dictionaries are served from.
#:
#: An explicit map, not `page.parent.name`. That worked while each page had its own
#: directory; now both pages are served out of one tree and the workspace page sits
#: at its root, so deriving the area from the parent directory would call it
#: "public".
LOCALES = {
    "web": PUBLIC / "locales",
    "admin": PUBLIC / "admin" / "locales",
}

#: page -> the area it belongs to.
AREA_OF = {
    PUBLIC / "index.html": "web",
    PUBLIC / "admin" / "index.html": "admin",
}


def _imported_names(source: str) -> list:
    """Names an ES module brings into its own scope."""
    match = re.search(r"^import\s*\{(.*?)\}\s*from\s*'([^']+)';", source, re.S | re.M)
    if not match:
        return []
    names = []
    for part in match.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        # `relative as sharedRelative` binds the alias, not the original.
        names.append(part.split(" as ")[-1].strip())
    return names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
class TestModuleScope:
    def test_no_imported_name_is_redeclared(self, script: Path):
        """The bug: `import { applyTheme }` plus `function applyTheme()`.

        SyntaxError at parse time, so the entire file is dead -- not just the
        feature that name belongs to.
        """
        source = script.read_text(encoding="utf-8")
        body = source.split("from './shared/core.js';", 1)[-1]

        clashes = [
            name
            for name in _imported_names(source)
            if re.search(rf"^\s*(?:function|const|let|var|class)\s+{re.escape(name)}\b", body, re.M)
        ]
        assert not clashes, (
            f"{script.name} imports {clashes} and also declares them. "
            "The module will not parse, and the whole page dies."
        )

    def test_nothing_is_declared_twice(self, script: Path):
        """Two `function foo()` at top level is legal; `const foo` twice is not."""
        source = script.read_text(encoding="utf-8")
        declared = re.findall(r"^(?:const|let|class)\s+([A-Za-z_$][\w$]*)", source, re.M)
        duplicates = {n for n in declared if declared.count(n) > 1}
        assert not duplicates, f"{script.name} declares {sorted(duplicates)} more than once"

    def test_every_import_is_actually_used(self, script: Path):
        """An unused import is dead weight, and usually a half-finished rewire."""
        source = script.read_text(encoding="utf-8")
        body = source.split("from './shared/core.js';", 1)[-1]
        unused = [
            name for name in _imported_names(source)
            if not re.search(rf"\b{re.escape(name)}\b", body)
        ]
        assert not unused, f"{script.name} imports but never uses {unused}"

    def test_the_shared_module_exports_what_is_imported(self, script: Path):
        core = CORE.read_text(encoding="utf-8")
        # `async` is part of the declaration, not a separate keyword -- `api` is
        # exported as `export async function api`.
        exported = set(
            re.findall(
                r"^export\s+(?:async\s+)?(?:function|const|let|class)\s+([\w$]+)",
                core,
                re.M,
            )
        )

        source = script.read_text(encoding="utf-8")
        match = re.search(r"^import\s*\{(.*?)\}\s*from\s*'([^']+)';", source, re.S | re.M)
        assert match, f"{script.name} does not import the shared module"

        wanted = {part.strip().split(" as ")[0].strip() for part in match.group(1).split(",") if part.strip()}
        missing = wanted - exported
        assert not missing, f"{script.name} imports {sorted(missing)}, which core.js does not export"


class TestNoInlineCode:
    """A Content Security Policy without 'unsafe-inline' forbids all of this."""

    @pytest.mark.parametrize("page", [p for p, _, _ in PAGES], ids=lambda p: AREA_OF[p])
    def test_no_inline_script_block(self, page: Path):
        html = page.read_text(encoding="utf-8")
        # <script src=...> is fine; <script>code</script> is not.
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(?!\s*</script>)", html)
        assert not inline, f"{AREA_OF[page]}/index.html has an inline <script>; the CSP blocks it"

    @pytest.mark.parametrize("page", [p for p, _, _ in PAGES], ids=lambda p: AREA_OF[p])
    def test_no_inline_event_handlers(self, page: Path):
        html = page.read_text(encoding="utf-8")
        handlers = re.findall(r"\son(?:click|change|input|submit|load|error)\s*=", html)
        assert not handlers, (
            f"{AREA_OF[page]}/index.html has inline event handlers "
            f"({len(handlers)}); the CSP blocks them and a module's functions are "
            "not global anyway"
        )

    @pytest.mark.parametrize("page", [p for p, _, _ in PAGES], ids=lambda p: AREA_OF[p])
    def test_no_inline_style_block(self, page: Path):
        html = page.read_text(encoding="utf-8")
        assert "<style>" not in html, f"{AREA_OF[page]}/index.html has an inline <style>"


class TestReferences:
    @pytest.mark.parametrize("page,script,style", PAGES, ids=lambda p: getattr(p, "name", ""))
    def test_the_page_references_its_own_assets(self, page: Path, script: Path, style: Path):
        html = page.read_text(encoding="utf-8")
        assert f"/assets/{style.name}" in html, f"{page} does not load {style.name}"
        assert f"/assets/{script.name}" in html, f"{page} does not load {script.name}"

    @pytest.mark.parametrize("page", [p for p, _, _ in PAGES], ids=lambda p: AREA_OF[p])
    def test_every_referenced_asset_exists(self, page: Path):
        html = page.read_text(encoding="utf-8")
        missing = []
        for ref in re.findall(r'(?:src|href)="(/[^"]+)"', html):
            name = ref.rsplit("/", 1)[-1]
            if name == "vanna-components.js":
                # The only built asset. Vite emits it into dist/assets/ at build
                # time, and `npm run dev` serves it from src/index.ts, so it is
                # never a file under public/.
                continue
            if ref.startswith("/api/") or not name or "." not in name:
                continue
            # The reference is a URL and public/ is the document root, so the URL
            # *is* the path. This used to guess among three directories, which
            # meant /assets/app.js and /admin/app.js were indistinguishable and
            # either satisfied the assertion.
            if not (PUBLIC / ref.lstrip("/")).exists():
                missing.append(ref)
        assert not missing, f"{AREA_OF[page]}/index.html references missing files: {missing}"


class TestLocaleWiring:
    """The path each page fetches its dictionary from must exist.

    The console asked for `/locales/admin.en.json` long after that file had moved to
    `/admin/locales/en.json`. The fetch 404'd, the catch fell back to raw keys, and
    every label on the screen became its own lookup key -- `console.title`,
    `tab.billing`, `bill.plan`. It reads like a deliberate naming scheme, which is
    why nobody spotted it as a failure.

    Key parity and key existence were already tested. Neither says anything about
    whether the file is reachable at the URL the code asks for.
    """

    #: (script, url template it fetches, directory nginx serves it from)
    WIRING = [
        (ASSETS / "app.js", "/locales/", LOCALES["web"]),
        (ASSETS / "console.js", "/admin/locales/", LOCALES["admin"]),
    ]

    @pytest.mark.parametrize("script,prefix,directory", WIRING, ids=lambda v: getattr(v, "name", str(v)))
    def test_the_fetched_path_matches_where_the_files_live(self, script: Path, prefix: str, directory: Path):
        source = script.read_text(encoding="utf-8")

        fetched = re.findall(r"fetch\(`([^`]*locales[^`]*)`", source)
        assert fetched, f"{script.name} does not fetch a dictionary"

        for url in fetched:
            assert url.startswith(prefix), (
                f"{script.name} fetches {url!r}, but its dictionaries are served "
                f"under {prefix!r}"
            )
            # Resolve the template against the files that actually exist.
            for locale in ("en", "ar"):
                concrete = url.replace("${locale}", locale)
                name = concrete.rsplit("/", 1)[-1]
                assert (directory / name).exists(), (
                    f"{script.name} would fetch {concrete}, which is not in {directory}"
                )

    @pytest.mark.parametrize("script,prefix,directory", WIRING, ids=lambda v: getattr(v, "name", str(v)))
    def test_a_failed_load_is_reported(self, script: Path, prefix: str, directory: Path):
        """Falling back to keys is fine. Doing it silently is not.

        A dictionary that fails to load degrades to raw keys by design -- better than
        a blank page. But the degradation has to be visible, or a 404 looks like a
        label scheme.
        """
        source = script.read_text(encoding="utf-8")
        loader = source.split("async function loadLocale", 1)[-1]
        loader = loader.split("\nfunction ", 1)[0]
        assert "console.error" in loader or "console.warn" in loader, (
            f"{script.name} swallows a failed dictionary load without a word"
        )


class TestTranslations:
    """A key present in one language and not the other renders as `nav.account`."""

    @pytest.mark.parametrize("area", ["web", "admin"])
    def test_the_dictionaries_have_the_same_keys(self, area: str):
        english = json.loads((LOCALES[area] / "en.json").read_text(encoding="utf-8"))
        arabic = json.loads((LOCALES[area] / "ar.json").read_text(encoding="utf-8"))
        assert set(english) == set(arabic), (
            f"{area}: only in en={sorted(set(english) - set(arabic))}, "
            f"only in ar={sorted(set(arabic) - set(english))}"
        )

    @pytest.mark.parametrize("page,script,_style", PAGES, ids=lambda p: getattr(p, "name", ""))
    def test_every_key_used_in_the_markup_exists(self, page: Path, script: Path, _style: Path):
        area = AREA_OF[page]
        english = json.loads((LOCALES[area] / "en.json").read_text(encoding="utf-8"))
        html = page.read_text(encoding="utf-8")

        used = set(re.findall(r'data-i18n="([^"]+)"', html))
        for attr in re.findall(r'data-i18n-attr="([^"]+)"', html):
            used.update(part.split(":", 1)[1] for part in attr.split(",") if ":" in part)

        # The script, not just the markup.
        #
        # This checked `data-i18n` attributes only, and almost every string in the
        # console is built in JavaScript -- so a `t('ws.databases')` with no entry
        # in en.json passed the guard and rendered the raw key to the user, which
        # is the exact failure this test exists to prevent. Two such keys were
        # shipping when this was widened.
        #
        # Literal keys only. A key assembled at runtime -- t() called with a
        # template literal or a concatenation -- cannot be resolved here, and is
        # left to the tests that actually drive the page.
        js = script.read_text(encoding="utf-8")
        used.update(re.findall(KEY_CALL, js))

        missing = sorted(used - set(english))
        assert not missing, (
            f"{area} uses keys with no translation: {missing}"
        )
