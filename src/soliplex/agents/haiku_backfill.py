"""Back-fill haiku-rag metadata providers over documents already indexed.

haiku-rag calls a source's ``metadata_provider`` only when it fetches a new or
changed document, so a provider added to a source -- or one that starts
returning more -- leaves every document indexed before it without its keys
until that document changes. This fills them in without re-ingesting: each
selected document is fetched again through the source that ingested it (the
same adapter, so the provider sees what the ingester would hand it), the
source's provider is called, and its keys are merged into the document's
metadata with a metadata-only update. Nothing is converted, chunked or
embedded.

The provider is the one code path to the metadata: the ingester calls it for
new documents and this calls it for old ones, so the two cannot drift apart.

Run as a subprocess, never in the agent's own process, for the same reason as
the load and the maintenance verbs: LanceDB's async runtime deadlocks in the
agent's event loop. ``si-agent manifest backfill-metadata`` and the
``backfill_metadata`` post-process callback spawn it through
:mod:`soliplex.agents.manifest.haiku_maint`::

    python -m soliplex.agents.haiku_backfill --config=<haiku cfg> \\
        [--missing KEY ...] [--content-type TYPE ...] [--filter SQL] \\
        [--db-name NAME] [--batch-size N] [--check] [--no-attachments]

Which documents run:

* the listing is scoped in LanceDB: ``--missing`` keys become a ``WHERE``
  clause matching documents whose metadata lacks one of them (see
  :func:`missing_filter`), AND-ed with an explicit ``--filter``;
* of those, a document runs when its source has a provider that does not opt
  out (``backfill = False``, see :func:`backfills`), and its stored
  ``content_type`` is one of the ``--content-type`` values when any are given.

With no scoping every document of a source with a provider is fetched again;
one whose metadata the provider would not change is still not written.

PDF attachments haiku-rag extracted (``parent_uri`` set, no ``source_id``)
belong to no source and have no URI a source can fetch. Each is filled from its
top-level ancestor instead: that document is fetched through its source, the
attachment is extracted from it as ingestion did (:func:`extract_attachments`,
following nested attachments down), and the ancestor source's provider is
called on the attachment's bytes with ``extra_metadata["parent_uri"]`` set --
what haiku-rag hands a provider for an attachment once it calls providers for
them at all. A sidecar provider therefore gives an attachment its parent's
sidecar, and the PDF provider an attached PDF its own page count.
``--no-attachments`` leaves them alone.

The last line of stdout is ``BACKFILL_SUMMARY <json>`` -- the
:class:`Summary` the caller reports. The process exits 3
(:data:`EXIT_PARTIAL`) when any document failed and 0 otherwise -- not 1,
which is what Python exits with when the run itself crashes.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import sys
from collections.abc import AsyncIterator
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "BACKFILL_SUMMARY "
# Exit status when the run finished but some documents could not be filled.
EXIT_PARTIAL = 3
DEFAULT_BATCH_SIZE = 500
# haiku-rag's attachment nesting cap (``MAX_ATTACHMENT_DEPTH`` in
# ``haiku.rag.client.documents``): no attachment chain is longer.
MAX_ATTACHMENT_DEPTH = 3

# Keys the source pipeline owns; haiku-rag drops them from provider output
# (``haiku.rag.client.documents._RESERVED_METADATA_KEYS``, private there).
# ``parent_uri`` links an attachment to its parent, which no provider may
# change either.
RESERVED_METADATA_KEYS = frozenset({"content_type", "md5", "source_revision", "source_id", "parent_uri"})

# A metadata key ``--missing`` will put into SQL. Anything else is refused
# rather than escaped: real keys look like this, and a quote has no business
# in one.
_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class Summary:
    """What a back-fill did.

    ``updated + unchanged + stale + orphaned + skipped_no_provider +
    skipped_provider_opt_out + skipped_not_selected + len(errors) == scanned``.
    Under ``--check`` ``updated`` counts the documents that *would* be written;
    ``attachments_updated`` is the part of it that is PDF attachments.
    """

    scanned: int = 0
    updated: int = 0
    attachments_updated: int = 0
    unchanged: int = 0
    # The stored document is older than the source's current bytes (for an
    # attachment: than its top-level ancestor's, or than what that ancestor now
    # embeds); the next load re-ingests it, which runs the provider anyway.
    stale: int = 0
    # An attachment whose parent is no longer indexed, or no longer embeds
    # it. A load removes such a child only when it re-ingests the parent, and
    # not at all in some cases, so these are worth looking at.
    orphaned: int = 0
    # Its source has no ``metadata_provider``, or no configured source owns it
    # (``haiku-rag add-src`` documents, and attachments under them).
    skipped_no_provider: int = 0
    # Its source's provider sets ``backfill = False``.
    skipped_provider_opt_out: int = 0
    # Not one of the ``--content-type`` values, or carries every ``--missing``
    # key (the SQL scope matched it on text alone).
    skipped_not_selected: int = 0
    errors: list[dict] = field(default_factory=list)
    check: bool = False


@dataclass(frozen=True)
class Attachment:
    """One file embedded in a PDF, as haiku-rag ingests it."""

    uri: str
    name: str
    data: bytes
    content_type: str
    content_hash: str


def attachment_uri(parent_uri: str, name: str) -> str:
    """The URI haiku-rag gives the attachment *name* of the document at *parent_uri*."""
    return f"{parent_uri}#attachment={quote(name, safe='')}"


def extract_attachments(body: bytes, parent_uri: str) -> dict[str, Attachment] | None:
    """The files embedded in the PDF *body*, keyed by the URI haiku-rag gives them.

    The same rules as haiku-rag's private ``_extract_pdf_attachments`` -- URI,
    content type guessed from the name, MD5 of the bytes, and every pdfium call
    under its process-wide ``PDFIUM_LOCK`` -- so a child found here is the one
    ingestion stored. Kept here until haiku-rag exposes it.

    Returns:
        ``None`` when pdfium cannot open *body*.
    """
    import pypdfium2 as pdfium
    from haiku.rag.converters.pdf_split import PDFIUM_LOCK

    with PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(body)
        except pdfium.PdfiumError:
            return None
        try:
            found: dict[str, Attachment] = {}
            for index in range(pdf.count_attachments()):
                attachment = pdf.get_attachment(index)
                name = attachment.get_name()
                # haiku-rag skips a nameless attachment too.
                if not name:  # pragma: no cover - needs a malformed PDF
                    continue
                data = bytes(attachment.get_data())
                uri = attachment_uri(parent_uri, name)
                found[uri] = Attachment(
                    uri=uri,
                    name=name,
                    data=data,
                    content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    content_hash=hashlib.md5(data, usedforsecurity=False).hexdigest(),
                )
            return found
        finally:
            pdf.close()


def missing_filter(keys: Iterable[str]) -> str | None:
    """A LanceDB ``WHERE`` clause matching documents lacking any of *keys*.

    ``metadata`` is one ``json.dumps`` text column, so this matches the quoted
    key inside it; the quotes keep a *value* that mentions the key from
    counting as the key. The ``IS NULL`` arm is there because ``NULL NOT LIKE
    x`` is NULL, which would exclude a null-metadata row. Parenthesised so it
    survives being AND-ed with another clause. ``_`` is a LIKE wildcard, so a
    key can over-match a near-identical one; :func:`backfill` checks the real
    keys again.

    Raises:
        ValueError: for a key that is not letters, digits, ``_``, ``.``, ``-``.
    """
    keys = list(keys)
    for key in keys:
        if not _KEY.match(key):
            raise ValueError(f"metadata key {key!r} may only contain letters, digits, '_', '.' and '-'")
    if not keys:
        return None
    lacking = " OR ".join(f"metadata NOT LIKE '%\"{key}\"%'" for key in keys)
    return f"(metadata IS NULL OR {lacking})"


def compose_filter(*clauses: str | None) -> str | None:
    """AND the given ``WHERE`` clauses together, ignoring ``None``."""
    present = [clause for clause in clauses if clause]
    if len(present) <= 1:
        return present[0] if present else None
    return " AND ".join(f"({clause})" for clause in present)


async def _iter_documents(client: Any, *, batch_size: int, doc_filter: str | None) -> AsyncIterator[Any]:
    """Yield every matching document, reading all pages before the first.

    The caller updates documents while consuming; reading every page first
    keeps those writes from shifting the offset pagination under it. The
    listing carries no content or docling blobs, so buffering it is cheap.
    """
    documents: list[Any] = []
    offset = 0
    while True:
        page = await client.list_documents(limit=batch_size, offset=offset, filter=doc_filter)
        if not page:
            break
        documents.extend(page)
        offset += batch_size
    for doc in documents:
        yield doc


def _selected(metadata: Mapping[str, Any], missing: list[str], content_types: set[str]) -> bool:
    if content_types and str(metadata.get("content_type", "")).lower() not in content_types:
        return False
    return not missing or any(key not in metadata for key in missing)


def backfills(provider: Any) -> bool:
    """Whether *provider* may be back-filled.

    A provider whose output depends on *when* it runs rather than on the
    document -- an ingestion timestamp -- sets ``backfill = False``: run over
    stored documents it would record when the back-fill ran.
    """
    return bool(getattr(provider, "backfill", True))


def _is_attachment(metadata: Mapping[str, Any]) -> bool:
    """A PDF attachment haiku-rag extracted: linked to a parent, owned by no source."""
    return bool(metadata.get("parent_uri")) and not metadata.get("source_id")


@dataclass
class _Run:
    """One back-fill's settings and tallies, shared by both document paths."""

    client: Any
    sources: Mapping[str, Any]
    providers: Mapping[str, Any]
    missing: list[str]
    content_types: set[str]
    check: bool
    summary: Summary
    opted_out: set[str] = field(default_factory=set)

    def owner(self, source_id: Any) -> tuple[Any, Any] | None:
        """The source and provider to fill a document of *source_id* with.

        ``None`` (counted) when there is none, or the provider opted out.
        """
        provider = self.providers.get(source_id)
        source = self.sources.get(source_id)
        if provider is None or source is None:
            self.summary.skipped_no_provider += 1
            return None
        if not backfills(provider):
            if source_id not in self.opted_out:
                self.opted_out.add(source_id)
                logger.info("%s's metadata provider sets backfill = False; not back-filling it", source_id)
            self.summary.skipped_provider_opt_out += 1
            return None
        return source, provider

    def selected(self, metadata: Mapping[str, Any]) -> bool:
        """Whether *metadata* passes ``--content-type`` / ``--missing`` (counted when not)."""
        if _selected(metadata, self.missing, self.content_types):
            return True
        self.summary.skipped_not_selected += 1
        return False

    def failed(self, uri: str, error: Exception) -> None:
        logger.warning("cannot back-fill %s: %s", uri, error, exc_info=error)
        self.summary.errors.append({"uri": uri, "error": f"{type(error).__name__}: {error}"})

    async def fill(self, doc: Any, source_id: str, provider: Any, result: Any, *, attachment: bool = False) -> None:
        """Call *provider* on *result* and write what changes *doc*'s metadata."""
        metadata = doc.metadata or {}
        provided = await provider(source_id, doc.uri, result.model_copy(deep=True))
        merged = {**metadata, **{k: v for k, v in provided.items() if k not in RESERVED_METADATA_KEYS}}
        if merged == metadata:
            self.summary.unchanged += 1
            return
        added = ", ".join(sorted(set(merged) - set(metadata))) or "changed values"
        if self.check:
            logger.info("would back-fill %s: %s", doc.uri, added)
        else:
            await self.client.update_document(doc.id, metadata=merged)
            logger.info("back-filled %s: %s", doc.uri, added)
        self.summary.updated += 1
        if attachment:
            self.summary.attachments_updated += 1

    async def document(self, doc: Any) -> None:
        """Fill one top-level document: fetch it again through its own source."""
        metadata = doc.metadata or {}
        source_id = metadata.get("source_id")
        owner = self.owner(source_id)
        if owner is None or not self.selected(metadata):
            return
        source, provider = owner
        try:
            result = await source.fetch(doc.uri)
            if metadata.get("md5") not in (None, result.content_hash):
                logger.info("%s changed since it was indexed; leaving it to the next load", doc.uri)
                self.summary.stale += 1
                return
            await self.fill(doc, source_id, provider, result)
        except Exception as e:
            self.failed(doc.uri, e)

    async def attachments(self, children: list[Any]) -> None:
        """Fill PDF attachments from their top-level ancestor's bytes.

        An attachment has no source of its own and no URI a source can fetch,
        so each is re-derived the way ingestion produced it: the ancestor is
        fetched through *its* source (once, however many children it has), the
        chain of attachments is extracted down to the child, and the provider
        is called on the parent source's behalf with what ingestion hands it.
        """
        known: dict[str, Any] = {}

        async def indexed(uri: str) -> Any:
            if uri not in known:
                known[uri] = await self.client.get_document_by_uri(uri)
            return known[uri]

        groups: dict[str, tuple[Any, str, Any, Any, list[tuple[list[str], Any]]]] = {}
        for child in children:
            chain, root = await self._ancestors(child, indexed)
            if root is None:
                logger.info("%s has no indexed top-level ancestor", child.uri)
                self.summary.orphaned += 1
                continue
            source_id = (root.metadata or {}).get("source_id")
            owner = self.owner(source_id)
            if owner is None or not self.selected(child.metadata or {}):
                continue
            source, provider = owner
            groups.setdefault(root.uri, (root, source_id, source, provider, []))[4].append((chain, child))

        for root, source_id, source, provider, members in groups.values():
            await self._fill_group(root, source_id, source, provider, members)

    async def _ancestors(self, child: Any, indexed: Any) -> tuple[list[str], Any]:
        """The URIs from *child*'s top-level ancestor down to its parent, and that ancestor.

        The ancestor is ``None`` when the chain breaks (a parent no longer
        indexed) or runs longer than haiku-rag ever nests.
        """
        chain: list[str] = []
        uri = child.metadata["parent_uri"]
        for _ in range(MAX_ATTACHMENT_DEPTH):
            parent = await indexed(uri)
            if parent is None:
                return chain, None
            chain.insert(0, parent.uri)
            uri = (parent.metadata or {}).get("parent_uri")
            if not uri:
                return chain, parent
        return chain, None

    async def _fill_group(self, root: Any, source_id: str, source: Any, provider: Any, members: list) -> None:
        """Fill every selected attachment under one top-level document."""
        from haiku.rag.sources.base import FetchResult

        try:
            result = await source.fetch(root.uri)
        except Exception as e:
            for _, child in members:
                self.failed(child.uri, e)
            return
        if (root.metadata or {}).get("md5") not in (None, result.content_hash):
            logger.info("%s changed since it was indexed; leaving its attachments to the next load", root.uri)
            self.summary.stale += len(members)
            return

        bodies = {root.uri: result.body}
        extracted: dict[str, dict[str, Attachment] | None] = {}

        def embedded(parent_uri: str, uri: str) -> Attachment | None:
            if parent_uri not in extracted:
                extracted[parent_uri] = extract_attachments(bodies[parent_uri], parent_uri)
            found = (extracted[parent_uri] or {}).get(uri)
            if found is not None:
                bodies[uri] = found.data
            return found

        for chain, child in members:
            try:
                attachment = None
                for parent_uri, uri in zip(chain, [*chain[1:], child.uri], strict=True):
                    attachment = embedded(parent_uri, uri)
                    if attachment is None:
                        break
                if attachment is None:
                    logger.info("%s is no longer embedded in %s", child.uri, chain[-1])
                    self.summary.orphaned += 1
                    continue
                if (child.metadata or {}).get("md5") not in (None, attachment.content_hash):
                    logger.info("%s changed since it was indexed; leaving it to the next load", child.uri)
                    self.summary.stale += 1
                    continue
                child_result = FetchResult(
                    uri=child.uri,
                    body=attachment.data,
                    content_type=attachment.content_type,
                    content_hash=attachment.content_hash,
                    extra_metadata={"parent_uri": chain[-1]},
                )
                await self.fill(child, source_id, provider, child_result, attachment=True)
            except Exception as e:
                self.failed(child.uri, e)


