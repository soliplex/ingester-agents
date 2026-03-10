import logging
from unittest.mock import patch

from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import configure_logging


class TestConfigureLogging:
    def test_configure_logging_success(self):
        """Test that configure_logging calls basicConfig with settings values."""
        with patch("soliplex.agents.config.logging.basicConfig") as mock_basic:
            configure_logging()
            mock_basic.assert_called_once()
            call_kwargs = mock_basic.call_args[1]
            assert call_kwargs["datefmt"] == "%Y-%m-%dT%H:%M:%S"
            assert call_kwargs["style"] == "{"

    def test_configure_logging_fallback_on_error(self):
        """Test that configure_logging falls back on exception."""
        with (
            patch("soliplex.agents.config.logging.basicConfig") as mock_basic,
            patch("soliplex.agents.config.logging.getLogger") as mock_get_logger,
        ):
            mock_basic.side_effect = [ValueError("bad level"), None]
            configure_logging()
            assert mock_basic.call_count == 2
            fallback_kwargs = mock_basic.call_args_list[1][1]
            assert fallback_kwargs["level"] == logging.INFO
            assert fallback_kwargs["style"] == "{"
            mock_get_logger.return_value.warning.assert_called_once()


class TestManifestConfig:
    def test_delete_stale_defaults_false(self):
        config = ManifestConfig()
        assert config.delete_stale is False

    def test_delete_stale_enabled(self):
        config = ManifestConfig(delete_stale=True)
        assert config.delete_stale is True
