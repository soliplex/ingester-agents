import json
import logging
import logging.handlers
from unittest.mock import patch

from soliplex.agents.config import JsonFormatter
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import _add_smtp_handler
from soliplex.agents.config import _ThrottledSMTPHandler
from soliplex.agents.config import configure_logging


class TestConfigureLogging:
    def test_configure_logging_sets_level_and_handler(self):
        """Test that configure_logging sets root level and adds a StreamHandler."""
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"
            mock_settings.log_format = "{message}"
            mock_settings.smtp_host = None
            mock_settings.smtp_from = None
            mock_settings.smtp_to = None
            configure_logging()
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) >= 1

    def test_configure_logging_json_format(self):
        """Test that log_format='json' installs JsonFormatter."""
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            mock_settings.log_format = "json"
            mock_settings.smtp_host = None
            mock_settings.smtp_from = None
            mock_settings.smtp_to = None
            configure_logging()
        root = logging.getLogger()
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert any(isinstance(h.formatter, JsonFormatter) for h in stream_handlers)

    def test_configure_logging_fallback_on_error(self):
        """Test that configure_logging falls back on exception."""
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.log_level = "INVALID_LEVEL"
            mock_settings.log_format = "{message}"
            mock_settings.smtp_host = None
            mock_settings.smtp_from = None
            mock_settings.smtp_to = None
            # setLevel with invalid string raises ValueError
            with patch.object(logging.getLogger(), "setLevel", side_effect=[ValueError("bad"), None]):
                configure_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_configure_logging_calls_add_smtp_handler(self):
        """Test that configure_logging invokes _add_smtp_handler."""
        with (
            patch("soliplex.agents.config.settings") as mock_settings,
            patch("soliplex.agents.config._add_smtp_handler") as mock_smtp,
        ):
            mock_settings.log_level = "INFO"
            mock_settings.log_format = "{message}"
            configure_logging()
            mock_smtp.assert_called_once()


def _raise_value_error():
    raise ValueError("boom")


class TestJsonFormatter:
    def test_format_basic_record(self):
        """Test that JsonFormatter produces valid JSON with expected keys."""
        fmt = JsonFormatter()
        record = logging.LogRecord("test.logger", logging.ERROR, "", 0, "test message", (), None)
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "ERROR"
        assert parsed["name"] == "test.logger"
        assert parsed["message"] == "test message"
        assert "timestamp" in parsed

    def test_format_with_exception(self):
        """Test that JsonFormatter includes exception info."""
        import sys

        fmt = JsonFormatter()
        record = logging.LogRecord("test", logging.ERROR, "", 0, "err", (), None)
        try:
            _raise_value_error()
        except ValueError:
            record.exc_info = sys.exc_info()
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]

    def test_format_with_extra_attrs(self):
        """Test that JsonFormatter includes extra attributes."""
        fmt = JsonFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        record.custom_field = "custom_value"
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["custom_field"] == "custom_value"


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
            mock_settings.smtp_cooldown = 30
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
            mock_settings.smtp_cooldown = 30
            configure_logging()
            configure_logging()
        smtp_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.SMTPHandler)]
        assert len(smtp_handlers) == 1
        # cleanup
        for h in smtp_handlers:
            root.removeHandler(h)

    def test_json_format_on_smtp_handler(self):
        """SMTP handler uses JsonFormatter when log_format is 'json'."""
        root = logging.getLogger()
        before = len(root.handlers)
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.smtp_host = "smtp.example.com"
            mock_settings.smtp_port = 25
            mock_settings.smtp_from = "a@b.com"
            mock_settings.smtp_to = ["c@d.com"]
            mock_settings.smtp_subject = "Alert"
            mock_settings.smtp_username = None
            mock_settings.smtp_password = None
            mock_settings.smtp_use_tls = False
            mock_settings.smtp_log_level = "ERROR"
            mock_settings.smtp_cooldown = 30
            mock_settings.log_format = "json"
            _add_smtp_handler()
        new_handlers = [h for h in root.handlers[before:] if isinstance(h, logging.handlers.SMTPHandler)]
        assert len(new_handlers) == 1
        assert isinstance(new_handlers[0].formatter, JsonFormatter)
        root.removeHandler(new_handlers[0])

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


class TestThrottledSMTPHandler:
    def test_first_emit_sends(self):
        """First record is always emitted."""
        handler = _ThrottledSMTPHandler(
            mailhost=("localhost", 25),
            fromaddr="a@b.com",
            toaddrs=["c@d.com"],
            subject="test",
            cooldown=30,
        )
        record = logging.LogRecord("test", logging.ERROR, "", 0, "msg", (), None)
        with patch.object(logging.handlers.SMTPHandler, "emit") as mock_emit:
            handler.emit(record)
            mock_emit.assert_called_once_with(record)

    def test_second_emit_within_cooldown_suppressed(self):
        """Second record within cooldown is dropped."""
        handler = _ThrottledSMTPHandler(
            mailhost=("localhost", 25),
            fromaddr="a@b.com",
            toaddrs=["c@d.com"],
            subject="test",
            cooldown=30,
        )
        record = logging.LogRecord("test", logging.ERROR, "", 0, "msg", (), None)
        with patch.object(logging.handlers.SMTPHandler, "emit") as mock_emit:
            handler.emit(record)
            handler.emit(record)
            assert mock_emit.call_count == 1

    def test_emit_after_cooldown_sends(self):
        """Record after cooldown expires is emitted."""
        handler = _ThrottledSMTPHandler(
            mailhost=("localhost", 25),
            fromaddr="a@b.com",
            toaddrs=["c@d.com"],
            subject="test",
            cooldown=30,
        )
        record = logging.LogRecord("test", logging.ERROR, "", 0, "msg", (), None)
        with patch.object(logging.handlers.SMTPHandler, "emit") as mock_emit:
            handler.emit(record)
            # simulate cooldown elapsed
            handler._last_emit -= 31
            handler.emit(record)
            assert mock_emit.call_count == 2


class TestManifestConfig:
    def test_delete_stale_defaults_true(self):
        config = ManifestConfig()
        assert config.delete_stale is True

    def test_delete_stale_disabled(self):
        config = ManifestConfig(delete_stale=False)
        assert config.delete_stale is False
