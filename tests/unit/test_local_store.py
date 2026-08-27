"""Tests for soliplex.agents.local_store (filesystem document writer)."""

import asyncio
import json
import logging

import pytest

from soliplex.agents import local_store
from soliplex.agents import store as agent_store


@pytest.fixture
def dl(tmp_path, monkeypatch):
    """Point download_dir at a temp directory."""
    d = tmp_path / "downloads"
    monkeypatch.setattr(agent_store.settings, "download_dir", str(d))
    return d


# --- sanitize_source ---


def test_sanitize_source_replaces_colons():
    assert local_store.sanitize_source("gitea:admin:myrepo:files") == "gitea_admin_myrepo_files"


def test_sanitize_source_collapses_and_strips():
    assert local_store.sanitize_source("a//b::c") == "a_b_c"


def test_sanitize_source_empty_fallback():
    assert local_store.sanitize_source(":::") == "source"


# --- uri_to_relpath ---


def test_uri_to_relpath_plain_file():
    assert local_store.uri_to_relpath("docs/readme.md").as_posix() == "docs/readme.md"


def test_uri_to_relpath_appends_extension_for_local_file():
    # Extension is now reconciled for every source, not just URLs/issues.
    rel = local_store.uri_to_relpath("docs/report", mime_type="application/pdf")
    assert rel.as_posix() == "docs/report.pdf"


def test_uri_to_relpath_replaces_wrong_extension():
    rel = local_store.uri_to_relpath("docs/report.bin", mime_type="application/pdf")
    assert rel.as_posix() == "docs/report.pdf"


def test_uri_to_relpath_strips_leading_slash():
    assert local_store.uri_to_relpath("/docs/readme.md").as_posix() == "docs/readme.md"


def test_uri_to_relpath_drops_traversal():
    assert local_store.uri_to_relpath("../../etc/passwd").as_posix() == "etc/passwd"


def test_uri_to_relpath_issue_gets_markdown(monkeypatch):
    rel = local_store.uri_to_relpath("/owner/repo/issues/12", mime_type="text/markdown")
    assert rel.as_posix() == "owner/repo/issues/12.md"


def test_uri_to_relpath_url_with_filename():
    rel = local_store.uri_to_relpath("https://example.com/a/b.html", mime_type="text/html")
    assert rel.as_posix() == "example.com/a/b.html"


def test_uri_to_relpath_url_no_filename_gets_index():
    rel = local_store.uri_to_relpath("https://example.com/", mime_type="text/html")
    assert rel.as_posix() == "example.com/index.html"


def test_uri_to_relpath_extensionless_file_preserved():
    # Not a URL and not an issue -> name is left untouched.
    assert local_store.uri_to_relpath("LICENSE").as_posix() == "LICENSE"


def test_uri_to_relpath_blank_segment_becomes_underscore():
    # A segment that sanitizes to empty (only dots/spaces) becomes "_".
    assert local_store.uri_to_relpath("a/   /b").as_posix() == "a/_/b"


def test_uri_to_relpath_reserved_name_prefixed():
    # Windows reserved device names are prefixed to stay safe.
    assert local_store.uri_to_relpath("CON/file.md").as_posix() == "_CON/file.md"


def test_uri_to_relpath_url_no_filename_no_mime():
    # No mime → no synthesized extension on the index file.
    assert local_store.uri_to_relpath("https://x.com/", mime_type=None).as_posix() == "x.com/index"


def test_uri_to_relpath_url_no_filename_guessed_ext():
    # Extension derived from mimetypes for a type without an override.
    assert local_store.uri_to_relpath("https://x.com/", mime_type="application/pdf").as_posix() == "x.com/index.pdf"


def test_uri_to_relpath_empty_uri_becomes_index():
    assert local_store.uri_to_relpath("/").as_posix() == "index"


# --- write_document / delete_document ---


@pytest.mark.asyncio
async def test_write_document_writes_file_and_sidecar(dl):
    target = await local_store.write_document(
        "gitea:admin:r:all",
        "docs/readme.md",
        b"hello",
        "text/markdown",
        {"last_commit_sha": "abc"},
    )
    assert target.read_bytes() == b"hello"
    assert target == dl / "gitea_admin_r_all" / "docs" / "readme.md"

    sidecar = target.with_name(target.name + ".meta.json")
    meta = json.loads(sidecar.read_text())
    assert meta["mime_type"] == "text/markdown"
    assert meta["source"] == "gitea:admin:r:all"
    assert meta["source_uri"] == "docs/readme.md"
    assert meta["size"] == 5
    assert meta["sha256"]
    assert meta["metadata"] == {"last_commit_sha": "abc"}
    # ingestion_type defaults to None; source_url is omitted unless provided.
    assert meta["ingestion_type"] is None
    assert "source_url" not in meta


