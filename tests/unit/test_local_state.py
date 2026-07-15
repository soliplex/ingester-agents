"""Tests for soliplex.agents.local_state (per-source SQLite sync state)."""

import datetime
import sqlite3

import pytest

from soliplex.agents import local_state
from soliplex.agents import local_store


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    """Point state_dir and download_dir at temp directories."""
    monkeypatch.setattr(local_state.settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(local_store.settings, "download_dir", str(tmp_path / "dl"))
    return tmp_path


# --- file state ---


def test_load_file_state_empty(state_env):
    assert local_state.load_file_state("s") == {}


def test_upsert_and_load_file(state_env):
    local_state.upsert_file("s", "docs/a.md", "sha1", etag="e1", size=3, mime_type="text/markdown")
    state = local_state.load_file_state("s")
    assert state["docs/a.md"] == {"sha256": "sha1", "etag": "e1", "size": 3, "mime_type": "text/markdown"}


def test_upsert_replaces(state_env):
    local_state.upsert_file("s", "a", "sha1")
    local_state.upsert_file("s", "a", "sha2")
    assert local_state.load_file_state("s")["a"]["sha256"] == "sha2"


def test_delete_file(state_env):
    local_state.upsert_file("s", "a", "sha1")
    local_state.delete_file("s", "a")
    assert "a" not in local_state.load_file_state("s")


# --- compute_to_process ---


def test_compute_to_process_new_and_changed(state_env):
    local_state.upsert_file("s", "a", "sha1")
    local_state.upsert_file("s", "b", "sha-b")
    inventory = [
        {"uri": "a", "sha256": "sha1"},  # unchanged -> skipped
        {"uri": "b", "sha256": "sha-b-new"},  # changed -> processed
        {"uri": "c", "sha256": "sha-c"},  # new -> processed
        {"path": "d", "sha256": None},  # no hash -> always processed
    ]
    result = [local_state._uri_of(r) for r in local_state.compute_to_process(inventory, "s")]
    assert result == ["b", "c", "d"]


def test_compute_to_process_skips_rows_without_uri(state_env):
    assert local_state.compute_to_process([{"sha256": "x"}], "s") == []


# --- prune ---


def test_prune_files_returns_removed(state_env):
    local_state.upsert_file("s", "a", "1")
    local_state.upsert_file("s", "b", "2")
    removed = local_state.prune_files("s", {"a"})
    assert removed == ["b"]
    assert set(local_state.load_file_state("s")) == {"a"}


def test_prune_files_nothing_removed(state_env):
    local_state.upsert_file("s", "a", "1")
    local_state.upsert_file("s", "b", "2")
    assert local_state.prune_files("s", {"a", "b"}) == []


def test_load_file_state_db_error_returns_empty(state_env, monkeypatch):
    def boom(_source):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(local_state, "_get_connection", boom)
    assert local_state.load_file_state("s") == {}


def test_get_sync_meta_db_error_returns_default(state_env, monkeypatch):
    def boom(_source):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(local_state, "_get_connection", boom)
    assert local_state.get_sync_meta("s")["last_commit_sha"] is None


def test_get_sync_meta_tolerates_corrupt_fields(state_env):
    local_state.set_sync_meta("s", "c1")
    db = local_state.get_state_path("s")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE sync SET last_sync_date = ?, metadata = ? WHERE id = 1", ("not-a-date", "{bad json"))
    conn.commit()
    conn.close()

    meta = local_state.get_sync_meta("s")
    assert meta["last_sync_date"] is None
    assert meta["metadata"] == {}


def test_prune_documents_deletes_files_and_state(state_env):
    local_store.write_document("s", "a.md", b"a", "text/markdown", {})
    local_store.write_document("s", "b.md", b"b", "text/markdown", {})
    local_state.upsert_file("s", "a.md", "1", mime_type="text/markdown")
    local_state.upsert_file("s", "b.md", "2", mime_type="text/markdown")

    removed = local_state.prune_documents("s", {"a.md"})
    assert removed == ["b.md"]
    assert (local_store.source_dir("s") / "a.md").exists()
    assert not (local_store.source_dir("s") / "b.md").exists()
    assert set(local_state.load_file_state("s")) == {"a.md"}


# --- reconcile_documents ---


def test_reconcile_documents_removes_delisted(state_env):
    # a.md (top level) and sub/c.md survive; b.md is delisted and removed.
    local_store.write_document("s", "a.md", b"a", "text/markdown", {})
    local_store.write_document("s", "sub/c.md", b"c", "text/markdown", {})
    local_store.write_document("s", "b.md", b"b", "text/markdown", {})
    for uri, sha in (("a.md", "1"), ("sub/c.md", "2"), ("b.md", "3")):
        local_state.upsert_file("s", uri, sha, mime_type="text/markdown")

    removed = local_state.reconcile_documents("s", {"a.md", "sub/c.md"})

    assert removed == ["b.md"]
    base = local_store.source_dir("s")
    assert (base / "a.md").exists()
    assert (base / "sub" / "c.md").exists()
    assert not (base / "b.md").exists()
    assert not (base / "b.md.meta.json").exists()
    assert set(local_state.load_file_state("s")) == {"a.md", "sub/c.md"}


def test_reconcile_documents_sweeps_disk_orphan(state_env):
    # A file present on disk with no state row is swept even though the state
    # comparison alone would never touch it.
    local_store.write_document("s", "a.md", b"a", "text/markdown", {})
    local_state.upsert_file("s", "a.md", "1", mime_type="text/markdown")
    base = local_store.source_dir("s")
    (base / "orphan.bin").write_bytes(b"x")

    removed = local_state.reconcile_documents("s", {"a.md"})

    assert removed == ["orphan.bin"]
    assert (base / "a.md").exists()
    assert (base / "a.md.meta.json").exists()
    assert not (base / "orphan.bin").exists()


def test_reconcile_documents_keeps_current(state_env):
    local_store.write_document("s", "a.md", b"a", "text/markdown", {})
    local_state.upsert_file("s", "a.md", "1", mime_type="text/markdown")

    removed = local_state.reconcile_documents("s", {"a.md"})

    assert removed == []
    assert (local_store.source_dir("s") / "a.md").exists()


def test_reconcile_documents_no_download_dir(state_env):
    # No files ever written: the source folder doesn't exist, so the disk
    # sweep is skipped and nothing is removed.
    removed = local_state.reconcile_documents("s", set())
    assert removed == []


# --- sync meta ---


def test_get_sync_meta_default(state_env):
    meta = local_state.get_sync_meta("s")
    assert meta == {
        "source_id": "s",
        "last_commit_sha": None,
        "last_sync_date": None,
        "branch": "main",
        "metadata": {},
    }


def test_set_and_get_sync_meta(state_env):
    when = datetime.datetime(2026, 6, 10, 12, 0, 0)
    local_state.set_sync_meta("s", "commitX", branch="dev", last_sync_date=when, metadata={"n": 1})
    meta = local_state.get_sync_meta("s")
    assert meta["last_commit_sha"] == "commitX"
    assert meta["branch"] == "dev"
    assert meta["last_sync_date"] == when
    assert meta["metadata"] == {"n": 1}


def test_set_sync_meta_overwrites(state_env):
    local_state.set_sync_meta("s", "c1")
    local_state.set_sync_meta("s", "c2")
    assert local_state.get_sync_meta("s")["last_commit_sha"] == "c2"


# --- reset ---


def test_reset_state(state_env):
    local_state.upsert_file("s", "a", "1")
    assert local_state.reset_state("s") is True
    assert local_state.load_file_state("s") == {}


def test_reset_state_missing(state_env):
    assert local_state.reset_state("never") is False
