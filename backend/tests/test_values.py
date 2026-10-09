"""Value resolution and the review queue.

Two properties carry the feature, and both are tested as properties rather than
examples:

* **Tier order is the design.** A confident match must never be displaced by a
  speculative one, so the tests assert which tier answered, not just that
  something did.
* **Nothing untouched by a human reaches a prompt.** Sampling proposes; review
  approves. A test that only checked "the value is found" would pass on a store
  that published everything it read.
"""

from types import SimpleNamespace

import pytest

from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata
from vanna.capabilities.values import (
    ColumnValues,
    MatchTier,
    ReviewStatus,
    SampledValue,
    ValueSynonym,
    describe_matches,
    extract_candidate_terms,
    is_sampleable,
    match_value,
    match_value_semantic,
    resolve_question,
    samples_from_catalog,
)
from vanna.integrations.local import MemoryValueStore


@pytest.fixture
def categories():
    return ColumnValues(
        qualified_name="products.category",
        values=("LAPTOP", "MONITOR", "KEYBOARD"),
        synonyms={"LAPTOP": ("notebook", "portable")},
    )


@pytest.fixture
def statuses():
    return ColumnValues(
        qualified_name="orders.status",
        values=("C", "S", "P"),
        labels={"C": "Cancelled", "S": "Shipped", "P": "Pending"},
    )


@pytest.fixture
def context():
    return SimpleNamespace(tenant_id="acme")


class TestTierOrder:
    """Each tier is strictly less certain than the one above it."""

    @pytest.mark.parametrize("term,expected_tier,expected_value", [
        ("LAPTOP", MatchTier.EXACT, "LAPTOP"),
        ("laptop", MatchTier.NORMALIZED, "LAPTOP"),
        ("Laptops", MatchTier.NORMALIZED, "LAPTOP"),
        ("notebook", MatchTier.SYNONYM, "LAPTOP"),
        ("moniter", MatchTier.FUZZY, "MONITOR"),
    ])
    def test_each_tier_answers(self, categories, term, expected_tier, expected_value):
        matches = match_value(term, categories)
        assert matches, f"{term!r} matched nothing"
        assert matches[0].tier is expected_tier
        assert matches[0].value == expected_value

    def test_a_declared_synonym_outranks_string_similarity(self):
        """Someone who knows the domain typed the synonym. Similarity is luck."""
        column = ColumnValues(
            qualified_name="t.c",
            # LAPTOBX is a close character match for "laptob" but neither an
            # exact nor a normalized one, so the two tiers actually compete.
            # (With "LAPTOB" in the list, normalized wins -- correctly: a
            # case-match against a real stored value should outrank a synonym
            # declared for a different one.)
            values=("LAPTOP", "LAPTOBX"),
            synonyms={"LAPTOP": ("laptob",)},
        )
        best = match_value("laptob", column)[0]
        assert best.tier is MatchTier.SYNONYM
        assert best.value == "LAPTOP"

    def test_a_code_is_reachable_by_its_label(self, statuses):
        """No amount of string similarity finds 'C' from 'cancelled'."""
        matches = match_value("cancelled", statuses)
        assert matches[0].value == "C"
        assert matches[0].tier is MatchTier.SYNONYM

    def test_confidence_descends_with_the_tier(self, categories):
        exact = match_value("LAPTOP", categories)[0]
        normalized = match_value("laptop", categories)[0]
        synonym = match_value("notebook", categories)[0]
        fuzzy = match_value("moniter", categories)[0]
        assert exact.confidence > normalized.confidence > synonym.confidence
        assert synonym.confidence > fuzzy.confidence

    def test_noise_is_not_a_match(self, categories):
        assert match_value("bicycle", categories) == []
        assert match_value("", categories) == []

    def test_an_empty_dictionary_matches_nothing(self):
        assert match_value("laptop", ColumnValues(qualified_name="t.c")) == []

    def test_results_are_deterministic(self, categories):
        """Context that varies run to run makes a failure impossible to reproduce."""
        first = match_value("laptop", categories)
        second = match_value("laptop", categories)
        assert [m.model_dump() for m in first] == [m.model_dump() for m in second]


class TestSemanticTier:
    async def test_it_runs_only_when_the_cheap_tiers_found_nothing(self, categories):
        calls = []

        async def resolver(term, column):
            calls.append(term)
            return [("LAPTOP", 0.9)]

        await match_value_semantic("LAPTOP", categories, resolver)
        assert calls == [], "an exact hit must not pay for an embedding"

        await match_value_semantic("thin computing device", categories, resolver)
        assert calls == ["thin computing device"]

    async def test_it_respects_the_confidence_threshold(self, categories):
        async def weak(term, column):
            return [("LAPTOP", 0.4)]

        assert await match_value_semantic("gadget", categories, weak) == []

    async def test_a_failing_backend_costs_the_tier_not_the_request(self, categories):
        async def broken(term, column):
            raise RuntimeError("vector store down")

        assert await match_value_semantic("gadget", categories, broken) == []


