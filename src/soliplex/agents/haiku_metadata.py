"""haiku-rag metadata providers.

haiku-rag lets a package attach metadata to every document an ingester source
fetches: the package registers a zero-argument factory under the
``haiku.rag.metadata_providers`` entry-point group, and a source in the haiku
config names it with ``metadata_provider: <name>``. The ingester calls the
provider with ``(source_id, uri, result)`` for each new or changed document and
merges the returned dict into the document's metadata. See
``docs/ingester.md`` in haiku-rag ("Metadata providers").

These run inside ``haiku-ingester`` (the load step), not inside ``si-agent``,
so they see the document exactly as haiku-rag fetched it from the download
store. A provider only runs for new or changed documents; ``si-agent manifest
backfill-metadata`` runs it over those indexed before it was configured (see
:mod:`soliplex.agents.haiku_backfill`).
"""

import asyncio
import datetime
import logging
import re
from typing import Any

import pypdfium2 as pdfium
from haiku.rag.converters.pdf_split import PDFIUM_LOCK
from haiku.rag.sources.base import FetchResult

from soliplex.agents.sidecar import Sidecars
from soliplex.agents.sidecar import get_kind
from soliplex.agents.store import get_document_store

logger = logging.getLogger(__name__)

PDF_MIME_TYPE = "application/pdf"

# The PDF spec lets a header appear anywhere in the first 1024 bytes.
_PDF_HEADER = b"%PDF-"
_HEADER_WINDOW = 1024

# Document information dictionary keys, and the metadata key each is stored
# under. ``Trapped`` is a name, not text, and says nothing useful for search.
_INFO_KEYS = {
    "Title": "pdf_title",
    "Author": "pdf_author",
    "Subject": "pdf_subject",
    "Keywords": "pdf_keywords",
    "Creator": "pdf_creator",
    "Producer": "pdf_producer",
    "CreationDate": "pdf_creation_date",
    "ModDate": "pdf_mod_date",
}
_DATE_KEYS = frozenset({"CreationDate", "ModDate"})

# ``D:YYYYMMDDHHmmSSOHH'mm'`` (PDF 32000-1, 7.9.4). Everything after the year
# is optional; real files also omit the ``D:`` prefix and the trailing
# apostrophe, or write ``Z`` with a zero offset after it.
_PDF_DATE = re.compile(
    r"^(?:D:)?(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    r"(?:(?P<tz>[Zz+-])(?:(?P<tzh>\d{2})'?(?P<tzm>\d{2})?'?)?)?$"
)


def is_pdf(content_type: str | None, body: bytes) -> bool:
    """Whether *body* is a PDF, by declared type or by its header."""
    if content_type and content_type.split(";", 1)[0].strip().lower() == PDF_MIME_TYPE:
        return True
    return _PDF_HEADER in body[:_HEADER_WINDOW]


def parse_pdf_date(value: str) -> str:
    """Render a PDF date as ISO 8601, or return it unchanged when it is not one.

    A date without a time zone is left naive, as the PDF wrote it.
    """
    match = _PDF_DATE.match(value.strip())
    if match is None:
        return value
    parts = match.groupdict()
    tz = None
    if parts["tz"] is not None:
        offset = datetime.timedelta(hours=int(parts["tzh"] or 0), minutes=int(parts["tzm"] or 0))
        tz = datetime.timezone(-offset if parts["tz"] == "-" else offset)
    try:
        moment = datetime.datetime(
            int(parts["year"]),
            int(parts["month"] or 1),
            int(parts["day"] or 1),
            int(parts["hour"] or 0),
            int(parts["minute"] or 0),
            int(parts["second"] or 0),
            tzinfo=tz,
        )
    except ValueError:
        return value
    return moment.isoformat()


def read_pdf_metadata(body: bytes) -> dict[str, Any]:
    """Page count, PDF version and document information of the PDF in *body*.

    Empty information entries are left out, so a key's presence means the PDF
    set it.

    Raises:
        pdfium.PdfiumError: when pdfium cannot open the PDF (password,
            corruption, not a PDF).
    """
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(body)
        try:
            metadata: dict[str, Any] = {"page_count": len(pdf)}
            version = pdf.get_version()
            if version is not None:
                metadata["pdf_version"] = f"{version // 10}.{version % 10}"
            for key, value in pdf.get_metadata_dict(skip_empty=True).items():
                value = value.strip()
                if value:
                    metadata[_INFO_KEYS[key]] = parse_pdf_date(value) if key in _DATE_KEYS else value
            return metadata
        finally:
            pdf.close()


