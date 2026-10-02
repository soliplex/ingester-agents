"""Tests for the haiku-rag metadata providers -- 100% branch coverage required.

The PDFs are real (``tests/fixtures/pdf``, see its README) so pdfium's own
page counting, version and information-dictionary reading are exercised. The
sidecar provider reads sidecars the agent's own sidecar facade wrote into a
local download store under ``tmp_path``.
"""

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from haiku.rag.converters.pdf_split import PDFIUM_LOCK
from haiku.rag.ingester.metadata import build_providers
from haiku.rag.ingester.metadata import load_metadata_providers
from haiku.rag.sources.base import FetchResult

from soliplex.agents import haiku_metadata
from soliplex.agents import store as agent_store
from soliplex.agents.config import settings
from soliplex.agents.haiku_metadata import PdfMetadataProvider
from soliplex.agents.haiku_metadata import SidecarMetadataProvider
from soliplex.agents.haiku_metadata import SoliplexMetadataProvider
from soliplex.agents.haiku_metadata import is_pdf
from soliplex.agents.haiku_metadata import parse_pdf_date
from soliplex.agents.haiku_metadata import read_pdf_metadata
from soliplex.agents.sidecar import DocumentWrite
from soliplex.agents.sidecar import Sidecars

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pdf"


def _result(body: bytes, content_type="application/pdf", uri="file:///docs/doc.pdf") -> FetchResult:
    return FetchResult(
        uri=uri,
        body=body,
        content_type=content_type,
        content_hash=hashlib.md5(body, usedforsecurity=False).hexdigest(),
    )


# --- is_pdf ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type, body, expected",
    [
        ("application/pdf", b"anything", True),
        ("Application/PDF; charset=binary", b"anything", True),
        ("application/octet-stream", b"%PDF-1.7\n...", True),
        (None, b"\x00" * 100 + b"%PDF-1.4", True),
        ("application/octet-stream", b"\x00" * 1024 + b"%PDF-1.4", False),
        ("text/markdown", b"# Title", False),
        ("", b"", False),
    ],
)
def test_is_pdf(content_type, body, expected):
    assert is_pdf(content_type, body) is expected


# --- parse_pdf_date ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("D:20240115093000-05'00'", "2024-01-15T09:30:00-05:00"),
        ("D:20240115093000+05'30'", "2024-01-15T09:30:00+05:30"),
        ("D:20240115093000+05", "2024-01-15T09:30:00+05:00"),
        ("D:20240115093000Z", "2024-01-15T09:30:00+00:00"),
        ("D:20240115093000Z00'00'", "2024-01-15T09:30:00+00:00"),
        ("20240115093000", "2024-01-15T09:30:00"),
        ("D:2024", "2024-01-01T00:00:00"),
        ("D:20240220", "2024-02-20T00:00:00"),
        (" D:20240220 ", "2024-02-20T00:00:00"),
        # Not dates: returned as written.
        ("D:20241340", "D:20241340"),
        ("Tuesday", "Tuesday"),
        ("D:2024-01-15", "D:2024-01-15"),
    ],
)
def test_parse_pdf_date(value, expected):
    assert parse_pdf_date(value) == expected


# --- read_pdf_metadata -------------------------------------------------------------


def test_reads_page_count_version_and_information():
    assert read_pdf_metadata((FIXTURES / "with_metadata.pdf").read_bytes()) == {
        "page_count": 3,
        "pdf_version": "1.4",
        "pdf_title": "Quarterly Report",
        "pdf_author": "Jane Doe",
        "pdf_subject": "Finance",
        "pdf_keywords": "budget, forecast",
        "pdf_creator": "Writer",
        "pdf_producer": "LibreOffice 7.6",
        "pdf_creation_date": "2024-01-15T09:30:00-05:00",
        "pdf_mod_date": "2024-02-20T00:00:00",
    }


def test_leaves_out_empty_information():
    metadata = read_pdf_metadata((FIXTURES / "valid.pdf").read_bytes())
    assert metadata["page_count"] == 1
    assert metadata["pdf_version"] == "1.7"
    assert "pdf_title" not in metadata
    assert "pdf_author" not in metadata


def test_owner_restricted_pdf_is_readable():
    assert read_pdf_metadata((FIXTURES / "owner_only.pdf").read_bytes())["page_count"] == 1


