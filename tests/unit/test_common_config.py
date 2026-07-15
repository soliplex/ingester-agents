"""Tests for soliplex.agents.common.config module."""

import json

import pytest

from soliplex.agents import ValidationError
from soliplex.agents.common.config import check_config
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

    @pytest.mark.asyncio
    async def test_read_config_invalid_json(self, tmp_path):
        """Test read_config with malformed JSON raises ValidationError."""
        config_file = tmp_path / "inventory.json"
        config_file.write_text("not valid json {{{{")

        with pytest.raises(ValidationError):
            await read_config(str(config_file))

    @pytest.mark.asyncio
    async def test_read_config_missing_metadata_size(self, tmp_path):
        """Test read_config handles entries missing metadata.size gracefully."""
        config_data = [
            {"path": "a.pdf", "metadata": {"size": 200}},
            {"path": "b.pdf", "metadata": {}},
            {"path": "c.pdf"},
        ]
        config_file = tmp_path / "inventory.json"
        config_file.write_text(json.dumps(config_data))

        result = await read_config(str(config_file))

        assert len(result) == 3
        # Entries without size should sort as 0
        assert result[0]["path"] in ("b.pdf", "c.pdf")
        assert result[-1]["path"] == "a.pdf"
