"""Tests for soliplex.agents.common.config module."""

import json

import pytest

from soliplex.agents import ValidationError
from soliplex.agents.common.config import MIME_OVERRIDES
from soliplex.agents.common.config import check_config
from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.common.config import read_config


class TestCheckConfig:
    """Tests for check_config function."""

    def test_check_config_valid_file(self):
        """Test check_config with valid file."""
        config = [
            {
                "path": "test.pdf",
                "metadata": {"content-type": "application/pdf"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is True
        assert "reason" not in result[0]

    def test_check_config_unsupported_zip(self):
        """Test check_config with zip file (unsupported)."""
        config = [
            {
                "path": "test.zip",
                "metadata": {"content-type": "application/zip"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "Unsupported content type"

    def test_check_config_unsupported_x_zip_compressed(self):
        """Test check_config with x-zip-compressed file (unsupported)."""
        config = [
            {
                "path": "test.zip",
                "metadata": {"content-type": "application/x-zip-compressed"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "Unsupported content type"

    def test_check_config_unsupported_octet_stream(self):
        """Test check_config with octet-stream file (unsupported)."""
        config = [
            {
                "path": "test.bin",
                "metadata": {"content-type": "application/octet-stream"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "Unsupported content type"

    def test_check_config_unsupported_rar(self):
        """Test check_config with rar file (unsupported)."""
        config = [
            {
                "path": "test.rar",
                "metadata": {"content-type": "application/x-rar-compressed"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "Unsupported content type"

    def test_check_config_unsupported_7z(self):
        """Test check_config with 7z file (unsupported)."""
        config = [
            {
                "path": "test.7z",
                "metadata": {"content-type": "application/x-7z-compressed"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "Unsupported content type"

    def test_check_config_no_metadata(self):
        """Test check_config with file missing metadata."""
        config = [
            {
                "path": "test.pdf",
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "No content type"

    def test_check_config_no_content_type(self):
        """Test check_config with metadata missing content-type."""
        config = [
            {
                "path": "test.pdf",
                "metadata": {"size": 1024},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "No content type"

    def test_check_config_extension_too_long(self):
        """Test check_config with extension longer than 4 characters."""
        config = [
            {
                "path": "test.verylongext",
                "metadata": {"content-type": "application/pdf"},
            }
        ]
        result = check_config(config)
        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["reason"] == "Unsupported file extension verylongext"

    def test_check_config_multiple_files(self):
        """Test check_config with multiple files."""
        config = [
            {
                "path": "valid.pdf",
                "metadata": {"content-type": "application/pdf"},
            },
            {
                "path": "invalid.zip",
                "metadata": {"content-type": "application/zip"},
            },
            {
                "path": "nometa.txt",
            },
        ]
        result = check_config(config)
        assert len(result) == 3
        assert result[0]["valid"] is True
        assert result[1]["valid"] is False
        assert result[1]["reason"] == "Unsupported content type"
        assert result[2]["valid"] is False
        assert result[2]["reason"] == "No content type"

    def test_check_config_with_start_end_params(self):
        """Test check_config accepts start and end parameters."""
        config = [
            {
                "path": "test.pdf",
                "metadata": {"content-type": "application/pdf"},
            }
        ]
        # These params don't affect validation logic but should be accepted
        result = check_config(config, start=0, end=10)
        assert len(result) == 1
        assert result[0]["valid"] is True


class TestDetectMimeType:
    """Tests for detect_mime_type function."""

    def test_detect_mime_type_standard_file(self):
        """Test detect_mime_type with standard file type."""
        mime_type = detect_mime_type("test.pdf")
        assert mime_type == "application/pdf"

    def test_detect_mime_type_text_file(self):
        """Test detect_mime_type with text file."""
        mime_type = detect_mime_type("readme.txt")
        assert mime_type == "text/plain"

    def test_detect_mime_type_markdown(self):
        """Test detect_mime_type with markdown file."""
        mime_type = detect_mime_type("doc.md")
        # Markdown files may not be recognized by mimetypes
        assert mime_type in ["text/markdown", "text/x-markdown", "application/octet-stream"]

    def test_detect_mime_type_docx_override(self):
        """Test detect_mime_type with .docx file using MIME override."""
        mime_type = detect_mime_type("document.docx")
        expected = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert mime_type == expected

    def test_detect_mime_type_xlsx_override(self):
        """Test detect_mime_type with .xlsx file using MIME override."""
        mime_type = detect_mime_type("spreadsheet.xlsx")
        expected = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert mime_type == expected

    def test_detect_mime_type_pptx_override(self):
        """Test detect_mime_type with .pptx file using MIME override."""
        mime_type = detect_mime_type("presentation.pptx")
        expected = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        assert mime_type == expected

    def test_detect_mime_type_unknown_extension(self):
        """Test detect_mime_type with unknown extension."""
        mime_type = detect_mime_type("file.unknownext")
        assert mime_type == "application/octet-stream"

    def test_detect_mime_type_no_extension(self):
        """Test detect_mime_type with file without extension."""
        mime_type = detect_mime_type("filename")
        assert mime_type == "application/octet-stream"

    def test_detect_mime_type_path_with_directory(self):
        """Test detect_mime_type with full path."""
        mime_type = detect_mime_type("/path/to/document.docx")
        expected = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert mime_type == expected


class TestMimeOverrides:
    """Tests for MIME_OVERRIDES constant."""

    def test_mime_overrides_exists(self):
        """Test MIME_OVERRIDES constant exists."""
        assert MIME_OVERRIDES is not None
        assert isinstance(MIME_OVERRIDES, dict)

    def test_mime_overrides_has_office_formats(self):
        """Test MIME_OVERRIDES includes Office formats."""
        assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in MIME_OVERRIDES
        assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in MIME_OVERRIDES
        assert "application/vnd.openxmlformats-officedocument.presentationml.presentation" in MIME_OVERRIDES

    def test_mime_overrides_values(self):
        """Test MIME_OVERRIDES maps to correct extensions."""
        assert MIME_OVERRIDES["application/vnd.openxmlformats-officedocument.wordprocessingml.document"] == ".docx"
        assert MIME_OVERRIDES["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"] == ".xlsx"
        assert MIME_OVERRIDES["application/vnd.openxmlformats-officedocument.presentationml.presentation"] == ".pptx"


class TestReadConfig:
    """Tests for read_config function."""

    @pytest.mark.asyncio
    async def test_read_config_list_format(self, tmp_path):
        """Test read_config with list format config file."""
        config_data = [
            {"path": "a.pdf", "metadata": {"size": 200}},
            {"path": "b.pdf", "metadata": {"size": 100}},
        ]
        config_file = tmp_path / "inventory.json"
        config_file.write_text(json.dumps(config_data))

        result = await read_config(str(config_file))

        assert len(result) == 2
        # Should be sorted by size
        assert result[0]["path"] == "b.pdf"
        assert result[1]["path"] == "a.pdf"

    @pytest.mark.asyncio
    async def test_read_config_dict_with_data_key(self, tmp_path):
        """Test read_config with dict format containing 'data' key."""
        config_data = {
            "data": [
                {"path": "large.pdf", "metadata": {"size": 500}},
                {"path": "small.pdf", "metadata": {"size": 50}},
            ]
        }
        config_file = tmp_path / "inventory.json"
        config_file.write_text(json.dumps(config_data))

        result = await read_config(str(config_file))

        assert len(result) == 2
        # Should be sorted by size
        assert result[0]["path"] == "small.pdf"
        assert result[1]["path"] == "large.pdf"

    @pytest.mark.asyncio
    async def test_read_config_invalid_format(self, tmp_path):
        """Test read_config with invalid format raises ValidationError."""
        config_data = {"invalid": "format", "no_data_key": True}
        config_file = tmp_path / "inventory.json"
        config_file.write_text(json.dumps(config_data))

        with pytest.raises(ValidationError):
            await read_config(str(config_file))
