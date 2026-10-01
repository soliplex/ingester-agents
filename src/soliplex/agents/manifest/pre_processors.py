"""Built-in pre-process steps.

A pre-process step is referenced from a manifest's ``config.pre_process`` list
by dotted path and called as ``method(document, **kwargs)`` on each new or
changed document before it is stored (see
:mod:`soliplex.agents.manifest.pre_process`). Steps work on
``document.path`` -- a local spooled copy -- so they behave the same whether
the download store is the local filesystem or S3.

None of them runs unless a manifest lists it.
"""

import logging
import re

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from soliplex.agents.manifest.pre_process import PreProcessDocument
from soliplex.agents.manifest.pre_process import PreProcessResult
from soliplex.agents.manifest.pre_process import PreProcessStatus

logger = logging.getLogger(__name__)


def check_pdf_password(
    document: PreProcessDocument,
    *,
    skip_invalid: bool = True,
    skip_owner_restricted: bool = False,
):
    """Skip PDFs that cannot be read without a password.

    pdfium reports a missing user password as ``FPDF_ERR_PASSWORD``; any other
    open failure (truncated, not a PDF) is *unreadable*. A PDF that opens
    without a password but carries a security handler is encrypted with an
    owner password only -- printing or copying may be restricted, but it is
    readable, so it is kept unless *skip_owner_restricted*.

    Args:
        document: The document to check.
        skip_invalid: Skip PDFs pdfium cannot open for a reason other than a
            password (default), rather than storing them.
        skip_owner_restricted: Also skip owner-password-only PDFs.
    """
    try:
        pdf = pdfium.PdfDocument(document.path)
    except pdfium.PdfiumError as e:
        if e.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            return PreProcessStatus.SKIP, "password protected"
        if skip_invalid:
            return PreProcessStatus.SKIP, f"unreadable PDF: {e}"
        return PreProcessStatus.CONTINUE, f"unreadable PDF kept: {e}"
    try:
        revision = pdfium_c.FPDF_GetSecurityHandlerRevision(pdf)
        if revision != -1:
            message = f"owner-password restricted (security handler r{revision})"
            status = PreProcessStatus.SKIP if skip_owner_restricted else PreProcessStatus.CONTINUE
            return status, message
        return PreProcessStatus.CONTINUE
    finally:
        pdf.close()


# Matches a standalone AsciiDoc block attribute line, e.g. [%autowidth] or
# [cols="1,2", options="header"].  Only stripped when it appears immediately
# before a |=== table delimiter.
_BLOCK_ATTR = re.compile(r"^\[.*\]$")

# Non-pipe, non-whitespace specifier characters directly before a | cell
# delimiter (e.g. "^.^h|" -> "|").  Only applied to lines inside a |=== block
# that do not already start with |.
_CELL_SPEC = re.compile(r"[^|\s]+(?=\|)")

# AsciiDoc block directives that are unresolvable at ingest time: include::
# and image:: (block macro form, always at the start of a line).
_DIRECTIVE = re.compile(r"^(include|image)::")


def fix_asciidoc(document: PreProcessDocument):
    """Rewrite AsciiDoc so docling's regex-based backend can parse it.

    Docling's AsciiDoc backend cannot handle:

    1. block attribute lines (``[%autowidth, cols="..."]``) before a table --
       they get swallowed into caption data, corrupting table captions;
    2. cell-format specifiers before pipes (``^.^h|Field``) -- a row not
       starting with ``|`` ends the table early with empty data, raising
       ``max() arg is an empty sequence``;
    3. ``include::`` / ``image::`` block directives -- unresolvable at ingest
       time, they produce parse errors or stray text;
    4. blank lines inside ``|===`` blocks -- any non-table line ends the
       table, so multi-line cell format closes it after the header row.

    All four are removed. Returns MODIFIED with the rewritten content, or
    CONTINUE when there was nothing to remove.
    """
    text = document.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_table = False
    removed = 0
    i = 0

    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip("\n\r")

        # Fix 1: drop block attribute lines immediately before a |=== table
        # delimiter (consecutive [attr] blocks are all dropped).
        if not in_table and _BLOCK_ATTR.match(stripped):
            j = i + 1
            while j < len(lines) and _BLOCK_ATTR.match(lines[j].rstrip("\n\r")):
                j += 1
            if j < len(lines) and lines[j].strip() == "|===":
                removed += j - i
                i = j  # skip all [attr] lines; resume from |===
                continue

        # Track table open/close.
        if stripped == "|===":
            in_table = not in_table
            out.append(raw)
            i += 1
            continue

        # Fix 4: drop blank lines inside table blocks.
        if in_table and not stripped:
            removed += 1
            i += 1
            continue

        # Fix 2: strip cell-format specifiers from rows inside a table block
        # that don't already start with |.
        if in_table and "|" in stripped and not stripped.startswith("|"):
            raw, count = _CELL_SPEC.subn("", raw)
            removed += count

        # Fix 3: drop include:: and image:: block directives entirely.
        if _DIRECTIVE.match(stripped):
            removed += 1
            i += 1
            continue

        out.append(raw)
        i += 1

    result = "".join(out)
    if result == text:
        return PreProcessStatus.CONTINUE
    return PreProcessResult(
        PreProcessStatus.MODIFIED,
        f"stripped {removed} docling-incompatible construct(s)",
        data=result.encode("utf-8"),
    )
