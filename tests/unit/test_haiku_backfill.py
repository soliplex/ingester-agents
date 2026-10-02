"""Tests for the haiku-rag metadata back-fill -- 100% branch coverage required.

:func:`backfill` is driven with stand-ins for the client, sources and
providers, to reach every branch. :func:`main` runs for real: a LanceDB seeded
with pre-embedded chunks (so no embedder is called), the haiku ``fs`` source,
the ``soliplex-pdf-metadata`` provider through its entry point, and the
``--missing`` scope evaluated by LanceDB itself. PDF attachments are seeded
exactly as haiku-rag's ``_reconcile_pdf_attachments`` stores them, from
``tests/fixtures/pdf/with_attachments.pdf``.
"""

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from haiku.rag.sources.base import FetchResult

from soliplex.agents import haiku_backfill
from soliplex.agents.haiku_backfill import Summary
from soliplex.agents.haiku_backfill import attachment_uri
from soliplex.agents.haiku_backfill import backfill
from soliplex.agents.haiku_backfill import backfills
from soliplex.agents.haiku_backfill import compose_filter
from soliplex.agents.haiku_backfill import extract_attachments
from soliplex.agents.haiku_backfill import missing_filter

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pdf"


def _md5(body: bytes) -> str:
    return hashlib.md5(body, usedforsecurity=False).hexdigest()


def _doc(uri, **metadata):
    return SimpleNamespace(id=f"id-{uri}", uri=uri, metadata=metadata)


class _Client:
    """Pages through *docs*; records each listing's filter.

    *hidden* documents are indexed but outside the listing's scope: found by
    URI only, as a parent the ``--missing`` scope left out would be.
    """

    def __init__(self, *docs, hidden=()):
        self.docs = list(docs)
        self.by_uri = {doc.uri: doc for doc in [*docs, *hidden]}
        self.filters = []
        self.looked_up = []
        self.update_document = AsyncMock()

    async def list_documents(self, limit=None, offset=None, filter=None):
        self.filters.append(filter)
        return self.docs[offset : offset + limit]

    async def get_document_by_uri(self, uri):
        self.looked_up.append(uri)
        return self.by_uri.get(uri)


class _Source:
    """Serves *bodies* by URI; a missing one raises like a vanished file."""

    def __init__(self, **bodies):
        self.bodies = bodies
        self.fetched = []

    async def fetch(self, uri):
        self.fetched.append(uri)
        body = self.bodies[uri]
        return FetchResult(uri=uri, body=body, content_type="application/pdf", content_hash=_md5(body))


def _provider(returned):
    return AsyncMock(return_value=returned)


# --- missing_filter / compose_filter -------------------------------------------------


def test_missing_filter():
    assert missing_filter(["page_count", "pdf.title-x"]) == (
        "(metadata IS NULL OR metadata NOT LIKE '%\"page_count\"%' OR metadata NOT LIKE '%\"pdf.title-x\"%')"
    )


def test_missing_filter_without_keys():
    assert missing_filter([]) is None


@pytest.mark.parametrize("key", ["it's", "a b", "x%", "", 'q"'])
def test_missing_filter_refuses_keys_unsafe_in_sql(key):
    with pytest.raises(ValueError, match="may only contain"):
        missing_filter([key])


@pytest.mark.parametrize(
    "clauses, expected",
    [
        ((), None),
        ((None, None), None),
        (("a = 1", None), "a = 1"),
        (("a = 1", "b OR c"), "(a = 1) AND (b OR c)"),
    ],
)
def test_compose_filter(clauses, expected):
    assert compose_filter(*clauses) == expected


# --- backfill ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merges_provider_metadata_without_reserved_keys():
    doc = _doc("a", source_id="s", md5=_md5(b"A"), content_type="application/pdf", keep="me")
    client = _Client(doc)
    provider = _provider({"page_count": 3, "md5": "forged", "content_type": "x", "source_id": "t", "source_revision": "r"})

    summary = await backfill(client, {"s": _Source(a=b"A")}, {"s": provider})

    client.update_document.assert_awaited_once_with(
        "id-a",
        metadata={"source_id": "s", "md5": _md5(b"A"), "content_type": "application/pdf", "keep": "me", "page_count": 3},
    )
    assert summary == Summary(scanned=1, updated=1)
    source_id, uri, result = provider.await_args.args
    assert (source_id, uri, result.body) == ("s", "a", b"A")