class TestTermExtraction:
    """Narrow on purpose: every word would drown the signal."""

    def test_it_finds_quoted_capitalised_and_code_shaped_terms(self):
        terms = extract_candidate_terms(
            "how many 'Rugged Laptop' units in LAPTOP or SKU-123 shipped to Berlin?"
        )
        # Compared case-insensitively because terms are deduplicated by casefold:
        # the quoted phrase contributes "Laptop", so the later "LAPTOP" is the
        # same term. Either spelling reaches "LAPTOP" through the normalized tier.
        folded = {t.casefold() for t in terms}
        assert "rugged laptop" in folded
        assert "laptop" in folded
        assert "sku-123" in folded, "an internal hyphen must not split the token"
        assert "berlin" in folded

    def test_curly_quotes_work_like_straight_ones(self):
        assert "Rugged Laptop" in extract_candidate_terms("show “Rugged Laptop” sales")

    def test_a_sentence_initial_capital_is_grammar_not_emphasis(self):
        assert "Show" not in extract_candidate_terms("Show me the revenue")

    def test_grammar_words_are_ignored(self):
        assert extract_candidate_terms("how many of them are there") == []

    def test_a_term_is_offered_once(self):
        terms = extract_candidate_terms("LAPTOP and laptop and 'LAPTOP'")
        assert len([t for t in terms if t.casefold() == "laptop"]) == 1


class TestResolvingAQuestion:
    async def test_it_spans_columns_and_renders_for_a_prompt(self, categories, statuses):
        matches = await resolve_question(
            "how many cancelled orders for laptop", [categories, statuses]
        )
        found = {(m.column, m.value) for m in matches}
        assert ("products.category", "LAPTOP") in found
        assert ("orders.status", "C") in found

        rendered = describe_matches(matches)
        assert "products.category" in rendered and "orders.status" in rendered
        assert "'laptop' is stored as 'LAPTOP'" in rendered

    async def test_an_exact_spelling_is_not_described_as_a_translation(self, categories):
        rendered = describe_matches(await resolve_question("show LAPTOP", [categories]))
        assert "is stored as" not in rendered

    async def test_it_is_bounded(self, categories):
        matches = await resolve_question(
            " ".join(f"Term{i}" for i in range(50)) + " laptop",
            [categories],
            max_matches=3,
        )
        assert len(matches) <= 3

    async def test_no_columns_means_no_work(self):
        assert await resolve_question("anything", []) == []


class TestSampleability:
    """A column that is skipped costs a near miss. One wrongly sampled costs
    a privacy incident, so the test is deliberately conservative."""

    @pytest.mark.parametrize("name", [
        "password", "api_key", "ssn", "credit_card", "salary",
        "email", "phone", "home_address", "date_of_birth",
    ])
    def test_sensitive_names_are_never_sampled(self, name):
        assert not is_sampleable(
            ColumnMetadata(name=name, data_type="text", low_cardinality=True)
        )

    def test_a_key_is_not_a_dictionary(self):
        assert not is_sampleable(
            ColumnMetadata(name="order_id", data_type="text", is_primary_key=True)
        )

    def test_a_generated_column_is_not_sampled(self):
        assert not is_sampleable(
            ColumnMetadata(name="slug", data_type="text", is_generated=True)
        )

    def test_an_enum_like_column_is(self):
        assert is_sampleable(
            ColumnMetadata(name="status", data_type="text", low_cardinality=True,
                           categories=["ACTIVE", "CLOSED"])
        )

    def test_a_high_cardinality_column_is_not(self):
        assert not is_sampleable(
            ColumnMetadata(name="note", data_type="text",
                           categories=[str(i) for i in range(500)])
        )

    def test_catalog_categories_become_pending_samples(self):
        samples = samples_from_catalog([
            TableMetadata(table_name="products", schema_name="shop", columns=[
                ColumnMetadata(name="id", is_primary_key=True),
                ColumnMetadata(name="category", data_type="text",
                               categories=["LAPTOP", "MONITOR"]),
                ColumnMetadata(name="email", data_type="text", categories=["a@b.c"]),
            ])
        ])
        assert {s.value for s in samples} == {"LAPTOP", "MONITOR"}
        assert all(s.status is ReviewStatus.PENDING for s in samples)
        assert all(s.table == "shop.products" for s in samples)