async def backfill(
    client: Any,
    sources: Mapping[str, Any],
    providers: Mapping[str, Any],
    *,
    missing: Iterable[str] = (),
    content_types: Iterable[str] = (),
    doc_filter: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    check: bool = False,
    attachments: bool = True,
) -> Summary:
    """Run each document's source provider and merge what it returns.

    A PDF attachment -- ``parent_uri`` set, no ``source_id`` -- is filled from
    its top-level ancestor (see :meth:`_Run.attachments`) with the provider of
    the ancestor's source, after every top-level document.

    Args:
        client: An open ``HaikuRAG`` over the one database to fill.
        sources: Source adapters keyed by source id.
        providers: Metadata providers keyed by source id.
        missing: Only documents lacking one of these keys.
        content_types: Only documents of one of these content types.
        doc_filter: A LanceDB ``WHERE`` clause, AND-ed with the one *missing*
            produces.
        batch_size: Pagination size for the document listing.
        check: Work out what would change, and write nothing.
        attachments: Fill PDF attachments too; without it they are counted
            as ``skipped_no_provider``, as no source owns them.

    Raises:
        ValueError: for a *missing* key :func:`missing_filter` refuses.
    """
    run = _Run(
        client=client,
        sources=sources,
        providers=providers,
        missing=list(missing),
        content_types={t.lower() for t in content_types},
        check=check,
        summary=Summary(check=check),
    )
    scope = compose_filter(missing_filter(run.missing), doc_filter)
    children: list[Any] = []
    async for doc in _iter_documents(client, batch_size=batch_size, doc_filter=scope):
        run.summary.scanned += 1
        if attachments and _is_attachment(doc.metadata or {}):
            children.append(doc)
        else:
            await run.document(doc)
    if children:
        await run.attachments(children)
    return run.summary


