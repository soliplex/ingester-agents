"""Unit tests for soliplex.agents.sidecar."""

import asyncio
import json
from typing import ClassVar

import pytest

from soliplex.agents.sidecar import _REGISTRY
from soliplex.agents.sidecar import META_SUFFIX
from soliplex.agents.sidecar import DocumentWrite
from soliplex.agents.sidecar import SidecarKind
from soliplex.agents.sidecar import Sidecars
from soliplex.agents.sidecar import get_kind
from soliplex.agents.sidecar import kinds
from soliplex.agents.sidecar import register
from soliplex.agents.sidecar.meta import MetaSidecar
from soliplex.agents.store import DownloadTarget
from soliplex.agents.store import LocalDocumentStore


@pytest.fixture
def clean_registry():
    """Snapshot and restore _REGISTRY so a test's kinds don't leak."""
    original = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(original)


@pytest.fixture
def store(tmp_path) -> LocalDocumentStore:
    return LocalDocumentStore(DownloadTarget(dir=str(tmp_path / "dl"), source="src"))


@pytest.fixture
def sidecars(store) -> Sidecars:
    return Sidecars(store)


def _doc(**over) -> DocumentWrite:
    base = {
        "source": "src",
        "uri": "docs/readme.md",
        "content": b"hello",
        "mime_type": "text/markdown",
        "metadata": {"project": "enfold"},
        "ingestion_type": "fs",
    }
    return DocumentWrite(**{**base, **over})


# --- registry -------------------------------------------------------------


def test_meta_is_registered():
    assert kinds()["meta"] is MetaSidecar
    assert META_SUFFIX == ".meta.json"


def test_kinds_returns_a_copy():
    """Mutating the returned mapping must not affect the registry."""
    kinds().clear()
    assert "meta" in kinds()


def test_get_kind_instantiates(clean_registry):
    assert isinstance(get_kind("meta"), MetaSidecar)


def test_get_kind_unknown_raises():
    with pytest.raises(KeyError):
        get_kind("nope")


def test_register_adds_a_kind(clean_registry):
    @register
    class _Extra(SidecarKind):
        kind: ClassVar[str] = "extra"
        suffix: ClassVar[str] = ".extra.json"

        def build(self, doc):
            return b"{}"

        def parse(self, content):
            return {}

    assert kinds()["extra"] is _Extra


# --- Sidecars addressing --------------------------------------------------


def test_key_of_appends_the_kinds_suffix(sidecars):
    assert sidecars.key_of("a/b.md", "meta") == "a/b.md.meta.json"


def test_expected_keys_covers_every_registered_kind(sidecars, clean_registry):
    @register
    class _Extra(SidecarKind):
        kind: ClassVar[str] = "extra"
        suffix: ClassVar[str] = ".extra.json"

        def build(self, doc):
            return b"x"

        def parse(self, content):
            return {}

    assert sidecars.expected_keys("a.md") == {"a.md.meta.json", "a.md.extra.json"}


def test_store_property_is_the_first_given(store):
    other = LocalDocumentStore(DownloadTarget(dir="/other", source="s"))
    assert Sidecars([store, other]).store is store


def test_accepts_a_single_store(store):
    assert Sidecars(store).store is store


# --- write / delete -------------------------------------------------------


@pytest.mark.asyncio
async def test_write_all_writes_every_kind(sidecars, store):
    await sidecars.write_all("a.md", _doc())
    assert await store.exists("a.md.meta.json")


@pytest.mark.asyncio
async def test_write_all_skips_a_kind_returning_none(sidecars, store, clean_registry):
    """A kind declining a document costs nothing and writes nothing."""

    @register
    class _Abstains(SidecarKind):
        kind: ClassVar[str] = "abstain"
        suffix: ClassVar[str] = ".abstain.json"

        def build(self, doc):
            return None

        def parse(self, content):
            return {}

    await sidecars.write_all("a.md", _doc())
    assert await store.exists("a.md.abstain.json") is False


@pytest.mark.asyncio
async def test_write_stores_caller_supplied_content(sidecars, store):
    await sidecars.write("a.md", "meta", b'{"k": 1}')
    assert await store.read("a.md.meta.json") == b'{"k": 1}'


@pytest.mark.asyncio
async def test_delete_all_removes_every_kind(sidecars, store):
    await sidecars.write_all("a.md", _doc())
    assert await sidecars.delete_all("a.md") is None
    assert await store.exists("a.md.meta.json") is False


@pytest.mark.asyncio
async def test_delete_all_tolerates_absence(sidecars):
    assert await sidecars.delete_all("absent.md") is None


# --- read_for_uri ---------------------------------------------------------


@pytest.mark.asyncio
async def test_read_for_uri_finds_the_sidecar(sidecars, store):
    await sidecars.write_all("a.md", _doc())
    raw = await sidecars.read_for_uri(store.uri("a.md"))
    assert json.loads(raw)["source_uri"] == "docs/readme.md"


@pytest.mark.asyncio
async def test_read_for_uri_absent_sidecar_is_none(sidecars, store):
    await store.write("a.md", b"body")
    assert await sidecars.read_for_uri(store.uri("a.md")) is None


@pytest.mark.asyncio
async def test_read_for_uri_foreign_uri_is_none(sidecars):
    assert await sidecars.read_for_uri("file:///elsewhere/a.md") is None


