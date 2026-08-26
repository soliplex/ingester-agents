"""Write fetched documents and metadata sidecars to the local filesystem.

Replaces the previous behaviour of POSTing documents to the Soliplex
Ingester. Each document is written under
``<download_dir>/<sanitized-source>/<source-relative-path>`` and is
accompanied by a ``<filename>.meta.json`` sidecar carrying its MIME
type and any other available metadata.
"""

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit

from soliplex.agents.common.mime import ensure_extension
from soliplex.agents.common.mime import guess_extension

logger = logging.getLogger(__name__)

# Re-exported for callers that still import it from here; the suffix itself is
# owned by the sidecar kind that uses it.
from soliplex.agents.sidecar import META_SUFFIX  # noqa: E402, F401

__all__ = ["META_SUFFIX"]

# Characters illegal in Windows path segments (superset of POSIX concerns).
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows reserved device names (matched case-insensitively against the stem).
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def sanitize_source(source: str) -> str:
    """Convert a source identifier into a single filesystem-safe folder name.

    Path separators and other illegal characters (notably the ``:`` in
    identifiers like ``gitea:admin:myrepo:files``) are replaced with
    underscores so the whole source maps to one directory.

    Args:
        source: Source identifier (e.g. ``"gitea:admin:myrepo:files"``).

    Returns:
        A folder name safe on Windows and POSIX (e.g.
        ``"gitea_admin_myrepo_files"``).
    """
    cleaned = _ILLEGAL_CHARS.sub("_", source)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_. ")
    return cleaned or "source"


def _sanitize_segment(segment: str) -> str:
    """Sanitize a single path segment, preserving directory nesting elsewhere."""
    cleaned = _ILLEGAL_CHARS.sub("_", segment).rstrip(". ")
    if not cleaned:
        return "_"
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def uri_to_relpath(uri: str, *, mime_type: str | None = None) -> Path:
    """Map a source URI to a safe relative path under the source directory.

    Preserves the source directory structure where possible. URLs are
    mapped to ``host/path`` and gain a synthesized filename when they end
    in ``/``. Path traversal segments (``..``) are dropped. The final
    segment's extension is reconciled against ``mime_type`` for every
    source (see :func:`~soliplex.agents.common.mime.ensure_extension`):
    an extension is added when missing, replaced when it clearly
    mismatches, and left alone when already correct.

    Args:
        uri: Source URI or path (e.g. ``"docs/readme.md"``,
            ``"/owner/repo/issues/12"``, ``"https://x.com/a/b"``).
        mime_type: MIME type used to synthesize a missing extension.

    Returns:
        A relative :class:`~pathlib.Path` (never absolute, no ``..``).
    """
    raw = uri.strip()
    split = urlsplit(raw)

    if split.scheme in ("http", "https"):
        path = unquote(split.path)
        segs = [s for s in path.split("/") if s and s not in (".", "..")]
        rel_segs = [split.netloc, *segs] if split.netloc else list(segs)
        no_filename = (not segs) or path.endswith("/")
    else:
        cleaned = unquote(raw).replace("\\", "/")
        rel_segs = [s for s in cleaned.split("/") if s and s not in (".", "..")]
        no_filename = raw.endswith("/") or not rel_segs

    rel_segs = [_sanitize_segment(s) for s in rel_segs]
    if not rel_segs:
        # Nothing to derive a name from (e.g. "/" or ""): use a bare index file.
        rel_segs = ["index"]
        no_filename = False

    if no_filename:
        rel_segs.append("index" + guess_extension(mime_type))
    else:
        rel_segs[-1] = ensure_extension(rel_segs[-1], mime_type)

    return Path(*rel_segs)


