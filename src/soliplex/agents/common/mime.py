"""Single source of truth for MIME-type detection and file-extension logic.

Detection prefers, in order: an explicit content-type header (e.g. WebDAV's
``getcontenttype`` / GET ``Content-Type``), content sniffing via
:mod:`puremagic` (magic bytes), the filename extension, and finally a
fallback.

:mod:`puremagic` classifies binary formats by their magic signature but
cannot recognise plain text or Markdown (which carry none); such files
resolve via their extension. For sources without an authoritative header
(filesystem, git), callers pass ``text_fallback=True`` so an extension-less
file whose bytes look like UTF-8 text is treated as ``text/plain`` (written
``.txt``) rather than opaque ``application/octet-stream``. WebDAV does not
use that default -- it relies on the server-provided content type, so an
extension-less WebDAV file with no usable header stays
``application/octet-stream``.
"""

import logging
import mimetypes
from pathlib import Path
from pathlib import PurePosixPath

import puremagic

logger = logging.getLogger(__name__)

# MIME type -> canonical extension (dot included) where
# ``mimetypes.guess_extension`` is unhelpful (e.g. ".markdown" not ".md").
_EXT_OVERRIDES = {
    "text/markdown": ".md",
    "text/html": ".html",
    "text/plain": ".txt",
}

# Extension overrides for MIME types the stdlib doesn't know, used when
# ``mimetypes.guess_type`` returns ``None`` for a path. Maps MIME -> the
# bare extension that identifies it.
MIME_OVERRIDES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "ppsx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",  # noqa: E501
    "text/plantuml": "puml",
    "text/asciidoc": "adoc",
    "text/svg+xml": "svg",
    "application/x-latex": "latex",
    "text/python": "python",
    "text/yaml": "yaml",
    "text/toml": "toml",
    "text/json": "json",
    "text/xml": "xml",
    "text/javascript": "js",
}

# Content types that carry no useful information -- treat as "unknown" so we
# fall through to sniffing / extension rather than trusting them.
_GENERIC_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# Bytes inspected when deciding whether content is plausibly text.
_TEXT_SNIFF_BYTES = 8192


def _normalize(mime_type: str) -> str:
    """Lower-case a MIME type and drop any ``; charset=...`` parameters."""
    return mime_type.split(";")[0].strip().lower()


def sniff_bytes(data: bytes | None) -> str | None:
    """Return a MIME type detected from *data*'s magic bytes, or ``None``.

    Returns ``None`` for empty input and for content puremagic can't
    identify (plain text, Markdown, and other signature-less formats).
    """
    if not data:
        return None
    try:
        guessed = puremagic.from_string(data, mime=True)
    except (puremagic.PureError, ValueError):
        return None
    return guessed or None


def _looks_like_text(data: bytes | None) -> bool:
    """Return ``True`` when *data* is plausibly UTF-8 text.

    Rejects content containing NUL bytes and content whose leading chunk
    isn't valid UTF-8 (tolerating a multi-byte sequence split at the chunk
    boundary).
    """
    if not data:
        return False
    chunk = data[:_TEXT_SNIFF_BYTES]
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        # The chunk boundary may bisect a multi-byte character; retry with
        # up to three trailing bytes trimmed before giving up.
        for back in (1, 2, 3):
            try:
                chunk[:-back].decode("utf-8")
            except UnicodeDecodeError:
                continue
            else:
                return True
        return False
    return True


def detect_mime_type(
    path: str,
    *,
    data: bytes | None = None,
    header_type: str | None = None,
    text_fallback: bool = False,
) -> str:
    """Resolve a MIME type for *path* from the best available signal.

    Precedence: an explicit ``header_type`` (unless generic) > content
    sniffing of ``data`` > the filename extension > (when *text_fallback*
    is set and ``data`` looks like text) ``text/plain`` >
    ``application/octet-stream``.
    """
    if header_type:
        norm = _normalize(header_type)
        if norm and norm not in _GENERIC_TYPES:
            return norm

    sniffed = sniff_bytes(data)
    if sniffed:
        return sniffed

    path_str = str(path)
    mime_type = mimetypes.guess_type(path_str)[0]
    if mime_type:
        return mime_type

    for mime, ext in MIME_OVERRIDES.items():
        if path_str.endswith(ext):
            return mime

    if "/issues/" in path_str:
        # Rendered git issues have no MIME type; treat as Markdown.
        return "text/markdown"

    if text_fallback and _looks_like_text(data):
        return "text/plain"

    logger.debug("unrecognized mime type for %s", path_str)
    return "application/octet-stream"


def guess_extension(mime_type: str | None) -> str:
    """Return a file extension (including the dot) for *mime_type*, or ``""``."""
    if not mime_type:
        return ""
    mt = _normalize(mime_type)
    if mt in _EXT_OVERRIDES:
        return _EXT_OVERRIDES[mt]
    return mimetypes.guess_extension(mt) or ""


def ensure_extension(name: str, mime_type: str | None) -> str:
    """Return *name* carrying the extension implied by *mime_type*.

    Adds an extension when *name* has none, replaces one that clearly
    mismatches, and keeps one that is already correct (or already resolves
    to the same MIME type, e.g. ``.htm`` for ``text/html``).
    """
    want = guess_extension(mime_type)
    if not want:
        return name
    suffix = PurePosixPath(name).suffix
    cur = suffix.lower()
    if cur == want:
        return name
    if cur and mimetypes.guess_type(name)[0] == _normalize(mime_type):
        return name
    if not cur:
        return name + want
    return name[: -len(suffix)] + want


def extension_allowed(mime_type: str | None, allowed_extensions: list[str]) -> bool:
    """Return ``True`` when *mime_type*'s extension is in *allowed_extensions*."""
    return guess_extension(mime_type).lstrip(".") in allowed_extensions


def passes_extension_prefilter(name: str, allowed_extensions: list[str] | None) -> bool:
    """Return ``True`` when *name* should be fetched for content typing.

    A coarse pre-download gate: files whose extension is in
    *allowed_extensions* pass, and so do extension-less files (their real
    type is only known once their bytes are sniffed). Files carrying a
    disallowed extension are dropped without downloading. The authoritative
    filter runs later against the detected MIME type (:func:`extension_allowed`).
    """
    if allowed_extensions is None:
        return True
    ext = Path(name).suffix.lstrip(".")
    return not ext or ext in allowed_extensions