@pytest.mark.asyncio
async def test_read_for_uri_searches_every_store(store, tmp_path):
    """A URI owned by the second store is still found.

    This is the mixed-target case: part-way through a storage migration a set
    of documents spans two targets.
    """
    second = LocalDocumentStore(DownloadTarget(dir=str(tmp_path / "other"), source="src"))
    await Sidecars(second).write_all("a.md", _doc())
    both = Sidecars([store, second])
    assert await both.read_for_uri(second.uri("a.md")) is not None


# --- MetaSidecar format ---------------------------------------------------


def test_meta_round_trips_to_the_flattened_shape():
    """build then parse yields the payload with `metadata` promoted.

    The pair is not strictly inverse -- parse flattens -- so the assertion is
    against the flattened shape rather than the payload as written.
    """
    kind = MetaSidecar()
    out = kind.parse(kind.build(_doc()))
    assert out == {
        "mime_type": "text/markdown",
        "source": "src",
        "source_uri": "docs/readme.md",
        "ingestion_type": "fs",
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "size": 5,
        "project": "enfold",
    }
    assert "metadata" not in out


def test_meta_includes_source_url_when_given():
    kind = MetaSidecar()
    out = kind.parse(kind.build(_doc(source_url="https://x.org/a.md")))
    assert out["source_url"] == "https://x.org/a.md"


def test_meta_omits_source_url_when_absent():
    kind = MetaSidecar()
    assert "source_url" not in json.loads(kind.build(_doc()))


def test_meta_drops_none_values():
    kind = MetaSidecar()
    out = kind.parse(kind.build(_doc(ingestion_type=None, metadata={"x": None})))
    assert "ingestion_type" not in out
    assert "x" not in out


def test_meta_nested_key_overrides_top_level():
    kind = MetaSidecar()
    out = kind.parse(kind.build(_doc(metadata={"source": "nested"})))
    assert out["source"] == "nested"


def test_meta_json_encodes_nested_containers():
    kind = MetaSidecar()
    out = kind.parse(kind.build(_doc(metadata={"tags": ["a", "b"], "d": {"z": 1}})))
    assert out["tags"] == json.dumps(["a", "b"])
    assert out["d"] == json.dumps({"z": 1}, sort_keys=True)


def test_meta_non_mapping_metadata_is_ignored():
    """A payload whose `metadata` is not a dict degrades to the top level."""
    raw = json.dumps({"mime_type": "text/plain", "metadata": "not-a-dict"}).encode("utf-8")
    assert MetaSidecar().parse(raw) == {"mime_type": "text/plain"}


def test_meta_payload_without_a_metadata_key():
    """A payload lacking `metadata` entirely parses to its top level.

    build always emits the key, so this only arises for a sidecar written by
    something else -- or by an older version of this code.
    """
    raw = json.dumps({"mime_type": "text/plain", "size": 3}).encode("utf-8")
    assert MetaSidecar().parse(raw) == {"mime_type": "text/plain", "size": 3}


def test_meta_invalid_json_is_empty():
    assert MetaSidecar().parse(b"{not json") == {}


def test_meta_non_object_top_level_is_empty():
    assert MetaSidecar().parse(b"[1, 2, 3]") == {}


# --- batching -------------------------------------------------------------


class _Watched:
    """Wrap a store's write/delete to record how many ran at once."""

    def __init__(self, store):
        self.store = store
        self.peak = 0
        self._in_flight = 0

    def _wrap(self, method):
        async def inner(*args):
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            # Yield, so a serialized caller cannot look concurrent.
            await asyncio.sleep(0)
            try:
                return await method(*args)
            finally:
                self._in_flight -= 1

        return inner

    def install(self):
        self.store.write = self._wrap(self.store.write)
        self.store.delete = self._wrap(self.store.delete)
        return self


@pytest.mark.asyncio
async def test_write_all_issues_every_kind_together(store, clean_registry):
    """Each kind costs a round trip, so serializing them scales with kinds."""

    @register
    class _Second(SidecarKind):
        kind: ClassVar[str] = "second"
        suffix: ClassVar[str] = ".second.json"

        def build(self, doc):
            return b"{}"

        def parse(self, content):
            return {}

    watched = _Watched(store).install()

    await Sidecars(store).write_all("a/b.pdf", _doc())

    assert watched.peak == 2
    assert (store.target.root / "a/b.pdf.meta.json").exists()
    assert (store.target.root / "a/b.pdf.second.json").exists()


@pytest.mark.asyncio
async def test_delete_all_issues_every_kind_together(store, clean_registry):
    @register
    class _Second(SidecarKind):
        kind: ClassVar[str] = "second"
        suffix: ClassVar[str] = ".second.json"

        def build(self, doc):
            return b"{}"

        def parse(self, content):
            return {}

    sidecars = Sidecars(store)
    await sidecars.write_all("a/b.pdf", _doc())
    watched = _Watched(store).install()

    await sidecars.delete_all("a/b.pdf")

    assert watched.peak == 2


@pytest.mark.asyncio
async def test_write_all_with_no_kinds_writes_nothing(store, clean_registry):
    _REGISTRY.clear()

    await Sidecars(store).write_all("a/b.pdf", _doc())

    assert list(store.target.root.rglob("*")) == []
    await Sidecars(store).delete_all("a/b.pdf")