def test_leaves_out_blank_information_and_unknown_version():
    pdf_document = haiku_metadata.pdfium.PdfDocument
    with (
        patch.object(pdf_document, "get_version", return_value=None),
        patch.object(pdf_document, "get_metadata_dict", return_value={"Title": "  ", "Author": " Jane "}),
    ):
        metadata = read_pdf_metadata((FIXTURES / "valid.pdf").read_bytes())
    assert metadata == {"page_count": 1, "pdf_author": "Jane"}


@pytest.mark.parametrize("name", ["user_password.pdf", "truncated.pdf"])
def test_unopenable_pdf_raises(name):
    with pytest.raises(haiku_metadata.pdfium.PdfiumError):
        read_pdf_metadata((FIXTURES / name).read_bytes())


def test_holds_the_pdfium_lock_and_closes_the_pdf():
    real = haiku_metadata.pdfium.PdfDocument
    opened = []

    def spy(body):
        assert PDFIUM_LOCK.locked()
        pdf = real(body)
        opened.append(pdf)
        return pdf

    with patch.object(haiku_metadata.pdfium, "PdfDocument", side_effect=spy):
        read_pdf_metadata((FIXTURES / "valid.pdf").read_bytes())
    (pdf,) = opened
    # pypdfium2 drops its raw handle on close.
    assert pdf.raw is None
    assert not PDFIUM_LOCK.locked()


# --- PdfMetadataProvider -----------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_returns_pdf_metadata():
    result = _result((FIXTURES / "with_metadata.pdf").read_bytes())
    metadata = await PdfMetadataProvider()("src", result.uri, result)
    assert metadata["page_count"] == 3
    assert metadata["pdf_title"] == "Quarterly Report"


@pytest.mark.asyncio
async def test_provider_detects_pdf_by_header():
    result = _result((FIXTURES / "valid.pdf").read_bytes(), content_type="application/octet-stream")
    assert (await PdfMetadataProvider()("src", result.uri, result))["page_count"] == 1


@pytest.mark.asyncio
async def test_provider_ignores_other_documents():
    result = _result(b"# Title\n", content_type="text/markdown")
    with patch.object(haiku_metadata, "read_pdf_metadata") as read:
        assert await PdfMetadataProvider()("src", result.uri, result) == {}
    read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["user_password.pdf", "truncated.pdf"])
async def test_provider_returns_nothing_for_unopenable_pdf(name, caplog):
    result = _result((FIXTURES / name).read_bytes())
    assert await PdfMetadataProvider()("src", result.uri, result) == {}
    assert f"cannot read PDF metadata of {result.uri} (source src)" in caplog.text


@pytest.mark.asyncio
async def test_provider_is_registered_with_haiku_rag():
    providers = build_providers([("src", "soliplex-pdf-metadata")], load_metadata_providers())
    provider = providers["src"]
    assert isinstance(provider, PdfMetadataProvider)
    result = _result((FIXTURES / "with_metadata.pdf").read_bytes())
    assert (await provider("src", result.uri, result))["page_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, provider_class",
    [
        ("soliplex-sidecar-metadata", SidecarMetadataProvider),
        ("soliplex-metadata", SoliplexMetadataProvider),
    ],
)
async def test_other_providers_are_registered_with_haiku_rag(name, provider_class):
    assert isinstance(build_providers([("src", name)], load_metadata_providers())["src"], provider_class)


# --- SidecarMetadataProvider -------------------------------------------------------

# A raw source as a manifest names it; haiku-ingester sees it sanitized.
RAW_SOURCE = "gitea:admin:repo"
SOURCE_ID = "gitea_admin_repo"


@pytest.fixture
def download_store(tmp_path, monkeypatch):
    """The agent's local download store under *tmp_path*, as the load sees it."""
    monkeypatch.setattr(settings, "download_dir", str(tmp_path / "downloads"), raising=False)
    monkeypatch.setattr(settings, "download_s3_bucket", None, raising=False)
    agent_store.reset_store_cache()
    yield agent_store.get_document_store(RAW_SOURCE)
    agent_store.reset_store_cache()


