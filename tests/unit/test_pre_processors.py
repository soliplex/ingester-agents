"""Tests for the built-in pre-process steps -- 100% branch coverage required.

``check_pdf_password`` runs against real PDFs (``tests/fixtures/pdf``, see its
README) so pdfium's actual error codes and security-handler detection are
exercised. The AsciiDoc cases are ported from the retired write-time
``AsciiDocTableProcessor`` and must keep producing the same output.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from soliplex.agents.manifest import pre_processors
from soliplex.agents.manifest.pre_process import PreProcessDocument
from soliplex.agents.manifest.pre_process import PreProcessStatus
from soliplex.agents.manifest.pre_processors import check_pdf_password
from soliplex.agents.manifest.pre_processors import fix_asciidoc

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pdf"


def _doc(path: Path, mime_type="application/pdf") -> PreProcessDocument:
    return PreProcessDocument("src", path.name, path.name, mime_type, path, path.parent, "sha")


# --- check_pdf_password ----------------------------------------------------------


def test_valid_pdf_continues():
    assert check_pdf_password(_doc(FIXTURES / "valid.pdf")) is PreProcessStatus.CONTINUE


def test_user_password_pdf_is_skipped():
    assert check_pdf_password(_doc(FIXTURES / "user_password.pdf")) == (PreProcessStatus.SKIP, "password protected")


def test_user_password_pdf_is_skipped_regardless_of_flags():
    doc = _doc(FIXTURES / "user_password.pdf")
    assert check_pdf_password(doc, skip_invalid=False)[0] is PreProcessStatus.SKIP


def test_owner_only_pdf_is_kept_with_a_message():
    status, message = check_pdf_password(_doc(FIXTURES / "owner_only.pdf"))
    assert status is PreProcessStatus.CONTINUE
    assert message.startswith("owner-password restricted (security handler r")


def test_owner_only_pdf_skipped_when_asked():
    status, message = check_pdf_password(_doc(FIXTURES / "owner_only.pdf"), skip_owner_restricted=True)
    assert status is PreProcessStatus.SKIP
    assert message.startswith("owner-password restricted")


def test_truncated_pdf_is_skipped():
    status, message = check_pdf_password(_doc(FIXTURES / "truncated.pdf"))
    assert status is PreProcessStatus.SKIP
    assert message.startswith("unreadable PDF: ")


def test_truncated_pdf_kept_when_not_skipping_invalid():
    status, message = check_pdf_password(_doc(FIXTURES / "truncated.pdf"), skip_invalid=False)
    assert status is PreProcessStatus.CONTINUE
    assert message.startswith("unreadable PDF kept: ")


@pytest.mark.parametrize("name", ["valid.pdf", "owner_only.pdf"])
def test_pdf_is_always_closed(name):
    real = pre_processors.pdfium.PdfDocument
    opened = []

    def spy(path):
        pdf = real(path)
        opened.append(pdf)
        return pdf

    with patch.object(pre_processors.pdfium, "PdfDocument", side_effect=spy):
        check_pdf_password(_doc(FIXTURES / name))
    (pdf,) = opened
    # pypdfium2 drops its raw handle on close.
    assert pdf.raw is None


# --- fix_asciidoc ----------------------------------------------------------------


def _fix(tmp_path, content: str):
    path = tmp_path / "doc.adoc"
    path.write_bytes(content.encode("utf-8"))
    return fix_asciidoc(_doc(path, "text/asciidoc"))


def _fixed(tmp_path, content: str) -> str:
    result = _fix(tmp_path, content)
    assert result.status is PreProcessStatus.MODIFIED
    return result.data.decode("utf-8")


def test_asciidoc_no_change_needed(tmp_path):
    assert _fix(tmp_path, ".Title\n|===\n| A | B\n|===\n") is PreProcessStatus.CONTINUE


def test_asciidoc_strips_single_block_attribute(tmp_path):
    result = _fix(tmp_path, ".Title\n[%autowidth]\n|===\n| A | B\n|===\n")
    assert result.data.decode("utf-8") == ".Title\n|===\n| A | B\n|===\n"
    assert result.message == "stripped 1 docling-incompatible construct(s)"


def test_asciidoc_strips_multiple_consecutive_block_attributes(tmp_path):
    out = _fixed(tmp_path, '.Title\n[%autowidth]\n[cols="1,2"]\n|===\n| A | B\n|===\n')
    assert out == ".Title\n|===\n| A | B\n|===\n"


def test_asciidoc_block_attribute_not_before_table_is_kept(tmp_path):
    assert _fix(tmp_path, "[NOTE]\nThis is a note.\n\n|===\n| A | B\n|===\n") is PreProcessStatus.CONTINUE


def test_asciidoc_block_attribute_at_end_of_file_is_kept(tmp_path):
    assert _fix(tmp_path, "Text.\n[NOTE]\n") is PreProcessStatus.CONTINUE


def test_asciidoc_fixes_header_cell_specifiers(tmp_path):
    out = _fixed(tmp_path, "|===\n^.^h|Field ^.^h| Description\n| a | b\n|===\n")
    assert out == "|===\n|Field | Description\n| a | b\n|===\n"


def test_asciidoc_rows_starting_with_pipe_are_unchanged(tmp_path):
    assert _fix(tmp_path, "|===\n| foo | bar\n| baz | qux\n|===\n") is PreProcessStatus.CONTINUE


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
    result = _fix(tmp_path, _TABLE_TEST_INPUT)
    assert result.data.decode("utf-8") == _TABLE_TEST_EXPECTED
    assert result.message == "stripped 3 docling-incompatible construct(s)"


def test_asciidoc_idempotent(tmp_path):
    once = _fixed(tmp_path, _TABLE_TEST_INPUT)
    assert _fix(tmp_path, once) is PreProcessStatus.CONTINUE


def test_asciidoc_removes_include_directive(tmp_path):
    assert _fixed(tmp_path, "Some text.\ninclude::other.adoc[]\nMore text.\n") == "Some text.\nMore text.\n"


def test_asciidoc_removes_image_directive(tmp_path):
    assert _fixed(tmp_path, "Some text.\nimage::diagram.png[Alt text]\nMore text.\n") == "Some text.\nMore text.\n"


def test_asciidoc_removes_multiple_directives(tmp_path):
    content = "Title\ninclude::a.adoc[]\nimage::fig1.png[]\ninclude::b.adoc[leveloffset=+1]\nBody.\n"
    assert _fixed(tmp_path, content) == "Title\nBody.\n"


def test_asciidoc_inline_image_macro_is_kept(tmp_path):
    assert _fix(tmp_path, "See image:icon.png[icon] for details.\n") is PreProcessStatus.CONTINUE


def test_asciidoc_strips_blank_lines_inside_table(tmp_path):
    out = _fixed(tmp_path, "|===\n| Item | Status\n\n| Foo | Bar\n|===\n")
    assert out == "|===\n| Item | Status\n| Foo | Bar\n|===\n"


def test_asciidoc_preserves_blank_lines_outside_table(tmp_path):
    assert _fix(tmp_path, "Para one.\n\nPara two.\n") is PreProcessStatus.CONTINUE


def test_asciidoc_multiline_cell_format(tmp_path):
    out = _fixed(tmp_path, "|===\n| Item | Status\n\n| Foo\n| Bar\n\n| Baz\n| Qux\n|===\n")
    assert out == "|===\n| Item | Status\n| Foo\n| Bar\n| Baz\n| Qux\n|===\n"


def test_asciidoc_not_utf8_raises(tmp_path):
    path = tmp_path / "doc.adoc"
    path.write_bytes(b"\xff\xfe bad")
    with pytest.raises(UnicodeDecodeError):
        fix_asciidoc(_doc(path, "text/asciidoc"))