@pytest.mark.asyncio
async def test_check_writes_nothing(caplog):
    client = _Client(_doc("a", source_id="s"))
    with caplog.at_level(logging.INFO):
        summary = await backfill(client, {"s": _Source(a=b"A")}, {"s": _provider({"page_count": 1})}, check=True)
    client.update_document.assert_not_awaited()
    assert summary == Summary(scanned=1, updated=1, check=True)
    assert "would back-fill a: page_count" in caplog.text


@pytest.mark.asyncio
async def test_changed_values_are_written():
    client = _Client(_doc("a", source_id="s", page_count=2))
    summary = await backfill(client, {"s": _Source(a=b"A")}, {"s": _provider({"page_count": 3})})
    assert summary.updated == 1
    assert client.update_document.await_args.kwargs["metadata"]["page_count"] == 3


@pytest.mark.asyncio
async def test_unchanged_metadata_is_not_written():
    client = _Client(_doc("a", source_id="s", md5=_md5(b"A"), page_count=3))
    summary = await backfill(client, {"s": _Source(a=b"A")}, {"s": _provider({"page_count": 3})})
    client.update_document.assert_not_awaited()
    assert summary == Summary(scanned=1, unchanged=1)


@pytest.mark.asyncio
async def test_changed_document_is_left_to_the_next_load():
    client = _Client(_doc("a", source_id="s", md5=_md5(b"old")))
    provider = _provider({"page_count": 1})
    summary = await backfill(client, {"s": _Source(a=b"new")}, {"s": provider})
    provider.assert_not_awaited()
    client.update_document.assert_not_awaited()
    assert summary == Summary(scanned=1, stale=1)


@pytest.mark.asyncio
async def test_documents_without_a_provider_or_source_are_counted_and_left():
    client = _Client(
        _doc("no-source-id"),
        _doc("no-provider", source_id="plain"),
        _doc("unknown-source", source_id="gone"),
    )
    client.docs[0].metadata = None
    sources = {"plain": _Source(), "s": _Source()}
    summary = await backfill(client, sources, {"s": _provider({}), "gone": _provider({})})
    assert summary == Summary(scanned=3, skipped_no_provider=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata, missing, content_types, selected",
    [
        ({}, [], [], True),
        ({"page_count": 1}, ["page_count"], [], False),
        ({"page_count": 1}, ["page_count", "pdf_title"], [], True),
        ({"content_type": "Application/PDF"}, [], ["application/pdf"], True),
        ({"content_type": "text/markdown"}, [], ["application/pdf"], False),
        ({}, [], ["application/pdf"], False),
        ({"content_type": "application/pdf", "page_count": 1}, ["page_count"], ["application/pdf"], False),
    ],
)
async def test_selection(metadata, missing, content_types, selected):
    client = _Client(_doc("a", source_id="s", **metadata))
    summary = await backfill(
        client, {"s": _Source(a=b"A")}, {"s": _provider({"x": 1})}, missing=missing, content_types=content_types
    )
    assert summary.updated == int(selected)
    assert summary.skipped_not_selected == int(not selected)


@pytest.mark.asyncio
async def test_listing_is_scoped_by_missing_and_filter():
    client = _Client(_doc("a", source_id="s"))
    await backfill(client, {"s": _Source(a=b"A")}, {"s": _provider({})}, missing=["page_count"], doc_filter="uri LIKE '%a'")
    assert set(client.filters) == {f"({missing_filter(['page_count'])}) AND (uri LIKE '%a')"}


@pytest.mark.asyncio
async def test_unscoped_listing_has_no_filter():
    client = _Client()
    assert await backfill(client, {}, {}) == Summary()
    assert client.filters == [None]


@pytest.mark.asyncio
async def test_every_page_is_read_before_any_write():
    docs = [_doc(str(i), source_id="s") for i in range(5)]
    client = _Client(*docs)
    pages_read_at_first_write = []
    client.update_document.side_effect = lambda *a, **k: pages_read_at_first_write.append(len(client.filters))

    summary = await backfill(
        client, {"s": _Source(**{str(i): b"x" for i in range(5)})}, {"s": _provider({"n": 1})}, batch_size=2
    )

    assert summary.updated == 5
    # Pages of 2, 2, 1, then an empty one ends the listing -- all before a write.
    assert len(client.filters) == 4
    assert pages_read_at_first_write[0] == 4


