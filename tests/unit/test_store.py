"""Unit tests for soliplex.agents.store.

The `store` fixture is parametrized over both backends, so every contract test
runs twice. That is the point: the two implementations only stay
interchangeable if the same assertions hold for both.
"""

from urllib.parse import quote

import pytest
from obstore.store import MemoryStore
from pydantic import SecretStr

from soliplex.agents import store as agent_store
from soliplex.agents.store import DocumentStore
from soliplex.agents.store import DownloadTarget
from soliplex.agents.store import LocalDocumentStore
from soliplex.agents.store import S3DocumentStore
from soliplex.agents.store import get_document_store
from soliplex.agents.store import storage_options

SOURCE = "gitea:admin:r:all"


@pytest.fixture
def local_target(tmp_path) -> DownloadTarget:
    return DownloadTarget(dir=str(tmp_path / "dl"), source=SOURCE)


@pytest.fixture
def s3_target() -> DownloadTarget:
    return DownloadTarget(dir="downloads", source=SOURCE, bucket="docs-bucket")


@pytest.fixture(autouse=True)
def _clear_store_cache():
    """Stores are cached per target; don't let one test see another's."""
    agent_store.reset_store_cache()
    yield
    agent_store.reset_store_cache()


@pytest.fixture
def memory_store(monkeypatch):
    """Swap the obstore S3Store for one shared in-process MemoryStore.

    `_make_s3_store` exists as its own function so this is the only seam a test
    needs; nothing else about S3DocumentStore is mocked. One instance per test,
    so two stores resolved for the same target see the same objects -- which is
    what a real bucket does.
    """
    shared = MemoryStore()
    monkeypatch.setattr(agent_store, "_make_s3_store", lambda bucket, options: shared)


@pytest.fixture(params=["local", "s3"])
def store(request, local_target, s3_target, memory_store) -> DocumentStore:
    if request.param == "local":
        return LocalDocumentStore(local_target)
    return S3DocumentStore(s3_target)


# --- DownloadTarget: local ------------------------------------------------


def test_local_target_root_sanitizes_source(local_target, tmp_path):
    assert local_target.root == tmp_path / "dl" / "gitea_admin_r_all"


def test_local_target_is_local(local_target):
    assert local_target.is_local is True


def test_local_base_uri_is_resolved_absolute(tmp_path, monkeypatch):
    """A relative download dir still matches its own documents.

    A consumer stores `Path.as_uri()`, which is resolved, so an unresolved base
    would never match anything this target wrote.
    """
    monkeypatch.chdir(tmp_path)
    target = DownloadTarget(dir="downloads", source="src")
    assert target.key_for_uri(target.uri("a.pdf")) == "a.pdf"


# --- DownloadTarget: s3 ---------------------------------------------------


def test_s3_target_is_not_local(s3_target):
    assert s3_target.is_local is False


def test_s3_prefix_and_base_uri(s3_target):
    assert s3_target.prefix == "downloads/gitea_admin_r_all"
    assert s3_target.base_uri == "s3://docs-bucket/downloads/gitea_admin_r_all"


def test_s3_base_uri_with_empty_dir():
    """An empty download dir puts the source folder at the bucket root."""
    target = DownloadTarget(dir="", source="src", bucket="b")
    assert target.base_uri == "s3://b/src"


def test_s3_dir_slashes_are_normalized():
    target = DownloadTarget(dir="/downloads/", source="src", bucket="b")
    assert target.base_uri == "s3://b/downloads/src"


def test_s3_uri_appends_the_key(s3_target):
    assert s3_target.uri("a/b.pdf") == "s3://docs-bucket/downloads/gitea_admin_r_all/a/b.pdf"


# --- key_for_uri: the property that keeps addressing honest ---------------


@pytest.mark.parametrize("key", ["plain.pdf", "sub/dir/doc.md", "deep/a/b/c.txt"])
def test_key_for_uri_round_trips(local_target, s3_target, key):
    for target in (local_target, s3_target):
        assert target.key_for_uri(target.uri(key)) == key


@pytest.mark.parametrize("original", ["a:b.pdf", "50%discount.pdf", "q?x.md"])
def test_key_for_uri_round_trips_encoded_names(local_target, s3_target, original):
    """A name needing percent-encoding survives on both backends.

    An ordinary filename round-trips whether or not the scheme-conditional
    unquote is right, so only an encoding name exercises the branch: a local URI
    arrives percent-encoded, an s3 URI carries the raw key.
    """
    key = quote(original, safe="")
    for target in (local_target, s3_target):
        assert target.key_for_uri(target.uri(key)) == key


def test_s3_key_for_uri_does_not_unquote(s3_target):
    """The raw key is returned verbatim; unquoting would corrupt an escape."""
    assert s3_target.key_for_uri("s3://docs-bucket/downloads/gitea_admin_r_all/a%3Ab.pdf") == "a%3Ab.pdf"


def test_key_for_uri_strips_attachment_fragment(local_target, s3_target):
    """An attachment child resolves to its parent document's key."""
    for target in (local_target, s3_target):
        assert target.key_for_uri(target.uri("file.pdf") + "#attachment=A") == "file.pdf"


def test_key_for_uri_rejects_foreign_uri(local_target, s3_target):
    for target in (local_target, s3_target):
        assert target.key_for_uri("file:///elsewhere/a.pdf") is None
        assert target.key_for_uri("s3://other-bucket/a.pdf") is None


# --- DocumentStore contract (both backends) ------------------------------


