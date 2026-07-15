"""Tests for soliplex.agents.common.mime module."""

import puremagic

from soliplex.agents.common import mime

# Minimal magic-byte payloads puremagic recognises deterministically.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


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
        # Force a guess_type miss so the MIME_OVERRIDES fallback runs on every
        # platform. Linux CI ships /etc/mime.types (media-types) which knows
        # .docx, so guess_type would otherwise return early and never reach
        # the override loop; Windows lacks it. Patching keeps the branch
        # deterministic regardless of the host mime database.
        monkeypatch.setattr(mime.mimetypes, "guess_type", lambda *a, **k: (None, None))
        expected = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert mime.detect_mime_type("/x/report.docx") == expected

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


class TestPassesExtensionPrefilter:
    def test_none_allowlist_passes_everything(self):
        assert mime.passes_extension_prefilter("a.png", None) is True

    def test_extensionless_passes(self):
        assert mime.passes_extension_prefilter("/path/a", ["md", "pdf"]) is True

    def test_allowed_extension_passes(self):
        assert mime.passes_extension_prefilter("doc.pdf", ["md", "pdf"]) is True

    def test_disallowed_extension_blocked(self):
        assert mime.passes_extension_prefilter("image.png", ["md", "pdf"]) is False