@pytest.mark.asyncio
async def test_failures_are_recorded_and_the_rest_still_run(caplog):
    client = _Client(_doc("missing", source_id="s"), _doc("broken", source_id="t"), _doc("ok", source_id="s"))
    sources = {"s": _Source(ok=b"OK"), "t": _Source(broken=b"B")}
    providers = {"s": _provider({"page_count": 1}), "t": AsyncMock(side_effect=ValueError("bad"))}

    summary = await backfill(client, sources, providers)

    assert summary.updated == 1
    assert summary.errors == [
        {"uri": "missing", "error": "KeyError: 'missing'"},
        {"uri": "broken", "error": "ValueError: bad"},
    ]
    assert "cannot back-fill broken: bad" in caplog.text


@pytest.mark.asyncio
async def test_provider_gets_a_copy_of_the_fetch_result():
    client = _Client(_doc("a", source_id="s", md5=_md5(b"A")))

    async def mutating(source_id, uri, result):
        result.content_hash = "tampered"
        return {}

    summary = await backfill(client, {"s": _Source(a=b"A")}, {"s": mutating})
    assert summary.unchanged == 1


# --- provider opt-out ---------------------------------------------------------------


class _OptedOut:
    backfill = False

    def __init__(self):
        self.calls = 0

    async def __call__(self, source_id, uri, result):  # pragma: no cover - must never run
        self.calls += 1
        return {"ingested_time": "now"}


def test_backfills():
    assert backfills(object()) is True
    assert backfills(_OptedOut()) is False
    assert backfills(SimpleNamespace(backfill=True)) is True


@pytest.mark.asyncio
async def test_opted_out_provider_is_never_called_and_logged_once(caplog):
    provider = _OptedOut()
    client = _Client(_doc("a", source_id="s"), _doc("b", source_id="s"))
    with caplog.at_level(logging.INFO):
        summary = await backfill(client, {"s": _Source(a=b"A", b=b"B")}, {"s": provider})
    assert provider.calls == 0
    assert summary == Summary(scanned=2, skipped_provider_opt_out=2)
    assert caplog.text.count("s's metadata provider sets backfill = False") == 1


# --- extract_attachments -------------------------------------------------------------

ROOT = "file:///docs/bundle.pdf"
BUNDLE = (FIXTURES / "with_attachments.pdf").read_bytes()


def test_attachment_uri_percent_encodes_the_name():
    assert attachment_uri(ROOT, "a b%/c.pdf") == f"{ROOT}#attachment=a%20b%25%2Fc.pdf"


def test_extract_attachments_matches_haiku_rag():
    """Same URIs, names, bytes, content types and hashes as ingestion stores."""
    from haiku.rag.client.documents import _extract_pdf_attachments

    ours = extract_attachments(BUNDLE, ROOT)
    theirs = _extract_pdf_attachments(BUNDLE, ROOT, depth=0)
    assert {uri: (a.name, a.data, a.content_type, a.content_hash) for uri, a in ours.items()} == theirs
    assert sorted(ours) == [f"{ROOT}#attachment=inner%20report.pdf", f"{ROOT}#attachment=readme.txt"]


def test_extract_attachments_of_a_pdf_without_any():
    assert extract_attachments((FIXTURES / "valid.pdf").read_bytes(), ROOT) == {}


def test_extract_attachments_of_an_unopenable_pdf():
    assert extract_attachments((FIXTURES / "truncated.pdf").read_bytes(), ROOT) is None


def test_extract_attachments_holds_the_pdfium_lock():
    import pypdfium2 as pdfium
    from haiku.rag.converters.pdf_split import PDFIUM_LOCK

    real = pdfium.PdfDocument

    def spy(body):
        assert PDFIUM_LOCK.locked()
        return real(body)

    with patch.object(pdfium, "PdfDocument", side_effect=spy):
        extract_attachments(BUNDLE, ROOT)
    assert not PDFIUM_LOCK.locked()


# --- attachments ---------------------------------------------------------------------

ATTACHED = extract_attachments(BUNDLE, ROOT)
INNER = f"{ROOT}#attachment=inner%20report.pdf"
README = f"{ROOT}#attachment=readme.txt"
NESTED = extract_attachments(ATTACHED[INNER].data, INNER)
DEEP = f"{INNER}#attachment=deep%20notes.txt"


def _root(**metadata):
    return _doc(ROOT, **({"source_id": "s", "md5": _md5(BUNDLE)} | metadata))


