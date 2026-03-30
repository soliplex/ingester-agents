import logging
import logging.handlers
from unittest.mock import patch

from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import _add_smtp_handler
from soliplex.agents.config import configure_logging


class TestConfigureLogging:
    def test_configure_logging_success(self):
        """Test that configure_logging calls basicConfig with settings values."""
        with (
            patch("soliplex.agents.config.logging.basicConfig") as mock_basic,
            patch("soliplex.agents.config._add_smtp_handler"),
        ):
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
            patch("soliplex.agents.config._add_smtp_handler"),
        ):
            mock_basic.side_effect = [ValueError("bad level"), None]
            configure_logging()
            assert mock_basic.call_count == 2
            fallback_kwargs = mock_basic.call_args_list[1][1]
            assert fallback_kwargs["level"] == logging.INFO
            assert fallback_kwargs["style"] == "{"
            mock_get_logger.return_value.warning.assert_called_once()

    def test_configure_logging_calls_add_smtp_handler(self):
        """Test that configure_logging invokes _add_smtp_handler."""
        with (
            patch("soliplex.agents.config.logging.basicConfig"),
            patch("soliplex.agents.config._add_smtp_handler") as mock_smtp,
        ):
            configure_logging()
            mock_smtp.assert_called_once()


class TestAddSmtpHandler:
    def test_no_smtp_settings_no_handler(self):
        """No SMTPHandler when smtp_host is not set."""
        root = logging.getLogger()
        before = len(root.handlers)
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.smtp_host = None
            mock_settings.smtp_from = "a@b.com"
            mock_settings.smtp_to = ["c@d.com"]
            _add_smtp_handler()
        smtp_handlers = [h for h in root.handlers[before:] if isinstance(h, logging.handlers.SMTPHandler)]
        assert smtp_handlers == []

    def test_partial_settings_no_handler(self):
        """No SMTPHandler when smtp_to is missing."""
        root = logging.getLogger()
        before = len(root.handlers)
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.smtp_host = "smtp.example.com"
            mock_settings.smtp_from = "a@b.com"
            mock_settings.smtp_to = None
            _add_smtp_handler()
        smtp_handlers = [h for h in root.handlers[before:] if isinstance(h, logging.handlers.SMTPHandler)]
        assert smtp_handlers == []

    def test_full_settings_adds_handler(self):
        """SMTPHandler added with correct level when fully configured."""
        root = logging.getLogger()
        before = len(root.handlers)
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.smtp_host = "smtp.example.com"
            mock_settings.smtp_port = 587
            mock_settings.smtp_from = "a@b.com"
            mock_settings.smtp_to = ["c@d.com"]
            mock_settings.smtp_subject = "Alert"
            mock_settings.smtp_username = None
            mock_settings.smtp_password = None
            mock_settings.smtp_use_tls = True
            mock_settings.smtp_log_level = "ERROR"
            mock_settings.log_format = "{message}"
            _add_smtp_handler()
        new_handlers = [h for h in root.handlers[before:] if isinstance(h, logging.handlers.SMTPHandler)]
        assert len(new_handlers) == 1
        assert new_handlers[0].level == logging.ERROR
        # cleanup
        root.removeHandler(new_handlers[0])

    def test_idempotency_with_force(self):
        """Calling configure_logging twice produces exactly one SMTPHandler."""
        root = logging.getLogger()
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            mock_settings.log_format = "{message}"
            mock_settings.smtp_host = "smtp.example.com"
            mock_settings.smtp_port = 587
            mock_settings.smtp_from = "a@b.com"
            mock_settings.smtp_to = ["c@d.com"]
            mock_settings.smtp_subject = "Alert"
            mock_settings.smtp_username = None
            mock_settings.smtp_password = None
            mock_settings.smtp_use_tls = True
            mock_settings.smtp_log_level = "ERROR"
            configure_logging()
            configure_logging()
        smtp_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.SMTPHandler)]
        assert len(smtp_handlers) == 1
        # cleanup
        for h in smtp_handlers:
            root.removeHandler(h)

    def test_invalid_smtp_config_logs_warning(self):
        """Bad SMTP config logs a warning instead of crashing."""
        with (
            patch("soliplex.agents.config.settings") as mock_settings,
            patch(
                "soliplex.agents.config.logging.handlers.SMTPHandler",
                side_effect=Exception("bad"),
            ),
            patch("soliplex.agents.config.logger") as mock_logger,
        ):
            mock_settings.smtp_host = "smtp.example.com"
            mock_settings.smtp_from = "a@b.com"
            mock_settings.smtp_to = ["c@d.com"]
            mock_settings.smtp_username = None
            mock_settings.smtp_password = None
            _add_smtp_handler()
            mock_logger.warning.assert_called_once()


class TestManifestConfig:
    def test_delete_stale_defaults_true(self):
        config = ManifestConfig()
        assert config.delete_stale is True

    def test_delete_stale_disabled(self):
        config = ManifestConfig(delete_stale=False)
        assert config.delete_stale is False