@pytest.mark.asyncio
async def test_write_document_records_ingestion_type_and_source_url(dl):
    target = await local_store.write_document(
        "webdav:host",
        "docs/readme.md",
        b"hello",
        "text/markdown",
        {},
        ingestion_type="webdav",
        source_url="https://dav.example.com/docs/readme.md",
    )
    sidecar = target.with_name(target.name + ".meta.json")
    meta = json.loads(sidecar.read_text())
    assert meta["ingestion_type"] == "webdav"
    assert meta["source_url"] == "https://dav.example.com/docs/readme.md"


@pytest.mark.asyncio
async def test_write_document_accepts_str(dl):
    target = await local_store.write_document("s", "a.txt", "hi", "text/plain", {})
    assert target.read_bytes() == b"hi"


@pytest.mark.asyncio
async def test_write_document_issue_markdown(dl):
    target = await local_store.write_document("s:issues", "/o/r/issues/3", b"# t", "text/markdown", {"state": "open"})
    assert target == dl / "s_issues" / "o" / "r" / "issues" / "3.md"


@pytest.mark.asyncio
async def test_delete_document_removes_file_and_sidecar(dl):
    target = await local_store.write_document("s", "docs/x.md", b"x", "text/markdown", {})
    sidecar = target.with_name(target.name + ".meta.json")
    assert target.exists()
    assert sidecar.exists()

    await local_store.delete_document("s", "docs/x.md", mime_type="text/markdown")

    assert not target.exists()
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_delete_document_tolerates_a_missing_document(dl):
    """Idempotent: absence is not an error, and is not reported."""
    assert await local_store.delete_document("s", "nope.md") is None


# --- rename logging -------------------------------------------------------

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.asyncio
async def test_write_warns_when_detection_overrides_the_uri_extension(dl, caplog):
    """The OOXML misfiling was invisible in the log; now it is not."""
    with caplog.at_level(logging.WARNING, logger="soliplex.agents.local_store"):
        await local_store.write_document("s", "/team/report.pptx", b"PK", DOCX, {})

    assert "renaming /team/report.pptx to team/report.docx" in caplog.text
    assert ".pptx" in caplog.text


@pytest.mark.asyncio
async def test_write_logs_a_derived_extension_at_info(dl, caplog):
    """Supplying a missing extension is routine, not a warning."""
    with caplog.at_level(logging.DEBUG, logger="soliplex.agents.local_store"):
        await local_store.write_document("s", "/team/handout", b"%PDF-", "application/pdf", {})

    records = [r for r in caplog.records if "handout" in r.message and "naming" in r.message]
    assert records
    assert records[0].levelno == logging.INFO


@pytest.mark.asyncio
async def test_write_says_nothing_when_the_name_already_agrees(dl, caplog):
    with caplog.at_level(logging.DEBUG, logger="soliplex.agents.local_store"):
        await local_store.write_document("s", "/team/notes.pdf", b"%PDF-", "application/pdf", {})

    assert "renaming" not in caplog.text
    assert "naming" not in caplog.text


# --- batching -------------------------------------------------------------


def _watch(store, name):
    """Replace store.<name> with a wrapper recording peak concurrency."""
    state = {"peak": 0, "in_flight": 0}
    method = getattr(store, name)

    async def inner(*args):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        # Yield, so a serialized caller cannot look concurrent.
        await asyncio.sleep(0)
        try:
            return await method(*args)
        finally:
            state["in_flight"] -= 1

    setattr(store, name, inner)
    return state


@pytest.mark.asyncio
async def test_write_document_writes_document_and_sidecar_together(dl, monkeypatch):
    """A document costs one round trip against object storage, not one per object."""
    store = agent_store.get_document_store("s")
    monkeypatch.setattr(agent_store, "get_document_store", lambda *a, **k: store)
    seen = _watch(store, "write")

    await local_store.write_document("s", "a/b.pdf", b"%PDF-", "application/pdf", {})

    assert seen["peak"] == 2
    assert (store.target.root / "a/b.pdf").exists()
    assert (store.target.root / "a/b.pdf.meta.json").exists()


@pytest.mark.asyncio
async def test_delete_document_deletes_document_and_sidecar_together(dl, monkeypatch):
    store = agent_store.get_document_store("s")
    monkeypatch.setattr(agent_store, "get_document_store", lambda *a, **k: store)
    await local_store.write_document("s", "a/b.pdf", b"%PDF-", "application/pdf", {})
    seen = _watch(store, "delete")

    await local_store.delete_document("s", "a/b.pdf", mime_type="application/pdf")

    assert seen["peak"] == 2
    assert not (store.target.root / "a/b.pdf").exists()
    assert not (store.target.root / "a/b.pdf.meta.json").exists()
