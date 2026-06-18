"""Write fetched documents and metadata sidecars to the local filesystem.

Replaces the previous behaviour of POSTing documents to the Soliplex
Ingester. Each document is written under
``<download_dir>/<sanitized-source>/<source-relative-path>`` and is
accompanied by a ``<filename>.meta.json`` sidecar carrying its MIME
type and any other available metadata.
"""

import hashlib
import json
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit

from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

# Suffix appended to a document's filename to form its metadata sidecar.
META_SUFFIX = ".meta.json"

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

# Preferred extensions for MIME types where guess_extension is unhelpful
# (e.g. it returns ".markdown" rather than ".md").
_EXT_OVERRIDES = {
    "text/markdown": ".md",
    "text/html": ".html",
    "text/plain": ".txt",
}


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


def _guess_ext(mime_type: str | None) -> str:
    """Return a file extension (including the dot) for a MIME type, or ``""``."""
    if not mime_type:
        return ""
    mt = mime_type.split(";")[0].strip().lower()
    if mt in _EXT_OVERRIDES:
        return _EXT_OVERRIDES[mt]
    return mimetypes.guess_extension(mt) or ""


def needs_extension(uri: str) -> bool:
    """Return True when *uri* names content that may lack a real extension.

    Web URLs (HTML pages) and git issues are written from rendered content
    whose URI often has no file extension; everything else (repo files,
    filesystem paths, WebDAV paths) already carries one. Deriving this
    purely from the URI keeps :func:`write_document` and
    :func:`delete_document` symmetric without the caller tracking a flag.
    """
    return urlsplit(uri.strip()).scheme in ("http", "https") or "/issues/" in uri


def uri_to_relpath(uri: str, *, mime_type: str | None = None) -> Path:
    """Map a source URI to a safe relative path under the source directory.

    Preserves the source directory structure where possible. URLs are
    mapped to ``host/path`` and gain a synthesized filename when they end
    in ``/``. Path traversal segments (``..``) are dropped. When the URI
    names extension-less content (see :func:`needs_extension`) and the
    final segment has no extension, one is derived from ``mime_type``
    (e.g. git issues -> ``.md``).

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
        rel_segs.append("index" + _guess_ext(mime_type))
    elif needs_extension(raw) and "." not in rel_segs[-1]:
        rel_segs[-1] = rel_segs[-1] + _guess_ext(mime_type)

    return Path(*rel_segs)


def source_dir(source: str, download_dir: str | None = None) -> Path:
    """Return the directory that holds all documents for *source*."""
    base = Path(download_dir if download_dir is not None else settings.download_dir)
    return base / sanitize_source(source)


def write_document(
    source: str,
    uri: str,
    content: bytes | str,
    mime_type: str | None,
    metadata: dict | None = None,
    *,
    download_dir: str | None = None,
) -> Path:
    """Write *content* and its metadata sidecar to the download directory.

    Args:
        source: Source identifier (becomes the per-source folder name).
        uri: Source URI; determines the relative path under the source folder.
        content: Document bytes (str is encoded as UTF-8).
        mime_type: MIME type recorded in the sidecar and used for extensions.
        metadata: Additional metadata stored under the sidecar ``metadata`` key.
        download_dir: Override for ``settings.download_dir`` (mainly for tests).

    Returns:
        The path of the written document.
    """
    rel = uri_to_relpath(uri, mime_type=mime_type)
    target = source_dir(source, download_dir) / rel
    target.parent.mkdir(parents=True, exist_ok=True)

    data = content.encode("utf-8") if isinstance(content, str) else content
    target.write_bytes(data)

    sidecar = target.with_name(target.name + META_SUFFIX)
    payload = {
        "mime_type": mime_type,
        "source": source,
        "source_uri": uri,
        "sha256": hashlib.sha256(data, usedforsecurity=False).hexdigest(),
        "size": len(data),
        "metadata": metadata or {},
    }
    sidecar.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("wrote %s (%d bytes)", target, len(data))
    return target


def delete_document(
    source: str,
    uri: str,
    *,
    mime_type: str | None = None,
    download_dir: str | None = None,
) -> bool:
    """Remove a document and its sidecar (used for stale-file cleanup).

    Args:
        source: Source identifier.
        uri: Source URI of the document to remove.
        mime_type: MIME type (only needed to reproduce a synthesized extension).
        download_dir: Override for ``settings.download_dir``.

    Returns:
        True if the document or its sidecar existed and was removed.
    """
    rel = uri_to_relpath(uri, mime_type=mime_type)
    target = source_dir(source, download_dir) / rel
    removed = False
    for path in (target, target.with_name(target.name + META_SUFFIX)):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
    if removed:
        logger.info("deleted stale document %s", target)
    return removed
