"""Unit tests for soliplex.agents.store."""

from pathlib import Path
from urllib.parse import quote

import pytest

from soliplex.agents import store as agent_store
from soliplex.agents.store import DocumentStore
from soliplex.agents.store import DownloadTarget
from soliplex.agents.store import LocalDocumentStore
from soliplex.agents.store import get_document_store


@pytest.fixture
def target(tmp_path) -> DownloadTarget:
    return DownloadTarget(dir=str(tmp_path / "dl"), source="gitea:admin:r:all")


@pytest.fixture
def store(target) -> LocalDocumentStore:
    return LocalDocumentStore(target)


# --- DownloadTarget -------------------------------------------------------


def test_target_root_sanitizes_source(target, tmp_path):
    """Illegal characters in the source collapse into one folder name."""
    assert target.root == tmp_path / "dl" / "gitea_admin_r_all"


def test_target_is_local(target):
    assert target.is_local is True


def test_target_uri_and_base_uri_agree(target):
    """A document's URI extends the target's base URI."""
    assert target.uri("a/b.pdf").startswith(target.base_uri)


def test_key_for_uri_round_trips(target):
    """key_for_uri inverts uri() -- the property that keeps the two halves honest."""
    for key in ("plain.pdf", "sub/dir/doc.md", "deep/a/b/c.txt"):
        assert target.key_for_uri(target.uri(key)) == key


def test_key_for_uri_round_trips_encoded_names(target):
    """A name needing percent-encoding survives the round trip.

    An ordinary filename round-trips whether or not the unquote is applied, so
    only an encoding name actually exercises the branch.
    """
    for original in ("a:b.pdf", "50%discount.pdf", "q?x.md"):
        key = quote(original, safe="")
        assert target.key_for_uri(target.uri(key)) == key


def test_key_for_uri_strips_attachment_fragment(target):
    """An attachment child resolves to its parent document's key."""
    uri = target.uri("file.pdf") + "#attachment=Attachment"
    assert target.key_for_uri(uri) == "file.pdf"


def test_key_for_uri_rejects_foreign_uri(target):
    """A URI outside the target returns None rather than a mangled key."""
    assert target.key_for_uri("file:///elsewhere/a.pdf") is None
    assert target.key_for_uri("s3://bucket/a.pdf") is None


def test_key_for_uri_relative_download_dir(tmp_path, monkeypatch):
    """A relative download dir still matches its own documents.

    FSSource stores a resolved absolute URI, so an unresolved base would never
    match anything it wrote.
    """
    monkeypatch.chdir(tmp_path)
    target = DownloadTarget(dir="downloads", source="src")
    assert target.key_for_uri(target.uri("a.pdf")) == "a.pdf"


def test_key_for_uri_raw_when_not_local(target, monkeypatch):
    """A non-local target takes the path verbatim instead of unquoting it.

    Object stores keep the raw key in the URI, so unquoting would corrupt any
    key containing a percent escape.
    """
    monkeypatch.setattr(type(target), "is_local", property(lambda self: False))
    uri = target.uri("a%3Ab.pdf")
    # uri() percent-encodes the % as %25; without the unquote the escape stays.
    assert target.key_for_uri(uri) == "a%253Ab.pdf"


# --- LocalDocumentStore ---------------------------------------------------


@pytest.mark.asyncio
async def test_write_then_read(store):
    await store.write("sub/doc.md", b"hello")
    assert await store.read("sub/doc.md") == b"hello"


@pytest.mark.asyncio
async def test_write_creates_intermediate_dirs(store):
    await store.write("a/b/c/doc.md", b"x")
    assert (store.target.root / "a" / "b" / "c" / "doc.md").is_file()


@pytest.mark.asyncio
async def test_read_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        await store.read("nope.md")


@pytest.mark.asyncio
async def test_exists(store):
    assert await store.exists("doc.md") is False
    await store.write("doc.md", b"x")
    assert await store.exists("doc.md") is True


@pytest.mark.asyncio
async def test_delete_returns_whether_it_existed(store):
    await store.write("doc.md", b"x")
    assert await store.delete("doc.md") is True
    assert await store.delete("doc.md") is False


@pytest.mark.asyncio
async def test_list_is_recursive_and_source_relative(store):
    await store.write("b.md", b"b")
    await store.write("sub/a.md", b"a")
    assert await store.list() == ["b.md", "sub/a.md"]


@pytest.mark.asyncio
async def test_list_absent_root_is_empty(store):
    """An unwritten source lists empty rather than raising."""
    assert await store.list() == []


def test_store_uri_delegates_to_target(store):
    assert store.uri("a.md") == store.target.uri("a.md")


@pytest.mark.asyncio
async def test_destroy_removes_the_source_folder(store):
    await store.write("doc.md", b"x")
    await store.destroy()
    assert await store.list() == []


@pytest.mark.asyncio
async def test_destroy_is_a_noop_when_absent(store):
    await store.destroy()
    assert await store.list() == []


# --- factory --------------------------------------------------------------


def test_get_document_store_uses_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "cfg"))
    store = get_document_store("src")
    assert store.target.root == tmp_path / "cfg" / "src"


def test_get_document_store_override_wins(tmp_path):
    store = get_document_store("src", download_dir=str(tmp_path / "override"))
    assert store.target.root == tmp_path / "override" / "src"


def test_local_store_satisfies_the_protocol(store):
    assert isinstance(store, DocumentStore)


def test_protocol_members_are_declared():
    """The Protocol's own bodies are ellipsis; assert the surface instead."""
    for name in ("write", "read", "exists", "delete", "list", "uri"):
        assert hasattr(DocumentStore, name)
    assert Path  # imported for the annotations above
