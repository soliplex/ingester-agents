"""Tests for soliplex.agents.common.config module."""

from soliplex.agents.common.config import check_config


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
