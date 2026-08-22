"""The shipped domain content, checked against the rules that govern it.

Nothing else loads `backend/instructions/` or `backend/domains/domains.yml`. That is
a gap rather than an oversight, because both files are strict in ways a reader cannot
see and expensive in ways a reader would not guess:

* **A malformed pack stops the deployment.** ``InstructionLibrary.load()`` runs in
  ``wiring.py`` and raises ``InstructionContentError``, so a typo in a pack file is
  discovered as a container that will not boot. There is no earlier signal.
* **Two workspaces sharing a rule string breaks the isolation canary** in
  ``tests/e2e/test_domains_in_browser.py``, which needs a browser, a live stack and
  an LLM to tell you something a set intersection can tell you in a millisecond.
* **The provisioner cannot correct a rule.** It dedupes on exact text, so an edited
  rule is added alongside the original rather than replacing it. Getting the content
  right before it is provisioned is the only cheap moment.

So these are the assertions that make editing that content safe. They need no
database, no browser, no LLM and no running stack -- which is the point: the
mistakes they catch are the ones that are otherwise expensive to find.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Phrases belonging to the platform baseline. ``test_the_platform_baseline_is_shared``
#: in the e2e suite asserts that any text two workspaces share contains one of these,
#: so a *domain* rule containing one would look like a baseline rule to that test.
BASELINE_PHRASES = (
    "schema context",
    "unit of any number",
    "cut down the rows",
    "explicit column list",
    "explicit JOIN",
)

#: The databases `domains.yml` is allowed to bind a workspace to -- the eight seeded
#: sandbox datasets. A typo here provisions a workspace pointing at nothing.
KNOWN_DATABASES = {
    "chinook",
    "northwind",
    "world",
    "healthcare",
    "ecommerce",
    "booking",
    "pagila",
    "employees",
}


def _norm(text: str) -> str:
    """Whitespace-normalised, the way both dedupe paths see a string."""
    return " ".join(str(text).split())


@pytest.fixture(scope="module")
def domains() -> List[Dict]:
    from vanna_app.domains import load_definitions

    return load_definitions(ROOT / "backend" / "domains" / "domains.yml")


@pytest.fixture(scope="module")
def library():
    """The real shipped library, loaded by the real loader.

    Not a hand-built fixture. A fixture would prove the loader works on data written
    to satisfy it, which is the one thing nobody needs to know.
    """
    from vanna_app.instruction_library import InstructionLibrary

    return InstructionLibrary.load()


def _rules(domains: List[Dict]) -> List[Tuple[str, str]]:
    return [
        (d["id"], _norm(rule["text"]))
        for d in domains
        for rule in d.get("instructions") or []
    ]


def _starters(domains: List[Dict]) -> List[Tuple[str, str]]:
    return [(d["id"], _norm(q)) for d in domains for q in d.get("starters") or []]


# ----------------------------------------------------------------------
# domains.yml
# ----------------------------------------------------------------------


class TestTheDomainFile:
    def test_it_parses_and_every_workspace_has_content(self, domains):
        assert domains, "no domains defined"
        for domain in domains:
            assert domain.get("instructions"), f"{domain['id']} has no rules"
            assert domain.get("starters"), f"{domain['id']} has no starter questions"

    def test_every_workspace_names_a_database_that_exists(self, domains):
        wrong = {d["id"]: d["database"] for d in domains if d["database"] not in KNOWN_DATABASES}
        assert not wrong, f"unknown databases: {wrong}"

    def test_no_two_workspaces_share_a_rule(self, domains):
        """The invariant the e2e isolation canary actually depends on.

        That test computes ``set(pagila_rules) & set(world_rules)`` and demands every
        survivor be a platform-baseline phrase. It only inspects one pair, so a
        duplicate between any *other* two workspaces is a trap that springs later --
        the day somebody widens the sample. Asserted over every pair here.
        """
        counts = collections.Counter(text for _, text in _rules(domains))
        duplicated = {text: count for text, count in counts.items() if count > 1}
        assert not duplicated, (
            "the same rule text appears in more than one workspace, which reads as a "
            f"knowledge leak to the e2e canary: {list(duplicated)[:3]}"
        )

    def test_no_workspace_repeats_a_starter(self, domains):
        counts = collections.Counter(text for _, text in _starters(domains))
        duplicated = [text for text, count in counts.items() if count > 1]
        assert not duplicated, f"duplicate starter questions: {duplicated}"

    def test_no_rule_impersonates_a_platform_rule(self, domains):
        """A domain rule containing a baseline phrase would be mistaken for one."""
        offenders = [
            (domain, phrase)
            for domain, text in _rules(domains)
            for phrase in BASELINE_PHRASES
            if phrase in text
        ]
        assert not offenders, f"domain rules echoing the baseline: {offenders}"

    def test_the_pagila_canary_phrase_is_unique_to_pagila(self, domains):
        """`test_a_rule_from_one_workspace_is_not_visible_in_another` pins this."""
        owners = {domain for domain, text in _rules(domains) if "partitioned by month" in text}
        assert owners == {"pagila"}, (
            f"'partitioned by month' must belong to pagila alone, found in {owners}"
        )

    def test_most_rules_name_the_schema_they_are_about(self, domains):
        """Schema-qualifying a rule is what makes the uniqueness above structural.

        A majority rather than a rule for every one, deliberately. Some legitimate
        domain rules name no table at all -- "salary is personal data, report it
        aggregated" is about the *subject* of a schema, not its shape -- and others
        name a column whose table is obvious from context. Demanding a qualifier
        everywhere would mean rewording eleven rules that shipped before this test,
        and the provisioner turns a reword into a duplicate.

        So this guards the drift that matters: if the qualified share ever falls,
        somebody has started writing conventions into a workspace instead of into a
        pack, and two workspaces will eventually write the same sentence.
        """
        rules = _rules(domains)
        qualified = [
            (domain, text)
            for domain, text in rules
            if any(f"{database}." in text for database in KNOWN_DATABASES)
        ]
        share = len(qualified) / len(rules)
        assert share > 0.75, (
            f"only {len(qualified)} of {len(rules)} domain rules name a "
            "schema-qualified object. Conventions that name no table belong in a "
            "starter-library pack, where every workspace can reuse them."
        )


# ----------------------------------------------------------------------
# The starter library
# ----------------------------------------------------------------------


class TestTheLibrary:
    def test_it_loads(self, library):
        """If this fails, the deployment would not have started.

        Which is the whole reason the test exists: the loader is strict, and until
        now its only caller was the boot path.
        """
        assert library.packs, "no packs loaded"
        assert library.baseline, "no baseline rules loaded"

    def test_every_pack_has_rules(self, library):
        empty = [name for name, pack in library.packs.items() if not pack.instructions]
        assert not empty, f"packs with no rules: {empty}"

    def test_every_pack_id_matches_its_filename(self, library):
        """The loader enforces this, but the message it gives is a boot failure."""
        stems = {path.stem for path in (ROOT / "backend" / "instructions" / "packs").glob("*.yml")}
        assert set(library.packs) == stems, (
            f"pack ids {sorted(set(library.packs))} do not match filenames {sorted(stems)}"
        )

    def test_no_yaml_file_is_silently_ignored(self, library):
        """``load_packs`` globs ``*.yml`` only, and not recursively.

        A pack saved as ``.yaml``, or dropped into a subdirectory, loads no rules and
        reports nothing at all -- the pack simply never appears in the console.
        """
        directory = ROOT / "backend" / "instructions" / "packs"
        ignored = [
            str(path.relative_to(directory))
            for path in directory.rglob("*")
            if path.is_file() and path not in set(directory.glob("*.yml"))
        ]
        assert not ignored, f"files the pack loader will never read: {ignored}"

    def test_no_two_packs_ship_the_same_rule(self, library):
        """``copy_pack`` dedupes casefolded, so the second one is silently skipped.

        A workspace that enables both packs gets the rule once and no warning that
        the other pack contributed less than its rule count implied.
        """
        seen: Dict[str, str] = {}
        collisions = []
        for name, pack in sorted(library.packs.items()):
            for rule in pack.instructions:
                key = _norm(rule.text).casefold()
                if key in seen:
                    collisions.append((seen[key], name, key[:60]))
                seen[key] = name
        assert not collisions, f"the same rule in two packs: {collisions}"

    def test_no_pack_repeats_a_baseline_rule(self, library):
        """This one duplicates rather than skipping, which is worse.

        ``copy_pack`` compares against the tenant's own ``instructions`` rows, and the
        baseline is merged in at read time rather than stored there -- so a pack rule
        identical to a baseline rule is copied in and the workspace shows it twice.
        """
        baseline = {_norm(entry.instruction.text).casefold() for entry in library.baseline}
        offenders = [
            (name, _norm(rule.text)[:60])
            for name, pack in library.packs.items()
            for rule in pack.instructions
            if _norm(rule.text).casefold() in baseline
        ]
        assert not offenders, f"pack rules duplicating the platform baseline: {offenders}"

    def test_packs_name_no_tables(self, library):
        """A pack is reusable precisely because it is schema-agnostic.

        A pack naming a table from one sandbox database is useless to every workspace
        that does not have it, and actively misleading to the ones that do not.
        """
        offenders = [
            (name, database, _norm(rule.text)[:50])
            for name, pack in library.packs.items()
            for rule in pack.instructions
            for database in KNOWN_DATABASES
            if f"{database}." in _norm(rule.text)
        ]
        assert not offenders, f"packs naming a specific schema: {offenders}"

    def test_every_pack_describes_itself(self, library):
        """The console renders `name`, `description` and the first three rule texts.

        A pack with no description renders as a bare title, and nobody enables a rule
        set they cannot read a sentence about.
        """
        bare = [name for name, pack in library.packs.items() if not (pack.description or "").strip()]
        assert not bare, f"packs with no description: {bare}"

    def test_a_pack_previews_well(self, library):
        """Only the first three rules reach the console, so they carry the pack."""
        for name, pack in library.packs.items():
            preview = pack.instructions[:3]
            assert len(preview) >= 3, (
                f"{name} has fewer than three rules, so its console preview is the "
                "whole pack -- fine, but check that is intended"
            )
            for rule in preview:
                assert len(_norm(rule.text)) > 40, (
                    f"{name} leads with a rule too short to be persuasive: {rule.text!r}"
                )
