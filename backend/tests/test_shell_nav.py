"""Static checks on the React shell's navigation and i18n, as text.

There is no test runner in `frontend/package.json` and no `*.test.*` file
under `frontend/src` -- every existing browser test drives the *vanilla*
build (`test_frontend_assets.py`'s own docstring makes the argument for why
a text-based check is worth having: a parse failure here would otherwise
only ever be caught by a browser, which is slow and not what anyone runs
before a commit).

The i18n parity check is the one worth the most: `en.ts` and `ar.ts` are two
hand-maintained object literals, the runtime only `console.warn`s on a miss,
and a previous session destroyed 514 keys with a careless regex rewrite of
exactly these two files -- so this suite reads, never writes, them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"

EN = SRC / "i18n" / "en.ts"
AR = SRC / "i18n" / "ar.ts"
LAYOUT = SRC / "app" / "layout.tsx"
NAV = SRC / "app" / "nav.ts"

#: A `"some.key": "..."` entry in a dictionary object literal.
DICT_ENTRY = re.compile(r'^\s*"([A-Za-z0-9_.]+)":', re.MULTILINE)

#: A `t('some.key')` or `t("some.key")` call with a literal key.
KEY_CALL = re.compile(r"""\bt\(\s*['"]([A-Za-z0-9_.]+)['"]\s*[),]""")


def _keys(path: Path) -> set:
    return set(DICT_ENTRY.findall(path.read_text(encoding="utf-8")))


class TestI18nParity:
    def test_en_and_ar_have_identical_keys(self):
        en, ar = _keys(EN), _keys(AR)
        missing_in_ar = en - ar
        missing_in_en = ar - en
        assert not missing_in_ar, f"present in en.ts but not ar.ts: {sorted(missing_in_ar)}"
        assert not missing_in_en, f"present in ar.ts but not en.ts: {sorted(missing_in_en)}"

    def test_every_t_call_in_layout_exists_in_both_dictionaries(self):
        source = LAYOUT.read_text(encoding="utf-8")
        called = set(KEY_CALL.findall(source))
        en, ar = _keys(EN), _keys(AR)
        missing = called - en
        assert not missing, f"layout.tsx calls t() with a key missing from en.ts: {missing}"
        missing = called - ar
        assert not missing, f"layout.tsx calls t() with a key missing from ar.ts: {missing}"


class TestRailStrings:
    """The four keys the collapsible rail needs, and the four it must not reuse.

    `nav.collapse`/`expand`/`collapsed`/`expanded` name the *conversation*
    rail (`ConversationRail.tsx`), a different component. Reusing them here
    would announce "Conversations hidden" while toggling the sidebar.
    """

    def test_the_four_rail_keys_exist_in_both_dictionaries(self):
        en, ar = _keys(EN), _keys(AR)
        wanted = {
            "shell.railCollapse",
            "shell.railCollapsed",
            "shell.railExpand",
            "shell.railExpanded",
        }
        assert wanted <= en, f"missing from en.ts: {wanted - en}"
        assert wanted <= ar, f"missing from ar.ts: {wanted - ar}"

    def test_layout_does_not_reuse_the_conversation_rail_wording(self):
        source = LAYOUT.read_text(encoding="utf-8")
        for key in ("nav.collapse", "nav.expand", "nav.collapsed", "nav.expanded"):
            assert key not in source, (
                f"layout.tsx references {key!r}, which names the conversation "
                "rail (ConversationRail.tsx), not the sidebar"
            )


class TestNavStructure:
    def test_every_nav_group_has_a_unique_id(self):
        source = NAV.read_text(encoding="utf-8")
        ids = re.findall(r"^\s*id:\s*'([^']+)',", source, re.MULTILINE)
        assert ids, "no `id:` fields found in nav.ts -- did the NavGroup shape change?"
        assert len(ids) == len(set(ids)), f"duplicate NavGroup ids: {ids}"

    def test_layout_has_no_hardcoded_sidebar_pixel_width(self):
        source = LAYOUT.read_text(encoding="utf-8")
        assert "248px" not in source, (
            "layout.tsx hardcodes a sidebar width; it should read var(--sidebar) "
            "so the collapsed rail (data-rail=mini) can override it"
        )
        assert "var(--sidebar)" in source

    def test_every_literal_aria_controls_target_has_a_matching_id(self):
        """Covers the literal-string case (the toggle buttons' `"sidebar-nav"`).

        The group disclosure's `aria-controls={panelId}` is a computed value
        shared with the panel's own `id={panelId}` in the same component --
        a text search can't safely resolve that without a JS parser, so it is
        out of scope here rather than approximated into a false failure.
        """
        source = LAYOUT.read_text(encoding="utf-8")
        controls = set(re.findall(r'aria-controls="([\w-]+)"', source))
        assert controls, "expected at least one literal aria-controls in layout.tsx"
        ids = set(re.findall(r'\bid="([\w-]+)"', source))
        missing = controls - ids
        assert not missing, f"aria-controls references missing ids: {missing}"
