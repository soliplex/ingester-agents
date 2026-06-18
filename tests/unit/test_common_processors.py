"""Unit tests for soliplex.agents.common.processors."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents.common.processors import _REGISTRY
from soliplex.agents.common.processors import FileProcessor
from soliplex.agents.common.processors import ProcessorRejected
from soliplex.agents.common.processors import register
from soliplex.agents.common.processors import run_processors
from soliplex.agents.common.processors.asciidoc import AsciiDocTableProcessor
from soliplex.agents.common.processors.pdf import PdfValidator

# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_registry():
    """Snapshot and restore _REGISTRY around a test so registrations don't leak."""
    original = {k: list(v) for k, v in _REGISTRY.items()}
    yield
    _REGISTRY.clear()
    _REGISTRY.update(original)


# ---------------------------------------------------------------------------
# Registry behaviour
# ---------------------------------------------------------------------------


def test_run_processors_no_match(tmp_path):
    """run_processors is a no-op when no processor is registered for the type."""
    f = tmp_path / "doc.xyz"
    f.write_text("hello", encoding="utf-8")
    run_processors(f, "application/x-unknown")
    assert f.read_text(encoding="utf-8") == "hello"


def test_run_processors_calls_processor(tmp_path, clean_registry):
    """Registered processor's process() is called with the correct arguments."""

    @register("text/test")
    class _Spy(FileProcessor):
        calls: list = []

        def process(self, path: Path, mime_type: str) -> None:
            _Spy.calls.append((path, mime_type))

    f = tmp_path / "doc.txt"
    f.write_text("x", encoding="utf-8")
    run_processors(f, "text/test")
    assert _Spy.calls == [(f, "text/test")]


def test_run_processors_logs_exception_on_failure(tmp_path, clean_registry):
    """A processor that raises does not propagate — the exception is logged."""

    @register("text/boom")
    class _Boom(FileProcessor):
        def process(self, path: Path, mime_type: str) -> None:
            raise RuntimeError("boom")

    f = tmp_path / "doc.txt"
    f.write_text("x", encoding="utf-8")
    # Should not raise
    run_processors(f, "text/boom")


def test_register_decorator_multiple_mime_types(clean_registry):
    """@register with multiple MIME types adds the class to each."""

    @register("text/a", "text/b")
    class _Multi(FileProcessor):
        def process(self, path: Path, mime_type: str) -> None:
            pass

    assert _Multi in _REGISTRY.get("text/a", [])
    assert _Multi in _REGISTRY.get("text/b", [])


# ---------------------------------------------------------------------------
# AsciiDocTableProcessor — Fix 1: block attribute lines
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "doc.adoc"
    f.write_text(content, encoding="utf-8")
    return f


def test_asciidoc_no_change_needed(tmp_path):
    """A clean file with no specifiers is not rewritten."""
    content = ".Title\n|===\n| A | B\n|===\n"
    f = _write(tmp_path, content)
    mtime_before = f.stat().st_mtime_ns
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.stat().st_mtime_ns == mtime_before
    assert f.read_text(encoding="utf-8") == content


def test_asciidoc_strips_single_block_attribute(tmp_path):
    """A [attr] line immediately before |=== is removed."""
    content = ".Title\n[%autowidth]\n|===\n| A | B\n|===\n"
    expected = ".Title\n|===\n| A | B\n|===\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_strips_multiple_consecutive_block_attributes(tmp_path):
    """Multiple consecutive [attr] lines before |=== are all removed."""
    content = '.Title\n[%autowidth]\n[cols="1,2"]\n|===\n| A | B\n|===\n'
    expected = ".Title\n|===\n| A | B\n|===\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_block_attribute_not_before_table_is_kept(tmp_path):
    """A [attr] line NOT followed by |=== is left untouched."""
    content = "[NOTE]\nThis is a note.\n\n|===\n| A | B\n|===\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# AsciiDocTableProcessor — Fix 2: cell-format specifiers
# ---------------------------------------------------------------------------


def test_asciidoc_fixes_header_cell_specifiers(tmp_path):
    """Cell specifiers like ^.^h| are stripped inside a table block."""
    content = "|===\n^.^h|Field ^.^h| Description\n| a | b\n|===\n"
    expected = "|===\n|Field | Description\n| a | b\n|===\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_rows_starting_with_pipe_are_unchanged(tmp_path):
    """Normal data rows that already start with | are not modified."""
    content = "|===\n| foo | bar\n| baz | qux\n|===\n"
    f = _write(tmp_path, content)
    mtime_before = f.stat().st_mtime_ns
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# AsciiDocTableProcessor — combined (mirrors table_test.adoc)
# ---------------------------------------------------------------------------


_TABLE_TEST_INPUT = """\

.Component Naming Schema - Field Definitions
[%autowidth, cols="^.^40,<.^60"]
|===
^.^h|Field               ^.^h| Directions & Description
| n                      | Use "n" to represents the parent node.
| [node number]          | Use a unique, usually serial, integer.
|===
"""

_TABLE_TEST_EXPECTED = """\

.Component Naming Schema - Field Definitions
|===
|Field               | Directions & Description
| n                      | Use "n" to represents the parent node.
| [node number]          | Use a unique, usually serial, integer.
|===
"""


def test_asciidoc_combined_fixes(tmp_path):
    """Both fixes are applied together on a realistic table_test.adoc excerpt."""
    f = _write(tmp_path, _TABLE_TEST_INPUT)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == _TABLE_TEST_EXPECTED