class TestReviewQueue:
    async def test_a_sampled_value_is_inert_until_approved(self, context):
        """Turning sampling on must not, by itself, publish column contents."""
        store = MemoryValueStore()
        await store.record_samples(context, [
            SampledValue(table="products", column="category", value="LAPTOP"),
        ])

        dictionary = await store.dictionary_for(
            context, table="products", column="category")
        assert dictionary.is_empty

        await store.set_status(
            context, table="products", column="category",
            values=["LAPTOP"], status=ReviewStatus.APPROVED, actor="alice")

        dictionary = await store.dictionary_for(
            context, table="products", column="category")
        assert dictionary.values == ("LAPTOP",)
        assert match_value("laptop", dictionary)[0].value == "LAPTOP"

    async def test_a_rejected_value_stays_out(self, context):
        store = MemoryValueStore()
        await store.record_samples(context, [
            SampledValue(table="t", column="c", value="SECRET"),
        ])
        await store.set_status(context, table="t", column="c", values=["SECRET"],
                               status=ReviewStatus.REJECTED)
        assert (await store.dictionary_for(context, table="t", column="c")).is_empty

    async def test_resampling_does_not_reset_a_decision(self, context):
        """Otherwise every scan silently un-approves the dictionary."""
        store = MemoryValueStore()
        sample = SampledValue(table="t", column="c", value="LAPTOP")
        await store.record_samples(context, [sample])
        await store.set_status(context, table="t", column="c", values=["LAPTOP"],
                               status=ReviewStatus.APPROVED)

        added = await store.record_samples(context, [sample])
        assert added == 0
        assert (await store.dictionary_for(context, table="t", column="c")).values \
            == ("LAPTOP",)

    async def test_a_synonym_for_an_unapproved_value_does_not_publish_it(self, context):
        """Declaring a nickname must not be a way around the review step."""
        store = MemoryValueStore()
        await store.record_samples(context, [
            SampledValue(table="t", column="c", value="SECRET"),
        ])
        await store.set_synonym(context, ValueSynonym(
            table="t", column="c", value="SECRET", terms=["hidden"]))
        assert (await store.dictionary_for(context, table="t", column="c")).is_empty

    async def test_synonyms_and_labels_reach_an_approved_dictionary(self, context):
        store = MemoryValueStore()
        await store.record_samples(context, [
            SampledValue(table="orders", column="status", value="C"),
        ])
        await store.set_status(context, table="orders", column="status",
                               values=["C"], status=ReviewStatus.APPROVED)
        await store.set_synonym(context, ValueSynonym(
            table="orders", column="status", value="C",
            terms=["voided"], label="Cancelled"))

        dictionary = await store.dictionary_for(
            context, table="orders", column="status")
        assert match_value("cancelled", dictionary)[0].value == "C"
        assert match_value("voided", dictionary)[0].value == "C"

    async def test_the_pending_queue_is_listable(self, context):
        store = MemoryValueStore()
        await store.record_samples(context, [
            SampledValue(table="t", column="c", value="A"),
            SampledValue(table="t", column="c", value="B"),
        ])
        pending = await store.list_samples(context, status=ReviewStatus.PENDING)
        assert {s.value for s in pending} == {"A", "B"}

    async def test_tenants_are_isolated(self, context):
        store = MemoryValueStore()
        await store.record_samples(context, [
            SampledValue(table="t", column="c", value="LAPTOP"),
        ])
        await store.set_status(context, table="t", column="c", values=["LAPTOP"],
                               status=ReviewStatus.APPROVED)
        other = SimpleNamespace(tenant_id="globex")
        assert (await store.dictionary_for(other, table="t", column="c")).is_empty


class TestEnhancer:
    @pytest.fixture
    def catalog(self):
        class Catalog:
            async def get_tables(self, context, *, data_source_id=None, schema=None):
                return [TableMetadata(table_name="products", columns=[
                    ColumnMetadata(name="category", data_type="text",
                                   low_cardinality=True,
                                   categories=["LAPTOP", "MONITOR"])])]
        return Catalog()

    @pytest.fixture
    def user(self):
        from vanna.core.user import User

        return User(id="u", tenant_id="acme", group_memberships=["analyst"])

    async def test_it_puts_the_stored_spelling_in_the_prompt(self, catalog, user):
        from vanna.capabilities.values.enhancer import ValueResolvingEnhancer

        prompt = await ValueResolvingEnhancer(catalog=catalog).enhance_system_prompt(
            "BASE", "how many laptop sales", user)
        assert "BASE" in prompt
        assert "LAPTOP" in prompt

    async def test_a_question_naming_nothing_adds_nothing(self, catalog, user):
        from vanna.capabilities.values.enhancer import ValueResolvingEnhancer

        prompt = await ValueResolvingEnhancer(catalog=catalog).enhance_system_prompt(
            "BASE", "how many rows are there", user)
        assert prompt == "BASE"

    async def test_it_delegates_to_the_enhancer_it_wraps(self, catalog, user):
        from vanna.capabilities.values.enhancer import ValueResolvingEnhancer
        from vanna.core.enhancer import LlmContextEnhancer

        class Inner(LlmContextEnhancer):
            async def enhance_system_prompt(self, system_prompt, user_message, user):
                return system_prompt + "\n[schema]"

        prompt = await ValueResolvingEnhancer(
            catalog=catalog, inner=Inner()
        ).enhance_system_prompt("BASE", "laptop sales", user)
        assert "[schema]" in prompt and "LAPTOP" in prompt

    async def test_a_broken_lookup_costs_the_hints_not_the_request(self, user):
        from vanna.capabilities.values.enhancer import ValueResolvingEnhancer

        class Broken:
            async def get_tables(self, context, **kwargs):
                raise RuntimeError("catalog down")

        prompt = await ValueResolvingEnhancer(catalog=Broken()).enhance_system_prompt(
            "BASE", "laptop sales", user)
        assert prompt == "BASE"