class PdfMetadataProvider:
    """Add a PDF's page count and document information to its haiku-rag metadata.

    Registered as the ``soliplex-pdf-metadata`` metadata provider. For a PDF it
    returns ``page_count`` and, when the file states them, ``pdf_version``,
    ``pdf_title``, ``pdf_author``, ``pdf_subject``, ``pdf_keywords``,
    ``pdf_creator``, ``pdf_producer``, ``pdf_creation_date`` and
    ``pdf_mod_date`` (the dates as ISO 8601). Anything else gets nothing.

    A PDF pdfium cannot open -- password protected, truncated -- also gets
    nothing, with a warning: haiku-rag treats a provider exception as an
    ingestion failure, and whether such a document is indexed is the
    converter's call, not this provider's.

    pdfium is not thread-safe, so every call holds haiku-rag's process-wide
    ``PDFIUM_LOCK``, the one its own page slicing and attachment scanning use.
    The work runs in a thread so waiting on that lock never blocks the
    ingester's event loop.
    """

    async def __call__(self, source_id: str, uri: str, result: FetchResult) -> dict:
        if not is_pdf(result.content_type, result.body):
            return {}
        try:
            return await asyncio.to_thread(read_pdf_metadata, result.body)
        except pdfium.PdfiumError as e:
            logger.warning("cannot read PDF metadata of %s (source %s): %s", uri, source_id, e)
            return {}


class SidecarMetadataProvider:
    """Add a document's ``.meta.json`` sidecar to its haiku-rag metadata.

    Registered as the ``soliplex-sidecar-metadata`` metadata provider.
    ``haiku-ingester`` reads only document bytes, so without this the upstream
    ``source_uri`` / ``source_url``, the ``ingestion_type``, when the document
    was downloaded and any metadata the manifest attached never reach the
    index. The sidecar is read back flattened (:meth:`MetaSidecar.parse
    <soliplex.agents.sidecar.meta.MetaSidecar.parse>`): the manifest metadata
    at the top level, nested values JSON-encoded.

    The download store is the agent's own: the haiku source's ``id`` is the
    sanitized manifest source (``${SOURCE}``), and sanitizing it again is a
    no-op, so it resolves the same target the run wrote; ``DOWNLOAD_DIR`` and
    the ``DOWNLOAD_S3_*`` settings come from the load's environment. Addressing
    belongs to :class:`~soliplex.agents.sidecar.Sidecars`, which maps the
    document URI haiku-rag stores back to its key.

    Lenient: a missing, unreadable or malformed sidecar costs the document its
    sidecar metadata, with a log line, not its ingestion -- haiku-rag treats a
    provider exception as a failed document.

    A sidecar that changes while its document does not (new manifest
    metadata) is not picked up: haiku-rag skips an unchanged document before
    calling any provider, and ``*.meta.json`` is not ingested itself. Run
    ``si-agent manifest backfill-metadata --filter ...`` (or a full pass) for
    that.
    """

    def __init__(self) -> None:
        self._sidecars: dict[str, Sidecars] = {}

    def _sidecars_for(self, source_id: str) -> Sidecars:
        sidecars = self._sidecars.get(source_id)
        if sidecars is None:
            sidecars = self._sidecars[source_id] = Sidecars(get_document_store(source_id))
        return sidecars

    async def __call__(self, source_id: str, uri: str, result: FetchResult) -> dict:
        try:
            raw = await self._sidecars_for(source_id).read_for_uri(uri)
        except Exception as e:
            logger.warning("cannot read the sidecar of %s (source %s): %s", uri, source_id, e, exc_info=True)
            return {}
        if raw is None:
            logger.info("no sidecar for %s (source %s)", uri, source_id)
            return {}
        return get_kind("meta").parse(raw)


class SoliplexMetadataProvider:
    """Both of the above: :class:`SidecarMetadataProvider` and :class:`PdfMetadataProvider`.

    Registered as ``soliplex-metadata``. A haiku source names a single
    ``metadata_provider``, so this is how one gets both. The sidecar is
    applied last, so manifest-supplied metadata wins a clash, as it does over
    the sidecar's own fields.
    """

    def __init__(self) -> None:
        self._providers = (PdfMetadataProvider(), SidecarMetadataProvider())

    async def __call__(self, source_id: str, uri: str, result: FetchResult) -> dict:
        metadata: dict = {}
        for provider in self._providers:
            metadata.update(await provider(source_id, uri, result))
        return metadata
