"""Tests for urls_file shared utility."""

import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents import UrlsFileFormatError
from soliplex.agents.common.urls_file import _is_on_webdav_host
from soliplex.agents.common.urls_file import _looks_like_html
from soliplex.agents.common.urls_file import is_http_url
from soliplex.agents.common.urls_file import is_webdav_path
from soliplex.agents.common.urls_file import is_webdav_url
from soliplex.agents.common.urls_file import parse_urls_content
from soliplex.agents.common.urls_file import read_text_from_url
from soliplex.agents.common.urls_file import read_text_from_webdav
from soliplex.agents.common.urls_file import read_urls_file
from soliplex.agents.common.urls_file import resolve_local_path


class TestResolveLocalPath:
    def test_absolute_path(self):
        assert resolve_local_path("/abs/path.txt") == "/abs/path.txt"

    def test_absolute_path_ignores_base_dir(self):
        assert resolve_local_path("/abs/path.txt", base_dir="/other") == "/abs/path.txt"

    def test_relative_with_base_dir_exists(self, tmp_path):
        (tmp_path / "urls.txt").write_text("url1\n")
        result = resolve_local_path("urls.txt", base_dir=str(tmp_path))
        assert result == str(tmp_path / "urls.txt")

    def test_relative_with_base_dir_not_exists(self, tmp_path):
        result = resolve_local_path("missing.txt", base_dir=str(tmp_path))
        assert result == "missing.txt"

    def test_relative_no_base_dir(self):
        result = resolve_local_path("relative.txt")
        assert result == "relative.txt"

    def test_relative_base_dir_none(self):
        result = resolve_local_path("relative.txt", base_dir=None)
        assert result == "relative.txt"


