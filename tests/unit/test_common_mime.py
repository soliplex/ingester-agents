"""Tests for soliplex.agents.common.mime module."""

import io
import zipfile

import puremagic

from soliplex.agents.common import mime

# Minimal magic-byte payloads puremagic recognises deterministically.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPSX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.slideshow"


def ooxml_bytes(part: str) -> bytes:
    """Build a minimal OOXML package carrying the Office 2007+ ZIP header.

    puremagic cannot see past the ZIP wrapper: it lists docx/pptx/xlsx against
    the bare ``504b0304`` header at equal confidence and maps the longer
    ``504b030414000600`` header (what Office actually writes, forced in here)
    to the docx type. Every one of these fixtures therefore sniffs as docx,
    which is the point -- only the filename can tell them apart.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(part, "<a/>")
    raw = bytearray(buf.getvalue())
    raw[0:8] = bytes.fromhex("504b030414000600")
    return bytes(raw)


PPTX_BYTES = ooxml_bytes("ppt/presentation.xml")
XLSX_BYTES = ooxml_bytes("xl/workbook.xml")
DOCX_BYTES = ooxml_bytes("word/document.xml")


class TestSniffBytes:
    def test_empty_returns_none(self):
        assert mime.sniff_bytes(b"") is None
        assert mime.sniff_bytes(None) is None

    def test_pdf_detected(self):
        assert mime.sniff_bytes(PDF_BYTES) == "application/pdf"

    def test_unidentifiable_text_returns_none(self):
        # Plain text carries no magic signature -> PureError -> None.
        assert mime.sniff_bytes(b"just some plain text, nothing magic") is None

    def test_falsy_result_returns_none(self, monkeypatch):
        # puremagic returning an empty string maps to None.
        monkeypatch.setattr(puremagic, "from_string", lambda *a, **k: "")
        assert mime.sniff_bytes(PDF_BYTES) is None


class TestLooksLikeText:
    def test_empty_is_not_text(self):
        assert mime._looks_like_text(b"") is False
        assert mime._looks_like_text(None) is False

    def test_nul_byte_is_not_text(self):
        assert mime._looks_like_text(b"hello\x00world") is False

    def test_utf8_is_text(self):
        assert mime._looks_like_text("héllo world".encode()) is True

    def test_split_multibyte_at_boundary_is_text(self):
        # Valid text followed by a truncated multi-byte char: trimming the
        # trailing partial bytes should still decode.
        data = b"hello" + "€".encode()[:2]  # euro sign, last byte dropped
        assert mime._looks_like_text(data) is True

    def test_undecodable_is_not_text(self):
        assert mime._looks_like_text(b"\xff\xff\xff\xff") is False


class TestDetectMimeType:
    def test_header_wins_when_specific(self):
        assert mime.detect_mime_type("a.bin", header_type="application/pdf", data=b"x") == "application/pdf"

    def test_header_charset_normalized(self):
        assert mime.detect_mime_type("a", header_type="Text/HTML; charset=utf-8") == "text/html"

    def test_empty_header_ignored(self):
        assert mime.detect_mime_type("notes.md", header_type="") == "text/markdown"

    def test_whitespace_header_ignored(self):
        assert mime.detect_mime_type("notes.md", header_type="   ") == "text/markdown"

    def test_generic_header_ignored(self):
        assert mime.detect_mime_type("notes.md", header_type="application/octet-stream") == "text/markdown"

    def test_content_sniff(self):
        assert mime.detect_mime_type("mystery", data=PNG_BYTES) == "image/png"

    def test_extension_used_when_no_sniff(self):
        assert mime.detect_mime_type("report.pdf") == "application/pdf"

    def test_mime_override_by_extension(self, monkeypatch):
        # Force a guess_type miss so the _EXTENSION_MIME fallback runs on every
        # platform. Linux CI ships /etc/mime.types (media-types) which knows
        # .docx, so guess_type would otherwise return early and never reach
        # the override loop; Windows lacks it. Patching keeps the branch
        # deterministic regardless of the host mime database.
        monkeypatch.setattr(mime.mimetypes, "guess_type", lambda *a, **k: (None, None))
        expected = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert mime.detect_mime_type("/x/report.docx") == expected

    def test_mime_override_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(mime.mimetypes, "guess_type", lambda *a, **k: (None, None))
        expected = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert mime.detect_mime_type("/x/REPORT.DOCX") == expected

    def test_mime_override_requires_dot_not_bare_suffix(self, monkeypatch):
        # A name merely ending in the bare token (no dot) must not match --
        # "roadoc" is not an AsciiDoc file.
        monkeypatch.setattr(mime.mimetypes, "guess_type", lambda *a, **k: (None, None))
        assert mime.detect_mime_type("/x/roadoc") == "application/octet-stream"

    def test_issues_default_markdown(self):
        assert mime.detect_mime_type("/owner/repo/issues/12") == "text/markdown"

    def test_text_fallback_for_extensionless_text(self):
        assert mime.detect_mime_type("a", data=b"plain text here", text_fallback=True) == "text/plain"

    def test_text_fallback_off_stays_octet_stream(self):
        assert mime.detect_mime_type("a", data=b"plain text here") == "application/octet-stream"

    def test_binary_extensionless_stays_octet_stream(self):
        assert mime.detect_mime_type("a", data=b"\x00\x01\x02\x03", text_fallback=True) == "application/octet-stream"

    def test_octet_stream_when_nothing_matches(self):
        assert mime.detect_mime_type("a") == "application/octet-stream"


class TestGuessExtension:
    def test_none_returns_empty(self):
        assert mime.guess_extension(None) == ""

    def test_override_markdown(self):
        assert mime.guess_extension("text/markdown") == ".md"

    def test_standard_via_mimetypes(self):
        assert mime.guess_extension("application/pdf") == ".pdf"

    def test_unknown_returns_empty(self):
        assert mime.guess_extension("application/x-madeup-type") == ""

    def test_mime_override_docx(self):
        # OOXML / custom types resolve via _MIME_EXTENSIONS even where the
        # runtime mimetypes DB does not know them.
        docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert mime.guess_extension(docx) == ".docx"

    def test_mime_override_custom_types_return_canonical(self):
        # Canonical (first) extension is returned even when aliases exist.
        assert mime.guess_extension("text/plantuml") == ".puml"
        assert mime.guess_extension("text/asciidoc") == ".adoc"


class TestExtensionsFor:
    def test_none_returns_empty(self):
        assert mime.extensions_for(None) == []

    def test_override_aliases_plus_stdlib_synonym_deduped(self, monkeypatch):
        # Table gives canonical + alias; stdlib adds a new synonym; a duplicate
        # from stdlib is deduped.
        monkeypatch.setattr(mime.mimetypes, "guess_all_extensions", lambda mt: [".puml", ".xyz"])
        assert mime.extensions_for("text/plantuml") == ["puml", "plantuml", "xyz"]

    def test_stdlib_only_when_not_in_table(self, monkeypatch):
        monkeypatch.setattr(mime.mimetypes, "guess_all_extensions", lambda mt: [".jpg", ".jpeg"])
        assert mime.extensions_for("image/jpeg") == ["jpg", "jpeg"]


class TestEnsureExtension:
    def test_no_extension_for_unknown_mime_kept(self):
        assert mime.ensure_extension("data", "application/x-madeup-type") == "data"

    def test_missing_extension_appended(self):
        assert mime.ensure_extension("a", "application/pdf") == "a.pdf"

    def test_correct_extension_kept(self):
        assert mime.ensure_extension("a.pdf", "application/pdf") == "a.pdf"

    def test_equivalent_extension_kept(self):
        # .htm already resolves to text/html -> leave it alone.
        assert mime.ensure_extension("a.htm", "text/html") == "a.htm"

    def test_wrong_extension_replaced(self):
        assert mime.ensure_extension("a.bin", "application/pdf") == "a.pdf"


class TestExtensionAllowed:
    def test_allowed(self):
        assert mime.extension_allowed("application/pdf", ["md", "pdf"]) is True

    def test_not_allowed(self):
        assert mime.extension_allowed("application/octet-stream", ["md", "pdf"]) is False

    def test_text_plain_allowed_when_txt_listed(self):
        assert mime.extension_allowed("text/plain", ["md", "pdf", "txt"]) is True

    def test_office_and_custom_types_allowed_via_overrides(self):
        # Regression: office/custom MIME types must round-trip to their
        # extension via _MIME_EXTENSIONS even when the runtime mimetypes DB does
        # not know them (the original ".docx skipped" bug).
        exts = ["md", "pdf", "docx", "adoc", "pptx", "ppsx", "txt", "xlsx", "puml"]
        for mt in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/plantuml",
            "text/asciidoc",
        ):
            assert mime.extension_allowed(mt, exts) is True

    def test_alias_extension_allowed(self):
        # C: long-form spellings the old single-extension map lacked.
        assert mime.extension_allowed("text/plantuml", ["plantuml"]) is True
        assert mime.extension_allowed("text/asciidoc", ["asciidoc"]) is True

    def test_synonym_extension_allowed(self, monkeypatch):
        # D: a synonym spelling (jpeg) is honored even though the canonical
        # guess is .jpg.
        monkeypatch.setattr(mime.mimetypes, "guess_all_extensions", lambda mt: [".jpg", ".jpeg"])
        assert mime.extension_allowed("image/jpeg", ["md", "jpeg"]) is True


class TestPassesExtensionPrefilter:
    def test_none_allowlist_passes_everything(self):
        assert mime.passes_extension_prefilter("a.png", None) is True

    def test_extensionless_passes(self):
        assert mime.passes_extension_prefilter("/path/a", ["md", "pdf"]) is True

    def test_allowed_extension_passes(self):
        assert mime.passes_extension_prefilter("doc.pdf", ["md", "pdf"]) is True

    def test_disallowed_extension_blocked(self):
        assert mime.passes_extension_prefilter("image.png", ["md", "pdf"]) is False


class TestOOXMLContainerDetection:
    """Regression: a ZIP sniff must never override an OOXML extension.

    puremagic reports every OOXML package as docx, so trusting the sniff
    renamed .pptx/.xlsx/.ppsx to .docx on the way to disk and dropped them
    outright wherever the extension allowlist had no ``docx`` entry.
    """

    def test_sniff_alone_cannot_tell_ooxml_apart(self):
        # Guards the premise: if puremagic ever learns to discriminate these,
        # this assertion fails and the override below can be revisited.
        assert mime.sniff_bytes(PPTX_BYTES) == DOCX_MIME
        assert mime.sniff_bytes(XLSX_BYTES) == DOCX_MIME

    def test_pptx_extension_beats_docx_sniff(self):
        assert mime.detect_mime_type("deck.pptx", data=PPTX_BYTES) == PPTX_MIME

    def test_ppsx_extension_beats_docx_sniff(self):
        assert mime.detect_mime_type("deck.ppsx", data=PPTX_BYTES) == PPSX_MIME

    def test_xlsx_extension_beats_docx_sniff(self):
        assert mime.detect_mime_type("book.xlsx", data=XLSX_BYTES) == XLSX_MIME

    def test_docx_still_detected(self):
        assert mime.detect_mime_type("report.docx", data=DOCX_BYTES) == DOCX_MIME

    def test_extension_override_survives_missing_mime_db(self, monkeypatch):
        # A minimal container has no /etc/mime.types, so guess_type misses and
        # the _EXTENSION_MIME override has to carry the lookup.
        monkeypatch.setattr(mime.mimetypes, "guess_type", lambda *a, **k: (None, None))
        assert mime.detect_mime_type("deck.pptx", data=PPTX_BYTES) == PPTX_MIME

    def test_zip_extension_kept_over_ooxml_sniff(self):
        # Spelling of the zip type is host-dependent (Windows says
        # application/x-zip-compressed); either is fine, docx is not.
        assert mime.detect_mime_type("bundle.zip", data=PPTX_BYTES) in mime._ZIP_TYPES

    def test_extensionless_container_falls_back_to_sniff(self):
        # Nothing better to go on -- the sniff stands rather than degrading to
        # application/octet-stream.
        assert mime.detect_mime_type("mystery", data=PPTX_BYTES) == DOCX_MIME

    def test_non_container_extension_does_not_override_sniff(self):
        # A .txt holding ZIP bytes is a mislabelled archive, not text: only
        # container-naming extensions are allowed to win.
        assert mime.detect_mime_type("notes.txt", data=PPTX_BYTES) == DOCX_MIME

    def test_non_container_sniff_still_wins(self):
        # The inversion is scoped to containers; a PDF sniff still beats a
        # wrong extension.
        assert mime.detect_mime_type("report.pptx", data=PDF_BYTES) == "application/pdf"

    def test_header_still_outranks_everything(self):
        assert mime.detect_mime_type("deck.pptx", data=PPTX_BYTES, header_type=XLSX_MIME) == XLSX_MIME

    def test_pptx_is_not_renamed_on_write(self):
        # The end-to-end symptom: ensure_extension used the sniffed docx type
        # and rewrote deck.pptx -> deck.docx.
        detected = mime.detect_mime_type("deck.pptx", data=PPTX_BYTES)
        assert mime.ensure_extension("deck.pptx", detected) == "deck.pptx"

    def test_pptx_allowed_without_docx_in_allowlist(self):
        detected = mime.detect_mime_type("deck.pptx", data=PPTX_BYTES)
        assert mime.extension_allowed(detected, ["md", "pdf", "pptx"]) is True