def test_asciidoc_idempotent(tmp_path):
    """Running the processor twice produces the same result."""
    f = _write(tmp_path, _TABLE_TEST_INPUT)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    after_first = f.read_text(encoding="utf-8")
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == after_first


# ---------------------------------------------------------------------------
# AsciiDocTableProcessor — Fix 3: include/image directives
# ---------------------------------------------------------------------------


def test_asciidoc_removes_include_directive(tmp_path):
    """include:: lines are dropped."""
    content = "Some text.\ninclude::other.adoc[]\nMore text.\n"
    expected = "Some text.\nMore text.\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_removes_image_directive(tmp_path):
    """image:: lines are dropped."""
    content = "Some text.\nimage::diagram.png[Alt text]\nMore text.\n"
    expected = "Some text.\nMore text.\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_removes_multiple_directives(tmp_path):
    """Multiple include:: and image:: lines are all removed."""
    content = "Title\ninclude::a.adoc[]\nimage::fig1.png[]\ninclude::b.adoc[leveloffset=+1]\nBody.\n"
    expected = "Title\nBody.\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_inline_image_macro_is_kept(tmp_path):
    """Inline image: (single colon) is NOT removed — only block image:: is."""
    content = "See image:icon.png[icon] for details.\n"
    f = _write(tmp_path, content)
    mtime_before = f.stat().st_mtime_ns
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# AsciiDocTableProcessor — Fix 4: blank lines inside table blocks
# ---------------------------------------------------------------------------


def test_asciidoc_strips_blank_lines_inside_table(tmp_path):
    """Blank lines inside |=== blocks are removed."""
    content = "|===\n| Item | Status\n\n| Foo | Bar\n|===\n"
    expected = "|===\n| Item | Status\n| Foo | Bar\n|===\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


def test_asciidoc_preserves_blank_lines_outside_table(tmp_path):
    """Blank lines outside |=== blocks are left untouched."""
    content = "Para one.\n\nPara two.\n"
    f = _write(tmp_path, content)
    mtime_before = f.stat().st_mtime_ns
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.stat().st_mtime_ns == mtime_before


def test_asciidoc_multiline_cell_format(tmp_path):
    """Multi-line cell format (one cell per line, blank-line row separators) is collapsed."""
    content = "|===\n| Item | Status\n\n| Foo\n| Bar\n\n| Baz\n| Qux\n|===\n"
    expected = "|===\n| Item | Status\n| Foo\n| Bar\n| Baz\n| Qux\n|===\n"
    f = _write(tmp_path, content)
    AsciiDocTableProcessor().process(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == expected


# ---------------------------------------------------------------------------
# run_processors integration — asciidoc processor is invoked end-to-end
# ---------------------------------------------------------------------------


def test_run_processors_invokes_asciidoc_for_asciidoc_mime(tmp_path):
    """run_processors dispatches to AsciiDocTableProcessor for text/asciidoc."""
    f = _write(tmp_path, _TABLE_TEST_INPUT)
    run_processors(f, "text/asciidoc")
    assert f.read_text(encoding="utf-8") == _TABLE_TEST_EXPECTED


# ---------------------------------------------------------------------------
# ProcessorRejected — propagation through run_processors
# ---------------------------------------------------------------------------


def test_run_processors_propagates_rejection(tmp_path, clean_registry):
    """ProcessorRejected escapes run_processors rather than being swallowed."""

    @register("text/reject-test")
    class _Rejecter(FileProcessor):
        def process(self, path: Path, mime_type: str) -> None:
            raise ProcessorRejected("not acceptable")

    f = tmp_path / "doc.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ProcessorRejected, match="not acceptable"):
        run_processors(f, "text/reject-test")


# ---------------------------------------------------------------------------
# PdfValidator
# ---------------------------------------------------------------------------


def _pdf_path(tmp_path: Path) -> Path:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    return f


def test_pdf_valid_opens_cleanly(tmp_path):
    """No exception when PdfDocument opens successfully."""
    mock_doc = MagicMock()
    with patch("soliplex.agents.common.processors.pdf.pdfium.PdfDocument", return_value=mock_doc):
        PdfValidator().process(_pdf_path(tmp_path), "application/pdf")
    mock_doc.close.assert_called_once()


def test_pdf_password_protected_raises_rejected(tmp_path):
    """PdfiumError (e.g. password required) is converted to ProcessorRejected."""
    import pypdfium2 as pdfium

    with patch(
        "soliplex.agents.common.processors.pdf.pdfium.PdfDocument",
        side_effect=pdfium.PdfiumError("FPDF_ERR_PASSWORD"),
    ):
        with pytest.raises(ProcessorRejected):
            PdfValidator().process(_pdf_path(tmp_path), "application/pdf")


def test_pdf_corrupt_raises_rejected(tmp_path):
    """Any PdfiumError (not just password) results in ProcessorRejected."""
    import pypdfium2 as pdfium

    with patch(
        "soliplex.agents.common.processors.pdf.pdfium.PdfDocument",
        side_effect=pdfium.PdfiumError("FPDF_ERR_FILE"),
    ):
        with pytest.raises(ProcessorRejected):
            PdfValidator().process(_pdf_path(tmp_path), "application/pdf")


def test_run_processors_invokes_pdf_for_pdf_mime(tmp_path):
    """run_processors dispatches to PdfValidator for application/pdf."""
    mock_doc = MagicMock()
    with patch("soliplex.agents.common.processors.pdf.pdfium.PdfDocument", return_value=mock_doc):
        run_processors(_pdf_path(tmp_path), "application/pdf")
    mock_doc.close.assert_called_once()
