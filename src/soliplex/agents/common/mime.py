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

# Single bidirectional source of truth: canonical MIME type -> the bare
# extensions it maps to, most-canonical first. Covers types the stdlib
# ``mimetypes`` DB is missing or resolves unhelpfully (e.g. ".markdown"
# instead of ".md", or OOXML office types absent from a minimal container's
# mime database).
#
# The FIRST extension is canonical -- used when naming/storing a file
# (:func:`guess_extension`). EVERY extension is an accepted alias, both when
# filtering (:func:`extension_allowed` / :func:`extensions_for`) and when
# detecting a MIME from a filename (:func:`detect_mime_type`). Deriving both
# directions from one table keeps them from drifting apart.
_MIME_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "text/markdown": ("md",),
    "text/html": ("html",),
    "text/plain": ("txt",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx",),  # noqa: E501
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("pptx",),  # noqa: E501
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": ("ppsx",),  # noqa: E501
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("xlsx",),  # noqa: E501
    "text/plantuml": ("puml", "plantuml"),
    "text/asciidoc": ("adoc", "asciidoc"),
    "text/svg+xml": ("svg",),
    "application/x-latex": ("latex",),
    "text/python": ("python",),
    "text/yaml": ("yaml", "yml"),
    "text/toml": ("toml",),
    "text/json": ("json",),
    "text/xml": ("xml",),
    "text/javascript": ("js",),
}

# Reverse index (extension -> MIME) for detecting a MIME from a filename when
# the stdlib doesn't know the extension. Built from _MIME_EXTENSIONS; the first
# MIME to claim an extension wins (insertion order preserved).
_EXTENSION_MIME: dict[str, str] = {ext: mime for mime, exts in _MIME_EXTENSIONS.items() for ext in exts}

# Content types that carry no useful information -- treat as "unknown" so we
# fall through to sniffing / extension rather than trusting them.
_GENERIC_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# ZIP-container types whose sniffed identity is unreliable. Every OOXML format
# is a ZIP, so magic bytes alone cannot tell docx from pptx from xlsx --
# puremagic lists all three against the bare ``504b0304`` header at equal
# confidence (first one wins) and maps the more specific Office 2007+ header
# ``504b030414000600`` to the *docx* type outright. Sniffing therefore reports
# every OOXML document as docx, which renamed .pptx/.xlsx/.ppsx files to .docx
# on the way to disk (:func:`ensure_extension`) and, where the allowlist had no
# ``docx`` entry, dropped them entirely. For these types the filename extension
# is the better signal, so :func:`detect_mime_type` lets it win.
#
# Both spellings of the ZIP type are listed because Windows' registry-backed
# mime database answers ``application/x-zip-compressed`` for ".zip" where
# Linux answers ``application/zip``; a plain archive must keep its own type on
# either host rather than inheriting puremagic's docx guess.
_ZIP_TYPES = ("application/zip", "application/x-zip-compressed")
_CONTAINER_TYPES = frozenset({*_ZIP_TYPES, *(m for m in _MIME_EXTENSIONS if "openxmlformats" in m)})

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


def _mime_from_extension(path_str: str) -> str | None:
    """Return the MIME type implied by *path_str*'s extension, or ``None``.

    Consults the stdlib first, then the :data:`_EXTENSION_MIME` overrides for
    extensions a minimal container's mime database does not carry.
    """
    mime_type = mimetypes.guess_type(path_str)[0]
    if mime_type:
        return mime_type
    lowered = path_str.lower()
    for ext, mime in _EXTENSION_MIME.items():
        if lowered.endswith(f".{ext}"):
            return mime
    return None


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

    The one inversion is :data:`_CONTAINER_TYPES`: when sniffing only proves
    the bytes are a ZIP/OOXML container, an extension naming a container type
    wins over it, because that is the only signal that separates ``.pptx``
    from ``.docx`` from ``.xlsx``.
    """
    if header_type:
        norm = _normalize(header_type)
        if norm and norm not in _GENERIC_TYPES:
            return norm

    path_str = str(path)
    sniffed = sniff_bytes(data)
    if sniffed:
        if _normalize(sniffed) not in _CONTAINER_TYPES:
            return sniffed
        # A ZIP container: trust the extension when it names one too (docx vs
        # pptx vs xlsx is only distinguishable that way), otherwise keep the
        # sniff -- an extension-less or misnamed archive has nothing better.
        from_ext = _mime_from_extension(path_str)
        if from_ext and _normalize(from_ext) in _CONTAINER_TYPES:
            return from_ext
        return sniffed

    mime_type = _mime_from_extension(path_str)
    if mime_type:
        return mime_type

    if "/issues/" in path_str:
        # Rendered git issues have no MIME type; treat as Markdown.
        return "text/markdown"

    if text_fallback and _looks_like_text(data):
        return "text/plain"

    logger.debug("unrecognized mime type for %s", path_str)
    return "application/octet-stream"


def is_container_type(mime_type: str | None) -> bool:
    """Return ``True`` when *mime_type* is a ZIP/OOXML container type.

    These are the types content sniffing cannot resolve on its own (see
    :data:`_CONTAINER_TYPES`), so they are also the only ones whose recorded
    value may be a stale docx guess rather than the document's real type.
    Callers reconciling already-written documents use this to scope repairs
    to that failure mode.
    """
    return bool(mime_type) and _normalize(mime_type) in _CONTAINER_TYPES


def guess_extension(mime_type: str | None) -> str:
    """Return the canonical file extension (including the dot) for *mime_type*.

    Consults :data:`_MIME_EXTENSIONS` (returning its first/canonical extension)
    before the stdlib, so OOXML / office / custom types resolve even where the
    runtime's ``mimetypes`` database does not know them (e.g. a minimal
    container without ``/etc/mime.types``). Returns ``""`` when unknown.
    """
    if not mime_type:
        return ""
    mt = _normalize(mime_type)
    if mt in _MIME_EXTENSIONS:
        return "." + _MIME_EXTENSIONS[mt][0]
    return mimetypes.guess_extension(mt) or ""


def extensions_for(mime_type: str | None) -> list[str]:
    """Return every accepted bare extension for *mime_type*, canonical first.

    Combines the override aliases in :data:`_MIME_EXTENSIONS` (e.g. ``puml`` and
    ``plantuml`` for PlantUML) with the stdlib's synonyms
    (``mimetypes.guess_all_extensions``, e.g. ``jpg``/``jpeg``/``jpe`` for JPEG
    or ``htm``/``html`` for HTML). Used by :func:`extension_allowed` so a file
    is accepted when *any* valid spelling of its type is in the allowlist.
    """
    if not mime_type:
        return []
    mt = _normalize(mime_type)
    exts = list(_MIME_EXTENSIONS.get(mt, ()))
    for ext in mimetypes.guess_all_extensions(mt):
        bare = ext.lstrip(".").lower()
        if bare not in exts:
            exts.append(bare)
    return exts


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
    """Return ``True`` when *any* extension for *mime_type* is allowed.

    Accepts the file when any valid spelling of its type (see
    :func:`extensions_for`) appears in *allowed_extensions*, so alias/synonym
    extensions (``plantuml`` vs ``puml``, ``jpeg`` vs ``jpg``) are all honored.
    """
    allowed = set(allowed_extensions)
    return any(ext in allowed for ext in extensions_for(mime_type))


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