def _child(uri, parent=ROOT, attached=ATTACHED, **metadata):
    stored = {"parent_uri": parent, "md5": attached[uri].content_hash, "content_type": attached[uri].content_type}
    return _doc(uri, **(stored | metadata))


def _recorder(returned=None):
    """A provider that records what it is handed, answering *returned* (default: the body size)."""
    calls = []

    async def provider(source_id, uri, result):
        calls.append((source_id, uri, result))
        return returned if returned is not None else {"size_seen": len(result.body)}

    provider.calls = calls
    return provider


@pytest.mark.asyncio
async def test_attachments_are_filled_from_their_parents_bytes():
    provider = _recorder()
    source = _Source(**{ROOT: BUNDLE})
    client = _Client(_child(INNER), _child(README), hidden=[_root()])

    summary = await backfill(client, {"s": source}, {"s": provider})

    assert summary == Summary(scanned=2, updated=2, attachments_updated=2)
    # The parent is fetched once for both of its children.
    assert source.fetched == [ROOT]
    handed = {uri: (source_id, result) for source_id, uri, result in provider.calls}
    source_id, result = handed[INNER]
    assert source_id == "s"
    assert result.body == ATTACHED[INNER].data
    assert result.content_type == "application/pdf"
    assert result.content_hash == ATTACHED[INNER].content_hash
    assert result.extra_metadata == {"parent_uri": ROOT}
    written = {call.args[0]: call.kwargs["metadata"] for call in client.update_document.await_args_list}
    assert written[f"id-{README}"]["size_seen"] == len(ATTACHED[README].data)
    # Still owned by no source.
    assert "source_id" not in written[f"id-{README}"]


@pytest.mark.asyncio
async def test_nested_attachment_is_extracted_down_the_chain():
    provider = _recorder()
    inner = _child(INNER)
    deep = _child(DEEP, parent=INNER, attached=NESTED)
    client = _Client(deep, hidden=[_root(), inner])

    summary = await backfill(client, {"s": _Source(**{ROOT: BUNDLE})}, {"s": provider})

    assert summary.attachments_updated == 1
    ((_, uri, result),) = provider.calls
    assert (uri, result.body, result.extra_metadata) == (DEEP, NESTED[DEEP].data, {"parent_uri": INNER})
    assert client.looked_up == [INNER, ROOT]


@pytest.mark.asyncio
async def test_parent_and_children_in_one_listing():
    provider = _recorder()
    source = _Source(**{ROOT: BUNDLE})
    client = _Client(_root(), _child(README))
    summary = await backfill(client, {"s": source}, {"s": provider})
    assert (summary.updated, summary.attachments_updated) == (2, 1)
    assert source.fetched == [ROOT, ROOT]


@pytest.mark.asyncio
async def test_provider_cannot_reparent_an_attachment():
    client = _Client(_child(README), hidden=[_root()])
    await backfill(client, {"s": _Source(**{ROOT: BUNDLE})}, {"s": _recorder({"parent_uri": "x", "k": 1})})
    assert client.update_document.await_args.kwargs["metadata"]["parent_uri"] == ROOT


@pytest.mark.asyncio
async def test_no_attachments_leaves_them_unowned():
    provider = _recorder()
    client = _Client(_child(README), hidden=[_root()])
    summary = await backfill(client, {"s": _Source(**{ROOT: BUNDLE})}, {"s": provider}, attachments=False)
    assert summary == Summary(scanned=1, skipped_no_provider=1)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_attachment_whose_parent_is_gone_is_orphaned():
    client = _Client(_child(README))
    summary = await backfill(client, {"s": _Source()}, {"s": _recorder()})
    assert summary == Summary(scanned=1, orphaned=1)


@pytest.mark.asyncio
async def test_attachment_chain_longer_than_haiku_nests_is_orphaned():
    # a -> b -> c -> d: deeper than haiku-rag ever extracts.
    chain = [_doc("a", source_id="s"), _doc("b", parent_uri="a"), _doc("c", parent_uri="b"), _doc("d", parent_uri="c")]
    client = _Client(_doc("e", parent_uri="d"), hidden=chain)
    summary = await backfill(client, {"s": _Source()}, {"s": _recorder()})
    assert summary == Summary(scanned=1, orphaned=1)


