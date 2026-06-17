"""AsciiDoc pre-processor for docling compatibility.

Docling's AsciiDoc backend uses simple regex-based line parsing that cannot
handle:

  1. Block attribute lines (``[%autowidth, cols="..."]``) — they have no
     handler and get swallowed into caption data, corrupting table captions.

  2. Cell-format specifiers before pipe characters (e.g. ``^.^h|Field``) —
     ``_is_table_line`` requires lines to start with ``|``, so specifier-
     prefixed header rows trigger a premature table-end with empty data,
     causing ``max() arg is an empty sequence``.

  3. ``include::`` and ``image::`` block directives — unresolvable at ingest
     time and would produce parse errors or stray text in the output.

  4. Blank lines inside ``|===`` blocks — docling treats any non-table line
     (including blank) as end-of-table.  Multi-line cell format (one cell per
     line, blank-line row separators) therefore causes a premature table-end
     after the header row; the closing ``|===`` is then misread as a table
     start, and the trailing blank triggers ``max() arg is an empty sequence``.

This processor removes all of these constructs so docling receives clean input.
"""

import re
from pathlib import Path

from soliplex.agents.common.processors import FileProcessor
from soliplex.agents.common.processors import register

# Matches a standalone AsciiDoc block attribute line, e.g. [%autowidth] or
# [cols="1,2", options="header"].  We only strip these when they appear
# immediately before a |=== table delimiter.
_BLOCK_ATTR = re.compile(r"^\[.*\]$")

# Strips non-pipe, non-whitespace specifier characters that appear directly
# before a | cell delimiter (e.g. "^.^h|" → "|").  Only applied to lines
# inside a |=== block that do not already start with |.
_CELL_SPEC = re.compile(r"[^|\s]+(?=\|)")

# Matches AsciiDoc block directives that are unresolvable at ingest time:
# include:: and image:: (block macro form, always at the start of a line).
_DIRECTIVE = re.compile(r"^(include|image)::")


@register("text/asciidoc", "text/x-asciidoc")
class AsciiDocTableProcessor(FileProcessor):
    """Rewrite AsciiDoc files so docling's backend can parse them."""

    def process(self, path: Path, mime_type: str) -> None:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        in_table = False
        i = 0

        while i < len(lines):
            raw = lines[i]
            stripped = raw.rstrip("\n\r")

            # Fix 1: drop block attribute lines that appear immediately before
            # a |=== table delimiter (consecutive [attr] blocks are all dropped).
            if not in_table and _BLOCK_ATTR.match(stripped):
                j = i + 1
                while j < len(lines) and _BLOCK_ATTR.match(lines[j].rstrip("\n\r")):
                    j += 1
                if j < len(lines) and lines[j].strip() == "|===":
                    i = j  # skip all [attr] lines; resume from |===
                    continue

            # Track table open/close.
            if stripped == "|===":
                in_table = not in_table
                out.append(raw)
                i += 1
                continue

            # Fix 4: drop blank lines inside table blocks.  Docling's parser
            # interprets any non-table-line as end-of-table, so a blank line
            # between multi-line cell rows causes a premature table close.
            if in_table and not stripped:
                i += 1
                continue

            # Fix 2: strip cell-format specifiers from header/data rows inside
            # a table block that don't already start with |.
            if in_table and "|" in stripped and not stripped.startswith("|"):
                raw = _CELL_SPEC.sub("", raw)

            # Fix 3: drop include:: and image:: block directives entirely.
            if _DIRECTIVE.match(stripped):
                i += 1
                continue

            out.append(raw)
            i += 1

        result = "".join(out)
        if result != text:
            path.write_text(result, encoding="utf-8")