async def _store_document(store, key: str, content: bytes, **metadata) -> str:
    """Write *content* and its sidecar the way ``write_document`` does; return its URI."""
    await store.write(key, content)
    await Sidecars(store).write_all(
        key,
        DocumentWrite(
            source=RAW_SOURCE,
            uri=f"docs/{key}",
            content=content,
            mime_type="application/pdf",
            metadata=metadata,
            ingestion_type="scm",
            source_url=f"https://git.example.com/{key}",
            downloaded_time="2026-10-01T12:00:00+00:00",
        ),
    )
    return store.uri(key)


@pytest.mark.asyncio
async def test_sidecar_provider_returns_the_flattened_sidecar(download_store):
    content = (FIXTURES / "valid.pdf").read_bytes()
    uri = await _store_document(download_store, "sub/a.pdf", content, team="ops", tags=["x", "y"])

    metadata = await SidecarMetadataProvider()(SOURCE_ID, uri, _result(content, uri=uri))

    assert metadata == {
        "mime_type": "application/pdf",
        "source": RAW_SOURCE,
        "source_uri": "docs/sub/a.pdf",
        "ingestion_type": "scm",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "source_url": "https://git.example.com/sub/a.pdf",
        "downloaded_time": "2026-10-01T12:00:00+00:00",
        "team": "ops",
        "tags": '["x", "y"]',
    }


@pytest.mark.asyncio
async def test_sidecar_provider_without_a_sidecar_returns_nothing(download_store, caplog):
    await download_store.write("bare.md", b"# bare")
    uri = download_store.uri("bare.md")
    with caplog.at_level(logging.INFO):
        assert await SidecarMetadataProvider()(SOURCE_ID, uri, _result(b"# bare", "text/markdown", uri)) == {}
    assert f"no sidecar for {uri} (source {SOURCE_ID})" in caplog.text


@pytest.mark.asyncio
async def test_sidecar_provider_for_a_uri_outside_the_store_returns_nothing(download_store):
    uri = "file:///elsewhere/doc.md"
    assert await SidecarMetadataProvider()(SOURCE_ID, uri, _result(b"x", "text/markdown", uri)) == {}


@pytest.mark.asyncio
async def test_sidecar_provider_read_failure_returns_nothing(download_store, caplog):
    uri = download_store.uri("a.md")
    with patch.object(Sidecars, "read_for_uri", side_effect=PermissionError("denied")):
        assert await SidecarMetadataProvider()(SOURCE_ID, uri, _result(b"x", "text/markdown", uri)) == {}
    assert f"cannot read the sidecar of {uri} (source {SOURCE_ID}): denied" in caplog.text


@pytest.mark.asyncio
async def test_sidecar_provider_malformed_sidecar_returns_nothing(download_store):
    await download_store.write("a.md", b"x")
    await Sidecars(download_store).write("a.md", "meta", b"not json")
    uri = download_store.uri("a.md")
    assert await SidecarMetadataProvider()(SOURCE_ID, uri, _result(b"x", "text/markdown", uri)) == {}


def test_sidecar_provider_reuses_one_facade_per_source(download_store):
    provider = SidecarMetadataProvider()
    first = provider._sidecars_for(SOURCE_ID)
    assert provider._sidecars_for(SOURCE_ID) is first
    # The sanitized id names the same place the raw manifest source does.
    assert first.store.target.base_uri == download_store.target.base_uri
    assert provider._sidecars_for("other") is not first


# --- SoliplexMetadataProvider ------------------------------------------------------


@pytest.mark.asyncio
async def test_combined_provider_merges_both_with_the_sidecar_last(download_store):
    content = (FIXTURES / "with_metadata.pdf").read_bytes()
    uri = await _store_document(download_store, "report.pdf", content, pdf_title="Operator title")

    metadata = await SoliplexMetadataProvider()(SOURCE_ID, uri, _result(content, uri=uri))

    assert metadata["page_count"] == 3
    assert metadata["pdf_author"] == "Jane Doe"
    assert metadata["source_uri"] == "docs/report.pdf"
    # Manifest metadata wins a clash with what the PDF says.
    assert metadata["pdf_title"] == "Operator title"


@pytest.mark.asyncio
async def test_combined_provider_for_a_non_pdf_without_a_sidecar(download_store):
    uri = download_store.uri("notes.md")
    assert await SoliplexMetadataProvider()(SOURCE_ID, uri, _result(b"# notes", "text/markdown", uri)) == {}