def log_extension_choice(uri: str, rel: Path, mime_type: str | None) -> None:
    """Log when the on-disk name does not match the name in *uri*.

    On-disk names come from the detected MIME type, so a document can land
    under an extension its URI never mentioned. That is usually intended (a
    ``.bin`` that really is a PDF), but it is also how OOXML documents ended
    up misfiled -- every one of them is a ZIP, and content sniffing reads
    them all back as docx. Either way the divergence is worth a line in the
    log, because nothing else records that the name changed.

    A *replacement* is logged at WARNING: the URI stated an extension and
    detection overruled it, which is the case that has been wrong before.
    Supplying a missing extension is routine and logged at INFO.
    """
    stated = uri_to_relpath(uri).suffix
    chosen = rel.suffix
    if stated == chosen:
        return
    if stated:
        logger.warning(
            "renaming %s to %s: detected type %s overrides the URI extension %s",
            uri,
            rel.as_posix(),
            mime_type,
            stated,
        )
    else:
        logger.info("naming %s as %s: extension %s derived from %s", uri, rel.as_posix(), chosen, mime_type)


def source_dir(source: str, download_dir: str | None = None) -> Path:
    """Return the directory that holds all documents for *source*.

    Thin wrapper over :class:`~soliplex.agents.store.DownloadTarget` so the
    layout has one definition; kept because callers and tests read better with
    a path than with a target.
    """
    from soliplex.agents.store import get_document_store

    return get_document_store(source, download_dir).target.root


async def write_document(
    source: str,
    uri: str,
    content: bytes | str,
    mime_type: str | None,
    metadata: dict | None = None,
    *,
    ingestion_type: str | None = None,
    source_url: str | None = None,
    download_dir: str | None = None,
) -> Path:
    """Write *content* and its metadata sidecar to the download directory.

    Args:
        source: Source identifier (becomes the per-source folder name).
        uri: Source URI; determines the relative path under the source folder.
        content: Document bytes (str is encoded as UTF-8).
        mime_type: MIME type recorded in the sidecar and used for extensions.
        metadata: Additional metadata stored under the sidecar ``metadata`` key.
        ingestion_type: Method used to fetch the document (e.g. ``"fs"``,
            ``"webdav"``, ``"scm"``, ``"web"``), recorded in the sidecar.
        source_url: Full URL the document was downloaded from, recorded in the
            sidecar when available (currently WebDAV only).
        download_dir: Override for ``settings.download_dir`` (mainly for tests).

    Returns:
        The path of the written document.
    """
    from soliplex.agents.sidecar import DocumentWrite
    from soliplex.agents.sidecar import Sidecars
    from soliplex.agents.store import get_document_store

    store = get_document_store(source, download_dir)
    rel = uri_to_relpath(uri, mime_type=mime_type)
    key = rel.as_posix()
    log_extension_choice(uri, rel, mime_type)
    target = store.target.root / rel

    data = content.encode("utf-8") if isinstance(content, str) else content
    # The document and its sidecars occupy distinct keys, so they go together:
    # against object storage each is a round trip, and a document costs one
    # instead of one-per-object. The consequence is that a failed document
    # write can leave a sidecar behind, because both were already in flight.
    # That is safe -- no state row is recorded for a failed write, so the
    # sidecar has nothing claiming it and the reconcile sweep removes it as an
    # orphan on the next clean run.
    await asyncio.gather(
        store.write(key, data),
        Sidecars(store).write_all(
            key,
            DocumentWrite(
                source=source,
                uri=uri,
                content=data,
                mime_type=mime_type,
                metadata=metadata or {},
                ingestion_type=ingestion_type,
                source_url=source_url,
            ),
        ),
    )
    logger.info("wrote %s (%d bytes)", target, len(data))
    return target


async def delete_document(
    source: str,
    uri: str,
    *,
    mime_type: str | None = None,
    download_dir: str | None = None,
) -> None:
    """Remove a document and its sidecar (used for stale-file cleanup).

    Idempotent, and silent about what was actually there: see
    :meth:`~soliplex.agents.store.DocumentStore.delete` for why no existence
    result is returned. A caller that needs one checks first.

    Args:
        source: Source identifier.
        uri: Source URI of the document to remove.
        mime_type: MIME type (only needed to reproduce a synthesized extension).
        download_dir: Override for ``settings.download_dir``.
    """
    from soliplex.agents.sidecar import Sidecars
    from soliplex.agents.store import get_document_store

    store = get_document_store(source, download_dir)
    key = uri_to_relpath(uri, mime_type=mime_type).as_posix()
    await asyncio.gather(
        store.delete(key),
        Sidecars(store).delete_all(key),
    )
    logger.info("deleted document %s", store.uri(key))