def _metadata_key(value: str) -> str:
    """argparse type for ``--missing``: a usage error, not a traceback."""
    try:
        missing_filter([value])
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m soliplex.agents.haiku_backfill", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, help="haiku-rag config (default: haiku-rag's own discovery)")
    parser.add_argument("--db-name", help="database in the config's lancedb.databases; only needed when it places several")
    parser.add_argument(
        "--missing", type=_metadata_key, action="append", default=[], help="only documents lacking this key (repeatable)"
    )
    parser.add_argument(
        "--content-type", dest="content_types", action="append", default=[], help="only this content type (repeatable)"
    )
    parser.add_argument("--filter", dest="doc_filter", help="LanceDB WHERE clause scoping which documents run")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="pagination size")
    parser.add_argument("--check", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--no-attachments", dest="attachments", action="store_false", help="leave PDF attachments alone")
    return parser.parse_args(argv)


def _database_name(config: Any, requested: str | None) -> str:
    """The configured database to open.

    The load's config places the database (``lancedb.databases``); a config
    placing none would leave haiku-rag to open its default one, which is not
    the database the load wrote.

    Raises:
        SystemExit: when the config places none, or several and *requested*
            names none of them.
    """
    configured = config.lancedb.databases
    if not configured:
        raise SystemExit("the haiku-rag config places no database (lancedb.databases)")
    if requested is not None:
        if requested not in configured:
            raise SystemExit(f"--db-name {requested!r} is not in lancedb.databases ({', '.join(sorted(configured))})")
        return requested
    if len(configured) > 1:
        raise SystemExit(
            f"the haiku-rag config places {len(configured)} databases ({', '.join(sorted(configured))}); pass --db-name"
        )
    return next(iter(configured))


async def _run(args: argparse.Namespace) -> Summary:
    from haiku.rag.client import HaikuRAG
    from haiku.rag.config import AppConfig
    from haiku.rag.config import find_config_file
    from haiku.rag.config import load_yaml_config
    from haiku.rag.ingester.metadata import build_providers
    from haiku.rag.ingester.metadata import load_metadata_providers
    from haiku.rag.ingester.pollers.factory import build_source

    config_path = find_config_file(cli_path=args.config)
    # haiku-rag's built-in defaults name no source, so there would be nothing
    # to fill -- and a default database that is not the one meant.
    if config_path is None:
        raise SystemExit("no haiku-rag config found; pass --config")
    config = AppConfig.model_validate(load_yaml_config(config_path))
    database = _database_name(config, args.db_name)
    configs = config.ingester.sources
    sources = [build_source(cfg) for cfg in configs]
    try:
        providers = build_providers(
            [(source.source_id, cfg.metadata_provider) for cfg, source in zip(configs, sources, strict=True)],
            load_metadata_providers(),
        )
        logger.info(
            "back-filling %s with %s (scope: %s)",
            database,
            ", ".join(f"{source_id}: {type(p).__name__}" for source_id, p in providers.items()) or "no providers",
            compose_filter(missing_filter(args.missing), args.doc_filter) or "all documents",
        )
        async with HaikuRAG(config=config, sources=[database]) as client:
            return await backfill(
                client,
                {source.source_id: source for source in sources},
                providers,
                missing=args.missing,
                content_types=args.content_types,
                doc_filter=args.doc_filter,
                batch_size=args.batch_size,
                check=args.check,
                attachments=args.attachments,
            )
    finally:
        for source in sources:
            await source.aclose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = asyncio.run(_run(_parse_args(argv)))
    print(SUMMARY_PREFIX + json.dumps(asdict(summary), sort_keys=True), flush=True)
    return EXIT_PARTIAL if summary.errors else 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