@pytest.mark.asyncio
async def test_attachment_under_an_unowned_parent_is_skipped():
    client = _Client(_child(README), hidden=[_doc(ROOT)])
    client.by_uri[ROOT].metadata = None
    summary = await backfill(client, {"s": _Source()}, {"s": _recorder()})
    assert summary == Summary(scanned=1, skipped_no_provider=1)


@pytest.mark.asyncio
async def test_attachment_under_an_opted_out_provider_is_skipped():
    client = _Client(_child(README), hidden=[_root()])
    summary = await backfill(client, {"s": _Source()}, {"s": _OptedOut()})
    assert summary == Summary(scanned=1, skipped_provider_opt_out=1)


@pytest.mark.asyncio
async def test_attachment_not_selected():
    client = _Client(_child(README), hidden=[_root()])
    summary = await backfill(client, {"s": _Source()}, {"s": _recorder()}, content_types=["application/pdf"])
    assert summary == Summary(scanned=1, skipped_not_selected=1)


@pytest.mark.asyncio
async def test_parent_fetch_failure_fails_each_child():
    client = _Client(_child(INNER), _child(README), hidden=[_root()])
    summary = await backfill(client, {"s": _Source()}, {"s": _recorder()})
    assert [error["uri"] for error in summary.errors] == [INNER, README]


@pytest.mark.asyncio
async def test_changed_parent_makes_its_children_stale():
    client = _Client(_child(INNER), _child(README), hidden=[_root(md5="old")])
    provider = _recorder()
    summary = await backfill(client, {"s": _Source(**{ROOT: BUNDLE})}, {"s": provider})
    assert summary == Summary(scanned=2, stale=2)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_changed_attachment_is_stale():
    client = _Client(_child(README, md5="old"), hidden=[_root()])
    summary = await backfill(client, {"s": _Source(**{ROOT: BUNDLE})}, {"s": _recorder()})
    assert summary == Summary(scanned=1, stale=1)


@pytest.mark.asyncio
async def test_attachments_without_stored_hashes_are_filled():
    root = _root()
    root.metadata.pop("md5")
    child = _child(README)
    child.metadata.pop("md5")
    summary = await backfill(_Client(child, hidden=[root]), {"s": _Source(**{ROOT: BUNDLE})}, {"s": _recorder()})
    assert summary.attachments_updated == 1


@pytest.mark.asyncio
async def test_attachment_no_longer_embedded_is_orphaned(caplog):
    gone = _doc(f"{ROOT}#attachment=gone.txt", parent_uri=ROOT)
    with caplog.at_level(logging.INFO):
        summary = await backfill(_Client(gone, hidden=[_root()]), {"s": _Source(**{ROOT: BUNDLE})}, {"s": _recorder()})
    assert summary == Summary(scanned=1, orphaned=1)
    assert f"{gone.uri} is no longer embedded in {ROOT}" in caplog.text


@pytest.mark.asyncio
async def test_nested_attachment_whose_parent_is_no_longer_embedded_is_orphaned():
    middle = _doc(f"{ROOT}#attachment=gone.pdf", parent_uri=ROOT)
    deep = _doc(f"{middle.uri}#attachment=x.txt", parent_uri=middle.uri)
    summary = await backfill(_Client(deep, hidden=[_root(), middle]), {"s": _Source(**{ROOT: BUNDLE})}, {"s": _recorder()})
    assert summary == Summary(scanned=1, orphaned=1)


@pytest.mark.asyncio
async def test_attachment_of_an_unopenable_parent_is_orphaned():
    broken = (FIXTURES / "truncated.pdf").read_bytes()
    root = _doc(ROOT, source_id="s", md5=_md5(broken))
    summary = await backfill(_Client(_child(README), hidden=[root]), {"s": _Source(**{ROOT: broken})}, {"s": _recorder()})
    assert summary == Summary(scanned=1, orphaned=1)


@pytest.mark.asyncio
async def test_provider_failure_on_one_attachment_spares_its_siblings():
    calls = []

    async def provider(source_id, uri, result):
        calls.append(uri)
        if uri == INNER:
            raise ValueError("bad")
        return {"k": 1}

    client = _Client(_child(INNER), _child(README), hidden=[_root()])
    summary = await backfill(client, {"s": _Source(**{ROOT: BUNDLE})}, {"s": provider})
    assert summary.errors == [{"uri": INNER, "error": "ValueError: bad"}]
    assert summary.attachments_updated == 1


# --- argument and database resolution ------------------------------------------------