@pytest.mark.asyncio
async def test_write_then_read(store):
    await store.write("sub/doc.md", b"hello")
    assert await store.read("sub/doc.md") == b"hello"


@pytest.mark.asyncio
async def test_write_nested_key(store):
    await store.write("a/b/c/doc.md", b"x")
    assert await store.read("a/b/c/doc.md") == b"x"


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
async def test_delete_reports_whether_it_existed(store):
    await store.write("doc.md", b"x")
    assert await store.delete("doc.md") is True
    assert await store.delete("doc.md") is False


@pytest.mark.asyncio
async def test_list_is_recursive_and_source_relative(store):
    await store.write("b.md", b"b")
    await store.write("sub/a.md", b"a")
    assert await store.list() == ["b.md", "sub/a.md"]


@pytest.mark.asyncio
async def test_list_absent_source_is_empty(store):
    assert await store.list() == []


@pytest.mark.asyncio
async def test_destroy_removes_everything(store):
    await store.write("doc.md", b"x")
    await store.write("sub/other.md", b"y")
    await store.destroy()
    assert await store.list() == []


@pytest.mark.asyncio
async def test_destroy_is_a_noop_when_empty(store):
    await store.destroy()
    assert await store.list() == []


def test_uri_delegates_to_target(store):
    assert store.uri("a.md") == store.target.uri("a.md")


def test_satisfies_the_protocol(store):
    assert isinstance(store, DocumentStore)


# --- S3-specific behaviour ------------------------------------------------


@pytest.mark.asyncio
async def test_s3_keys_are_written_under_the_prefix(s3_target, memory_store):
    """Objects land under `<dir>/<source>/`, not at the bucket root."""
    import obstore

    store = S3DocumentStore(s3_target)
    await store.write("a.md", b"x")
    async for batch in obstore.list(store._store):
        assert [obj["path"] for obj in batch] == ["downloads/gitea_admin_r_all/a.md"]


@pytest.mark.asyncio
async def test_s3_list_ignores_other_prefixes(s3_target, memory_store):
    """A neighbouring prefix in the same bucket is not this source's business."""
    import obstore

    store = S3DocumentStore(s3_target)
    await store.write("mine.md", b"x")
    await obstore.put_async(store._store, "downloads/other_source/theirs.md", b"y")
    assert await store.list() == ["mine.md"]


@pytest.mark.asyncio
async def test_s3_store_at_bucket_root(memory_store):
    """An empty download dir means keys sit directly under the source folder."""
    store = S3DocumentStore(DownloadTarget(dir="", source="src", bucket="b"))
    await store.write("a.md", b"x")
    assert await store.list() == ["a.md"]


def test_make_s3_store_delegates_to_haiku(monkeypatch):
    """The endpoint / path-style decision is haiku's, so both sides agree."""
    seen = {}

    def fake(bucket, options):
        seen["args"] = (bucket, options)
        return MemoryStore()

    monkeypatch.setattr("haiku.rag.s3.make_s3_store", fake)
    agent_store._make_s3_store("b", {"region": "xx"})
    assert seen["args"] == ("b", {"region": "xx"})


# --- storage_options ------------------------------------------------------


def test_storage_options_omits_unset(monkeypatch):
    """An unset credential is omitted so the AWS default chain still applies."""
    for name in ("s3_access_key_id", "s3_secret_access_key", "s3_region", "s3_endpoint_url"):
        monkeypatch.setattr(agent_store.settings, name, None)
    monkeypatch.setattr(agent_store.settings, "s3_allow_http", False)
    assert storage_options() == {}


def test_storage_options_unwraps_the_secret(monkeypatch):
    monkeypatch.setattr(agent_store.settings, "s3_access_key_id", "key")
    monkeypatch.setattr(agent_store.settings, "s3_secret_access_key", SecretStr("shh"))
    monkeypatch.setattr(agent_store.settings, "s3_region", "xx")
    monkeypatch.setattr(agent_store.settings, "s3_endpoint_url", "http://minio:9000")
    monkeypatch.setattr(agent_store.settings, "s3_allow_http", True)
    assert storage_options() == {
        "aws_access_key_id": "key",
        "aws_secret_access_key": "shh",
        "region": "xx",
        "endpoint": "http://minio:9000",
        "allow_http": "true",
    }


# --- factory dispatch -----------------------------------------------------


def test_factory_is_local_without_a_bucket(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", None)
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "cfg"))
    store = get_document_store("src")
    assert isinstance(store, LocalDocumentStore)
    assert store.target.root == tmp_path / "cfg" / "src"


def test_factory_is_s3_with_a_bucket(monkeypatch, memory_store):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", "b")
    monkeypatch.setattr(agent_store.settings, "download_dir", "downloads")
    store = get_document_store("src")
    assert isinstance(store, S3DocumentStore)
    assert store.target.base_uri == "s3://b/downloads/src"


def test_factory_passes_storage_options_when_remote(monkeypatch, memory_store):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", "b")
    monkeypatch.setattr(agent_store.settings, "s3_region", "xx")
    store = get_document_store("src")
    assert store.target.storage_options["region"] == "xx"


def test_factory_omits_storage_options_when_local(monkeypatch, tmp_path):
    """A local target carries none, even with credentials configured."""
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", None)
    monkeypatch.setattr(agent_store.settings, "s3_region", "xx")
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path))
    assert get_document_store("src").target.storage_options == {}


def test_factory_download_dir_override_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", None)
    store = get_document_store("src", download_dir=str(tmp_path / "override"))
    assert store.target.root == tmp_path / "override" / "src"
