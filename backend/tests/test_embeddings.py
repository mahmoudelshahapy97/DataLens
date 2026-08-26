"""Building the embedder, and refusing an index written by a different one.

The interesting failure here is not "the API key is wrong". It is that two models can
produce vectors of the *same width* and still be incompatible: setting
``VANNA_EMBED_DIMENSIONS=1536`` makes a truncated ``text-embedding-3-large`` vector
exactly as wide as a ``-3-small`` one, Qdrant accepts it, nothing errors, and cosine
similarity between the two returns confident nonsense. A width check alone passes that
case.

So the collection records which model wrote it, and these tests pin that.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from vanna.capabilities.index.embeddings import (
    DEFAULT_MODEL,
    OPENAI_BATCH,
    OPENAI_DIMENSIONS,
    OpenAIEmbedder,
    build_embedder,
)


class FakeOpenAI:
    """Records the calls made, and returns vectors of the requested width."""

    def __init__(self, dimension: int = 1536) -> None:
        self.dimension = dimension
        self.calls: List[dict] = []
        self.embeddings = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        width = kwargs.get("dimensions") or self.dimension
        # Returned out of order deliberately: the adapter must sort by index
        # rather than trusting the order the API happened to use.
        data = [
            type("E", (), {"index": i, "embedding": [float(i)] * width})()
            for i in range(len(kwargs["input"]))
        ]
        return type("R", (), {"data": list(reversed(data))})()


@pytest.fixture
def openai_embedder(monkeypatch):
    def embedder(model: str = DEFAULT_MODEL, **kwargs: Any) -> OpenAIEmbedder:
        e = OpenAIEmbedder(model, api_key="sk-test", **kwargs)
        e._client = FakeOpenAI(OPENAI_DIMENSIONS.get(model, 1536))
        return e

    return embedder


class TestSelection:
    def test_the_default_is_openai(self, monkeypatch):
        monkeypatch.delenv("VANNA_EMBED_PROVIDER", raising=False)
        monkeypatch.delenv("VANNA_EMBED_MODEL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(OpenAIEmbedder, "warm_up", lambda self: None)
        embedder = build_embedder()
        assert isinstance(embedder, OpenAIEmbedder)
        assert embedder.model_name == DEFAULT_MODEL

    def test_openai_is_selected_by_name(self, monkeypatch):
        monkeypatch.delenv("VANNA_EMBED_MODEL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(OpenAIEmbedder, "warm_up", lambda self: None)
        embedder = build_embedder(provider="openai")
        assert isinstance(embedder, OpenAIEmbedder)
        assert embedder.model_name == DEFAULT_MODEL

    def test_an_unknown_provider_falls_back_to_keyword_search(self, monkeypatch):
        # None, not an exception: a misconfigured optional feature should cost
        # ranking quality, not answers. A .env left over from a build that embedded
        # locally lands here too, which is why the log line names the one value that
        # works.
        assert build_embedder(provider="banana") is None
        assert build_embedder(provider="fastembed") is None

    def test_a_missing_api_key_falls_back(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert build_embedder(provider="openai") is None

    def test_the_model_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(OpenAIEmbedder, "warm_up", lambda self: None)
        embedder = build_embedder("text-embedding-3-large", provider="openai")
        assert embedder.dimension == 3072


class TestOpenAIEmbedder:
    def test_native_width_is_known_without_a_call(self):
        # Sized before the first request, so a collection can be created without
        # paying for a probe.
        assert OpenAIEmbedder("text-embedding-3-small", api_key="k").dimension == 1536
        assert OpenAIEmbedder("text-embedding-3-large", api_key="k").dimension == 3072

    def test_dimensions_shortens_the_vector(self, openai_embedder):
        embedder = openai_embedder(dimensions=384)
        assert embedder.dimension == 384
        vectors = embedder.embed(["one"])
        assert len(vectors[0]) == 384
        assert embedder._client.calls[0]["dimensions"] == 384

    def test_asking_for_more_than_exists_is_refused(self):
        with pytest.raises(ValueError, match="more than exists"):
            OpenAIEmbedder("text-embedding-3-small", api_key="k", dimensions=9999)

    def test_no_api_key_is_refused_with_a_usable_message(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError) as caught:
            OpenAIEmbedder("text-embedding-3-small")
        message = str(caught.value)
        assert "OPENAI_API_KEY" in message
        assert "lexical" in message  # names the way out: keyword-only retrieval

    def test_results_are_returned_in_input_order(self, openai_embedder):
        """The API returns an index on each result; the order is not assumed."""
        embedder = openai_embedder()
        vectors = embedder.embed(["a", "b", "c"])
        assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]

    def test_large_batches_are_chunked(self, openai_embedder):
        embedder = openai_embedder()
        embedder.embed([f"doc {i}" for i in range(OPENAI_BATCH * 2 + 5)])
        assert len(embedder._client.calls) == 3
        assert len(embedder._client.calls[0]["input"]) == OPENAI_BATCH

    def test_empty_strings_are_replaced_not_sent(self, openai_embedder):
        """The API rejects an empty input.

        One empty description in a scanned catalog would otherwise fail the whole
        batch, taking every other document in it down.
        """
        embedder = openai_embedder()
        embedder.embed(["", "   ", "real"])
        assert all(text.strip() or text == " " for text in embedder._client.calls[0]["input"])
        assert "" not in embedder._client.calls[0]["input"]

    def test_an_empty_list_makes_no_call(self, openai_embedder):
        embedder = openai_embedder()
        assert embedder.embed([]) == []
        assert embedder._client.calls == []

    def test_a_query_is_one_vector(self, openai_embedder):
        assert len(openai_embedder().embed_query("how many orders")) == 1536

    def test_an_unknown_model_is_probed_rather_than_guessed(self, openai_embedder):
        embedder = openai_embedder("some-future-model")
        assert embedder.dimension == 0  # nothing sizes a collection from this
        embedder.embed(["probe"])
        assert embedder.dimension == 1536


class TestCollectionIdentity:
    """The check that a width comparison alone would miss."""

    @staticmethod
    def _index(model: str, dimension: int):
        from vanna.integrations.qdrant.search_index import QdrantSearchIndex

        index = QdrantSearchIndex.__new__(QdrantSearchIndex)
        index.collection = "vanna_knowledge"
        index.embedder = type("E", (), {"model_name": model, "dimension": dimension})()
        return index

    @staticmethod
    def _client(width: int, identity: str = ""):
        class FakeClient:
            def __init__(self) -> None:
                self.deleted = False

            def get_collection(self, _name):
                params = type("P", (), {"size": width})()
                config = type("C", (), {"params": type("PP", (), {"vectors": params})()})()
                return type("I", (), {"config": config})()

            def retrieve(self, **_kwargs):
                if not identity:
                    return []
                return [type("P", (), {"payload": {"embedder": identity}})()]

            def delete_collection(self, _name):
                self.deleted = True

            def create_collection(self, **_kwargs):
                return None

            def upsert(self, **_kwargs):
                return None

        return FakeClient()

    def test_the_same_model_passes(self):
        index = self._index("text-embedding-3-small", 1536)
        index._check_width(self._client(1536, "text-embedding-3-small:1536"))

    def test_a_different_width_is_refused(self):
        from vanna.integrations.qdrant.search_index import VectorWidthMismatch

        index = self._index("text-embedding-3-small", 1536)
        with pytest.raises(VectorWidthMismatch) as caught:
            index._check_width(self._client(3072, "text-embedding-3-large:3072"))
        assert "reindex" in str(caught.value)

    def test_the_same_width_but_a_different_model_is_refused(self):
        """The trap.

        `VANNA_EMBED_DIMENSIONS=1536` makes a truncated -3-large vector the same
        width as a -3-small one. Qdrant accepts it and nothing errors -- and every
        similarity score is meaningless, because the two models embed into different
        spaces.
        """
        from vanna.integrations.qdrant.search_index import VectorWidthMismatch

        index = self._index("text-embedding-3-small", 1536)
        with pytest.raises(VectorWidthMismatch) as caught:
            index._check_width(self._client(1536, "text-embedding-3-large:1536"))
        message = str(caught.value)
        assert "same-width" in message
        assert "different spaces" in message

    def test_an_unlabelled_collection_is_assumed_to_be_the_old_default(self):
        """Collections written before the sentinel existed carry no label.

        Only the local model that was the default then could have written them, so
        that is what they are compared against -- rather than being waved through as
        though the configured embedder had produced them.
        """
        from vanna.integrations.qdrant.search_index import VectorWidthMismatch

        index = self._index("text-embedding-3-small", 1536)
        with pytest.raises(VectorWidthMismatch):
            index._check_width(self._client(384, identity=""))

    def test_an_unlabelled_collection_is_not_credited_to_the_current_model(self):
        """Same width, no label: still refused.

        The regression this guards against is comparing an unlabelled collection
        against the *configured* model, which would wave through vectors written by
        something else entirely at the same width.
        """
        from vanna.integrations.qdrant.search_index import VectorWidthMismatch

        index = self._index("text-embedding-3-small", 1536)
        with pytest.raises(VectorWidthMismatch):
            index._check_width(self._client(1536, identity=""))

    def test_recreation_is_opt_in(self, monkeypatch):
        import vanna.integrations.qdrant.search_index as module

        monkeypatch.setattr(module, "_RECREATE_ON_MISMATCH", True)
        index = self._index("text-embedding-3-small", 1536)
        client = self._client(384, f"{module._PRE_SENTINEL_MODEL}:384")

        index._check_width(client)  # must not raise
        assert client.deleted, "the stale collection should have been dropped"

    def test_an_unprobed_embedder_defers_the_check(self):
        # dimension 0 means "not measured yet"; comparing against it would refuse a
        # perfectly good collection.
        index = self._index("some-future-model", 0)
        index._check_width(self._client(1536, "some-future-model:1536"))