def test_attachments_are_on_unless_turned_off():
    assert haiku_backfill._parse_args([]).attachments is True
    assert haiku_backfill._parse_args(["--no-attachments"]).attachments is False


def test_bad_missing_key_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        haiku_backfill._parse_args(["--missing=it's"])
    assert raised.value.code == 2
    assert "may only contain" in capsys.readouterr().err


def _config(*names):
    return SimpleNamespace(lancedb=SimpleNamespace(databases={name: f"/db/{name}" for name in names}))


def test_database_name_is_the_only_one_configured():
    assert haiku_backfill._database_name(_config("db"), None) == "db"


def test_database_name_selects_a_configured_one():
    assert haiku_backfill._database_name(_config("a", "b"), "b") == "b"


@pytest.mark.parametrize(
    "config, requested, match",
    [
        (_config(), None, "places no database"),
        (_config("a", "b"), None, r"places 2 databases \(a, b\); pass --db-name"),
        (_config("a"), "c", r"--db-name 'c' is not in lancedb.databases \(a\)"),
    ],
)
def test_database_name_refuses(config, requested, match):
    with pytest.raises(SystemExit, match=match):
        haiku_backfill._database_name(config, requested)


# --- main, against a real database ---------------------------------------------------

_CONFIG = """\
lancedb:
  databases:
    db: {db}
embeddings:
  model:
    provider: ollama
    name: unused
    vector_dim: 4
ingester:
  sources:
    - type: fs
      id: demo
      root: {root}
      metadata_provider: soliplex-pdf-metadata
    - type: fs
      id: other
      root: {root}
"""


async def _seed(config_path: Path, files: dict[str, str]) -> None:
    from docling_core.types.doc.document import DoclingDocument
    from haiku.rag.client import HaikuRAG
    from haiku.rag.config import AppConfig
    from haiku.rag.config import load_yaml_config
    from haiku.rag.store.models.chunk import Chunk

    config = AppConfig.model_validate(load_yaml_config(config_path))
    root = Path(config.ingester.sources[0].root)
    async with HaikuRAG(config=config, create=True) as client:
        ids = []
        for name, content_type in files.items():
            path = root / name
            doc = await client.import_document(
                DoclingDocument(name=name),
                [Chunk(content=name, embedding=[0.1, 0.2, 0.3, 0.4])],
                uri=path.resolve().as_uri(),
                title=name,
                metadata={"content_type": content_type, "md5": _md5(path.read_bytes())},
            )
            ids.append(doc.id)
        await client.set_document_source(ids, "demo")


async def _metadata(config_path: Path) -> dict[str, dict]:
    from haiku.rag.client import HaikuRAG
    from haiku.rag.config import AppConfig
    from haiku.rag.config import load_yaml_config

    config = AppConfig.model_validate(load_yaml_config(config_path))
    async with HaikuRAG(config=config) as client:
        return {doc.uri.rsplit("/", 1)[1]: doc.metadata for doc in await client.list_documents()}


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGFIRE_IGNORE_NO_CONFIG", "1")
    root = tmp_path / "docs"
    root.mkdir()
    shutil.copy(FIXTURES / "with_metadata.pdf", root / "report.pdf")
    shutil.copy(FIXTURES / "truncated.pdf", root / "broken.pdf")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")
    config = tmp_path / "haiku.rag.yaml"
    config.write_text(_CONFIG.format(db=(tmp_path / "demo.lancedb").as_posix(), root=root.as_posix()), encoding="utf-8")
    asyncio.run(
        _seed(config, {"report.pdf": "application/pdf", "broken.pdf": "application/pdf", "notes.md": "text/markdown"})
    )
    return config


def _summary(capsys) -> dict:
    (line,) = [line for line in capsys.readouterr().out.splitlines() if line.startswith(haiku_backfill.SUMMARY_PREFIX)]
    return json.loads(line.removeprefix(haiku_backfill.SUMMARY_PREFIX))