class TestReadUrlsFile:
    @pytest.mark.asyncio
    async def test_local_file(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("http://a.com\nhttp://b.com\n")
        result = await read_urls_file(str(f))
        assert result == ["http://a.com", "http://b.com"]

    @pytest.mark.asyncio
    async def test_local_file_strips_whitespace(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("  http://a.com  \n  http://b.com  \n")
        result = await read_urls_file(str(f))
        assert result == ["http://a.com", "http://b.com"]

    @pytest.mark.asyncio
    async def test_local_file_filters_blank_lines(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("http://a.com\n\n  \nhttp://b.com\n\n")
        result = await read_urls_file(str(f))
        assert result == ["http://a.com", "http://b.com"]

    @pytest.mark.asyncio
    async def test_relative_with_base_dir(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("http://a.com\n")
        result = await read_urls_file("urls.txt", base_dir=str(tmp_path))
        assert result == ["http://a.com"]

    @pytest.mark.asyncio
    async def test_s3_url(self):
        with patch(
            "soliplex.agents.common.urls_file.read_text_from_s3",
            new_callable=AsyncMock,
            return_value="http://a.com\nhttp://b.com\n",
        ) as mock_s3:
            result = await read_urls_file("s3://bucket/urls.txt")

        assert result == ["http://a.com", "http://b.com"]
        mock_s3.assert_called_once()

    @pytest.mark.asyncio
    async def test_s3_url_passes_endpoint(self):
        with (
            patch(
                "soliplex.agents.common.urls_file.read_text_from_s3",
                new_callable=AsyncMock,
                return_value="url1\n",
            ) as mock_s3,
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
        ):
            mock_settings.s3_endpoint_url = "https://minio:9000"
            await read_urls_file("s3://bucket/key.txt")

        mock_s3.assert_called_once_with("s3://bucket/key.txt", "https://minio:9000")

    @pytest.mark.asyncio
    async def test_s3_url_no_endpoint(self):
        with (
            patch(
                "soliplex.agents.common.urls_file.read_text_from_s3",
                new_callable=AsyncMock,
                return_value="url1\n",
            ) as mock_s3,
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
        ):
            mock_settings.s3_endpoint_url = None
            await read_urls_file("s3://bucket/key.txt")

        mock_s3.assert_called_once_with("s3://bucket/key.txt", None)


class TestIsWebdavUrl:
    def test_http_url(self):
        assert is_webdav_url("http://example.com/urls.txt") is True

    def test_https_url(self):
        assert is_webdav_url("https://example.com/urls.txt") is True

    def test_s3_url(self):
        assert is_webdav_url("s3://bucket/key") is False

    def test_local_path(self):
        assert is_webdav_url("/local/path.txt") is False

    def test_relative_path(self):
        assert is_webdav_url("relative.txt") is False


class TestReadTextFromWebdav:
    def _mock_create_client(self, content: bytes = b"url1\n"):
        """Return a patched create_async_webdav_client and its mock client."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.download.return_value = (content, "text/plain")
        return patch(
            "soliplex.agents.webdav.async_client.create_async_webdav_client",
            return_value=mock_client,
        ), mock_client

    @pytest.mark.asyncio
    async def test_downloads_file_with_explicit_credentials(self):
        patcher, mock_client = self._mock_create_client(b"/doc1.pdf\n/doc2.pdf\n")
        with patcher as mock_create:
            result = await read_text_from_webdav(
                "https://webdav.example.com/manifests/urls.txt",
                webdav_username="user",
                webdav_password="pass",
            )

        assert result == "/doc1.pdf\n/doc2.pdf\n"
        mock_create.assert_called_once_with("https://webdav.example.com", "user", "pass")
        mock_client.download.assert_called_once_with("/manifests/urls.txt")

    @pytest.mark.asyncio
    async def test_uses_webdav_url_override(self):
        patcher, _ = self._mock_create_client()
        with patcher as mock_create:
            await read_text_from_webdav(
                "https://webdav.example.com/urls.txt",
                webdav_url="https://override.example.com",
                webdav_username="u",
                webdav_password="p",
            )

        mock_create.assert_called_once_with("https://override.example.com", "u", "p")

    @pytest.mark.asyncio
    async def test_derives_base_url_from_full_url(self):
        patcher, mock_client = self._mock_create_client()
        with patcher as mock_create:
            await read_text_from_webdav("https://webdav.example.com:8443/deep/path/urls.txt")

        mock_create.assert_called_once_with("https://webdav.example.com:8443", None, None)
        mock_client.download.assert_called_once_with("/deep/path/urls.txt")

    @pytest.mark.asyncio
    async def test_passes_none_credentials_when_not_provided(self):
        patcher, _ = self._mock_create_client()
        with patcher as mock_create:
            await read_text_from_webdav("https://webdav.example.com/urls.txt")

        mock_create.assert_called_once_with("https://webdav.example.com", None, None)


class TestIsOnWebdavHost:
    def test_matches_explicit_override(self):
        assert _is_on_webdav_host("https://h.example.com/x.txt", "https://h.example.com") is True

    def test_differs_from_override(self):
        assert _is_on_webdav_host("http://manifest:8001/x.txt", "https://h.example.com") is False

    def test_falls_back_to_settings(self):
        with patch("soliplex.agents.common.urls_file.settings") as mock_settings:
            mock_settings.webdav_url = "https://h.example.com"
            assert _is_on_webdav_host("https://h.example.com/x.txt", None) is True

    def test_no_configured_host(self):
        with patch("soliplex.agents.common.urls_file.settings") as mock_settings:
            mock_settings.webdav_url = None
            assert _is_on_webdav_host("https://h.example.com/x.txt", None) is False


class TestReadTextFromUrl:
    def _mock_session(self, content: bytes = b"url1\n"):
        """Return a mock aiohttp ClientSession whose GET yields *content*."""
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=content)
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=mock_resp)
        return mock_session, mock_resp

    @pytest.mark.parametrize("ssl_verify,expected_ssl", [(True, None), (False, False)])
    @pytest.mark.asyncio
    async def test_reads_literal_url(self, ssl_verify, expected_ssl):
        mock_session, mock_resp = self._mock_session(b"/a.pdf\n/b.pdf\n")
        with (
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
            patch(
                "soliplex.agents.common.urls_file.aiohttp.ClientSession",
                return_value=mock_session,
            ),
        ):
            mock_settings.ssl_verify = ssl_verify
            result = await read_text_from_url("http://manifest_server:8001/urls.txt")

        assert result == "/a.pdf\n/b.pdf\n"
        mock_session.get.assert_called_once_with(
            "http://manifest_server:8001/urls.txt",
            ssl=expected_ssl,
            allow_redirects=True,
        )
        mock_resp.raise_for_status.assert_called_once()


class TestReadUrlsFileWebdav:
    @pytest.mark.asyncio
    async def test_routes_to_webdav_client_on_webdav_host(self):
        with (
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
            patch(
                "soliplex.agents.common.urls_file.read_text_from_webdav",
                new_callable=AsyncMock,
                return_value="/doc1.pdf\n/doc2.pdf\n",
            ) as mock_webdav,
        ):
            mock_settings.webdav_url = "https://webdav.example.com"
            result = await read_urls_file(
                "https://webdav.example.com/urls.txt",
                webdav_username="user",
                webdav_password="pass",
            )

        assert result == ["/doc1.pdf", "/doc2.pdf"]
        mock_webdav.assert_called_once_with(
            "https://webdav.example.com/urls.txt",
            None,
            "user",
            "pass",
        )

    @pytest.mark.asyncio
    async def test_webdav_url_override_identifies_host(self):
        # An explicit webdav_url override names the WebDAV host; a URL on that
        # host routes to the authenticated client (no settings needed).
        with patch(
            "soliplex.agents.common.urls_file.read_text_from_webdav",
            new_callable=AsyncMock,
            return_value="url1\n",
        ) as mock_webdav:
            await read_urls_file(
                "https://override.example.com/urls.txt",
                webdav_url="https://override.example.com",
                webdav_username="u",
                webdav_password="p",
            )

        mock_webdav.assert_called_once_with(
            "https://override.example.com/urls.txt",
            "https://override.example.com",
            "u",
            "p",
        )

    @pytest.mark.asyncio
    async def test_routes_to_literal_on_foreign_host(self):
        # A full URL to a different host than the WebDAV server is fetched
        # literally (no host rewrite, no webdav credentials).
        foreign = "http://manifest_server:8001/api/v1/manifest-file/mobile/urls.txt"
        with (
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
            patch(
                "soliplex.agents.common.urls_file.read_text_from_url",
                new_callable=AsyncMock,
                return_value="/doc1.pdf\n",
            ) as mock_literal,
            patch(
                "soliplex.agents.common.urls_file.read_text_from_webdav",
                new_callable=AsyncMock,
            ) as mock_webdav,
        ):
            mock_settings.webdav_url = "https://webdav.example.com"
            result = await read_urls_file(foreign)

        assert result == ["/doc1.pdf"]
        mock_literal.assert_called_once_with(foreign)
        mock_webdav.assert_not_called()


_HTML_BODIES = [
    pytest.param("<!DOCTYPE html>\n<html><body>Error</body></html>\n", id="doctype"),
    pytest.param("﻿\n\n  <html>\n<head><title>Login</title></head>\n", id="bom-blank-lines"),
    pytest.param("<HTML>\n<BODY>Maintenance</BODY>\n</HTML>\n", id="uppercase"),
]


class TestIsWebdavPath:
    @pytest.mark.parametrize("line", ["/a/b.pdf", "/x"])
    def test_valid(self, line):
        assert is_webdav_path(line) is True

    @pytest.mark.parametrize("line", ["https://x/a.pdf", "a/b.pdf", "</div>"])
    def test_invalid(self, line):
        assert is_webdav_path(line) is False


class TestIsHttpUrl:
    @pytest.mark.parametrize("line", ["https://x/a", "http://x.example.com:8080/p?q=1"])
    def test_valid(self, line):
        assert is_http_url(line) is True

    @pytest.mark.parametrize("line", ["/a", "ftp://x/a", "https://", "example.com/a"])
    def test_invalid(self, line):
        assert is_http_url(line) is False


class TestLooksLikeHtml:
    @pytest.mark.parametrize("content", _HTML_BODIES)
    def test_html(self, content):
        assert _looks_like_html(content) is True

    def test_lt_without_marker(self):
        assert _looks_like_html("<foo>\n/a.pdf\n") is False

    def test_marker_not_at_start(self):
        assert _looks_like_html("/a/<html.pdf\n") is False

    def test_marker_beyond_sniff_window(self):
        assert _looks_like_html("<x>" + " " * 5000 + "<html>") is False


class TestParseUrlsContent:
    def test_html_rejected(self):
        with pytest.raises(UrlsFileFormatError, match=r"returned HTML, not a URL list \(first line: '<!DOCTYPE html>'\)"):
            parse_urls_content("<!DOCTYPE html>\n<html></html>\n", "http://m/u.txt")

    def test_lt_without_marker_falls_to_line_validation(self):
        with pytest.raises(UrlsFileFormatError, match=r"all 2 lines were invalid; first: '<foo>'"):
            parse_urls_content("<foo>\n</foo>\n", "u.txt", is_valid_line=is_webdav_path)

    def test_comments_dropped(self):
        content = "# header\n/a.pdf\n  # indented comment\n/b.pdf\n"
        assert parse_urls_content(content, "u.txt", is_valid_line=is_webdav_path) == ["/a.pdf", "/b.pdf"]

    def test_only_comments_raises(self):
        with pytest.raises(UrlsFileFormatError, match="only comment lines"):
            parse_urls_content("# one\n# two\n", "u.txt")

    @pytest.mark.parametrize("content", ["", "\n  \n\n"])
    def test_empty_returns_empty(self, content):
        assert parse_urls_content(content, "u.txt", is_valid_line=is_webdav_path) == []

    def test_no_validator_keeps_any_line(self):
        assert parse_urls_content("# c\nanything\n/a\n", "u.txt") == ["anything", "/a"]

    def test_mixed_valid_and_invalid_warns_once(self, caplog):
        bad = [f"bad{i}" for i in range(7)]
        content = "\n".join(["/a.pdf", *bad[:4], "/b.pdf", *bad[4:], "/c.pdf"])
        with caplog.at_level(logging.WARNING, logger="soliplex.agents.common.urls_file"):
            result = parse_urls_content(content, "u.txt", is_valid_line=is_webdav_path)

        assert result == ["/a.pdf", "/b.pdf", "/c.pdf"]
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "dropped 7 invalid line(s) of 10" in message
        assert all(f"'{line}'" in message for line in bad[:5])
        assert all(f"'{line}'" not in message for line in bad[5:])

    def test_long_example_truncated(self):
        with pytest.raises(UrlsFileFormatError) as exc_info:
            parse_urls_content("x" * 300, "u.txt", is_valid_line=is_webdav_path)
        assert "'" + "x" * 120 + "...'" in str(exc_info.value)
        assert "x" * 121 not in str(exc_info.value)

    def test_all_invalid_raises_with_count_and_first(self):
        with pytest.raises(UrlsFileFormatError, match=r"u\.txt: all 2 lines were invalid; first: 'https://x/a\.pdf'"):
            parse_urls_content("https://x/a.pdf\na/b.pdf\n", "u.txt", is_valid_line=is_webdav_path)

    def test_web_validator(self):
        content = "https://x/a\n/a\nftp://x\nhttps://\n"
        assert parse_urls_content(content, "u.txt", is_valid_line=is_http_url) == ["https://x/a"]


class TestReadUrlsFileFormat:
    @pytest.mark.parametrize("content", _HTML_BODIES)
    @pytest.mark.asyncio
    async def test_local_html_rejected(self, tmp_path, content):
        f = tmp_path / "urls.txt"
        f.write_text(content, encoding="utf-8")
        with pytest.raises(UrlsFileFormatError, match="returned HTML"):
            await read_urls_file(str(f))

    @pytest.mark.parametrize("content", _HTML_BODIES)
    @pytest.mark.asyncio
    async def test_s3_html_rejected(self, content):
        with (
            patch("soliplex.agents.common.urls_file.read_text_from_s3", new_callable=AsyncMock, return_value=content),
            pytest.raises(UrlsFileFormatError, match="s3://bucket/urls.txt returned HTML"),
        ):
            await read_urls_file("s3://bucket/urls.txt")

    @pytest.mark.parametrize("content", _HTML_BODIES)
    @pytest.mark.asyncio
    async def test_literal_http_html_rejected(self, content):
        with (
            patch("soliplex.agents.common.urls_file.settings", webdav_url="https://webdav.example.com"),
            patch("soliplex.agents.common.urls_file.read_text_from_url", new_callable=AsyncMock, return_value=content),
            pytest.raises(UrlsFileFormatError, match="returned HTML"),
        ):
            await read_urls_file("http://manifest_server:8001/urls.txt", is_valid_line=is_webdav_path)

    @pytest.mark.parametrize("content", _HTML_BODIES)
    @pytest.mark.asyncio
    async def test_webdav_host_html_rejected(self, content):
        with (
            patch("soliplex.agents.common.urls_file.read_text_from_webdav", new_callable=AsyncMock, return_value=content),
            pytest.raises(UrlsFileFormatError, match="returned HTML"),
        ):
            await read_urls_file("https://dav.example.com/urls.txt", webdav_url="https://dav.example.com")

    @pytest.mark.asyncio
    async def test_local_non_utf8_rejected(self, tmp_path):
        f = tmp_path / "urls.bin"
        f.write_bytes(b"/a.pdf\n\xff\xfe\x00junk\n")
        with pytest.raises(UrlsFileFormatError, match=r"urls\.bin is not UTF-8 text"):
            await read_urls_file(str(f))

    @pytest.mark.asyncio
    async def test_remote_non_utf8_rejected(self):
        err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with (
            patch("soliplex.agents.common.urls_file.read_text_from_s3", new_callable=AsyncMock, side_effect=err),
            pytest.raises(UrlsFileFormatError, match="s3://bucket/urls.txt is not UTF-8 text") as exc_info,
        ):
            await read_urls_file("s3://bucket/urls.txt")
        assert exc_info.value.__cause__ is err

    @pytest.mark.asyncio
    async def test_validator_applied(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("# list\n/a.pdf\nhttps://x/b.pdf\n", encoding="utf-8")
        assert await read_urls_file(str(f), is_valid_line=is_webdav_path) == ["/a.pdf"]