def test_main_fills_the_database(database, capsys):
    argv = [f"--config={database}", "--missing=page_count", "--content-type=application/pdf", "--batch-size=2"]
    assert haiku_backfill.main(argv) == 0

    assert _summary(capsys) == {
        "scanned": 3,
        "updated": 1,
        "unchanged": 1,
        "stale": 0,
        "skipped_no_provider": 0,
        "skipped_not_selected": 1,
        "skipped_provider_opt_out": 0,
        "orphaned": 0,
        "attachments_updated": 0,
        "errors": [],
        "check": False,
    }
    metadata = asyncio.run(_metadata(database))
    assert metadata["report.pdf"]["page_count"] == 3
    assert metadata["report.pdf"]["pdf_title"] == "Quarterly Report"
    assert metadata["report.pdf"]["source_id"] == "demo"
    assert "page_count" not in metadata["broken.pdf"]
    assert "page_count" not in metadata["notes.md"]

    # LanceDB's scope now leaves the filled document out of the listing.
    assert haiku_backfill.main([f"--config={database}", "--missing=page_count"]) == 0
    assert _summary(capsys)["scanned"] == 2


def test_main_check_writes_nothing(database, capsys):
    assert haiku_backfill.main([f"--config={database}", "--missing=page_count", "--check"]) == 0
    summary = _summary(capsys)
    assert (summary["updated"], summary["check"]) == (1, True)
    assert "page_count" not in asyncio.run(_metadata(database))["report.pdf"]


def test_main_filter_scopes_the_listing(database, capsys):
    assert haiku_backfill.main([f"--config={database}", "--filter=uri LIKE '%notes.md'"]) == 0
    assert _summary(capsys)["scanned"] == 1


def test_main_exits_partial_when_a_document_fails(database, capsys):
    (Path(database).parent / "docs" / "report.pdf").unlink()
    assert haiku_backfill.main([f"--config={database}", "--db-name=db"]) == haiku_backfill.EXIT_PARTIAL
    (error,) = _summary(capsys)["errors"]
    assert error["uri"].endswith("/report.pdf")


def test_main_closes_the_sources(database):
    from haiku.rag.sources.fs import FSSource

    with patch.object(FSSource, "aclose", autospec=True) as aclose:
        haiku_backfill.main([f"--config={database}"])
    assert aclose.await_count == 2


def test_main_requires_a_config():
    with (
        patch("haiku.rag.config.find_config_file", return_value=None),
        pytest.raises(SystemExit, match="no haiku-rag config"),
    ):
        haiku_backfill.main([])


async def _seed_attachments(config_path: Path) -> None:
    """Index ``bundle.pdf`` and its attachments the way haiku-rag stores them."""
    from docling_core.types.doc.document import DoclingDocument
    from haiku.rag.client import HaikuRAG
    from haiku.rag.config import AppConfig
    from haiku.rag.config import load_yaml_config
    from haiku.rag.store.models.chunk import Chunk

    config = AppConfig.model_validate(load_yaml_config(config_path))
    path = Path(config.ingester.sources[0].root) / "bundle.pdf"
    root_uri = path.resolve().as_uri()
    body = path.read_bytes()
    rows = [(root_uri, "application/pdf", _md5(body), None)]
    for attachment in extract_attachments(body, root_uri).values():
        rows.append((attachment.uri, attachment.content_type, attachment.content_hash, root_uri))
        for nested in (extract_attachments(attachment.data, attachment.uri) or {}).values():
            rows.append((nested.uri, nested.content_type, nested.content_hash, attachment.uri))
    async with HaikuRAG(config=config) as client:
        ids = []
        for uri, content_type, md5, parent in rows:
            metadata = {"content_type": content_type, "md5": md5} | ({"parent_uri": parent} if parent else {})
            doc = await client.import_document(
                DoclingDocument(name=uri),
                [Chunk(content=uri, embedding=[0.1, 0.2, 0.3, 0.4])],
                uri=uri,
                metadata=metadata,
            )
            ids.append(doc.id)
        await client.set_document_source(ids[:1], "demo")


def test_main_fills_attachments(database, capsys):
    shutil.copy(FIXTURES / "with_attachments.pdf", Path(database).parent / "docs" / "bundle.pdf")
    asyncio.run(_seed_attachments(database))

    assert haiku_backfill.main([f"--config={database}", "--missing=page_count", "--content-type=application/pdf"]) == 0

    summary = _summary(capsys)
    # report.pdf, bundle.pdf and its attached PDF; broken.pdf gets nothing.
    assert (summary["updated"], summary["attachments_updated"], summary["unchanged"]) == (3, 1, 1)
    metadata = asyncio.run(_metadata(database))
    assert metadata["bundle.pdf"]["page_count"] == 1
    attached = metadata["bundle.pdf#attachment=inner%20report.pdf"]
    assert attached["page_count"] == 2
    assert "source_id" not in attached
    assert attached["parent_uri"].endswith("/bundle.pdf")
