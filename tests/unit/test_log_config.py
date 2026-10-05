"""LOG_CONFIG_FILE: a dictConfig file applied by configure_logging()."""

import json
import logging
import logging.handlers
from unittest.mock import patch

import pytest

from soliplex.agents import log_config
from soliplex.agents.config import Settings
from soliplex.agents.config import configure_logging

# Records land in a stdlib BufferingHandler, found again by its config name.
MEMORY = {"class": "logging.handlers.BufferingHandler", "capacity": 1000}


@pytest.fixture(autouse=True)
def _restore_logging():
    """Put back what configure_logging() and the config files changed."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    log_config.stop_listeners()
    root.handlers[:] = handlers
    root.setLevel(level)
    for name in [n for n in logging.root.manager.loggerDict if n.startswith("logcfg_test")]:
        named = logging.getLogger(name)
        named.handlers.clear()
        named.setLevel(logging.NOTSET)
        named.propagate = True
        named.disabled = False


@pytest.fixture
def configure(tmp_path):
    """Run configure_logging() with *config* as LOG_CONFIG_FILE."""

    def _configure(config: dict | str | None, **settings):
        path = None
        if config is not None:
            path = tmp_path / "logging.yaml"
            path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
        values = {"log_level": "INFO", "log_format": "{message}", "smtp_host": None, "log_config_file": path and str(path)}
        with patch("soliplex.agents.config.settings", Settings(**(values | settings))):
            configure_logging()

    return _configure


def _console_only(root: logging.Logger) -> bool:
    return [type(handler) for handler in root.handlers] == [logging.StreamHandler]


def _config(**sections) -> dict:
    return {"version": 1, **sections}


class TestSettings:
    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_file_is_unset(self, value):
        assert Settings(log_config_file=value).log_config_file is None

    def test_file_kept(self):
        assert Settings(log_config_file=" /etc/logging.yaml ").log_config_file == "/etc/logging.yaml"

    def test_defaults(self):
        settings = Settings()
        assert settings.log_config_file is None
        assert settings.log_config_strict is False


class TestApply:
    def test_no_file_is_console_only(self, configure):
        configure(None)
        assert _console_only(logging.getLogger())

    def test_handler_on_named_logger(self, configure):
        existing = logging.getLogger("logcfg_test.existing")
        configure(
            _config(
                handlers={"mem": MEMORY | {"level": "ERROR"}},
                loggers={"logcfg_test.a": {"handlers": ["mem"]}},
            )
        )
        named = logging.getLogger("logcfg_test.a")
        named.info("not sent")
        named.error("sent")

        assert [r.getMessage() for r in logging.getHandlerByName("mem").buffer] == ["sent"]
        # Loggers created before the file was applied keep working.
        assert existing.disabled is False
        # The file only adds: the root keeps its console handler.
        assert _console_only(logging.getLogger())

    def test_disable_existing_loggers_respected_when_given(self, configure):
        existing = logging.getLogger("logcfg_test.existing")
        configure(_config(disable_existing_loggers=True))
        assert existing.disabled is True

    def test_json_file(self, configure):
        configure(json.dumps(_config(loggers={"logcfg_test.a": {"level": "ERROR"}})))
        assert logging.getLogger("logcfg_test.a").level == logging.ERROR

    def test_log_level_applies_without_root_level(self, configure):
        configure(_config(), log_level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_root_level_from_file_wins(self, configure):
        configure(_config(root={"level": "WARNING"}), log_level="DEBUG")
        assert logging.getLogger().level == logging.WARNING

    def test_root_handlers_keep_console(self, configure):
        configure(_config(handlers={"mem": MEMORY}, root={"handlers": ["mem"]}))
        root = logging.getLogger()
        assert root.handlers == [logging.getHandlerByName("mem"), root.handlers[1]]
        assert type(root.handlers[1]) is logging.StreamHandler


class TestQueueListeners:
    QUEUED = _config(
        handlers={
            "mem": MEMORY,
            "queued": {
                "class": "logging.handlers.QueueHandler",
                "listener": "logging.handlers.QueueListener",
                "handlers": ["mem"],
                "level": "ERROR",
            },
        },
        loggers={"logcfg_test.q": {"handlers": ["queued"]}},
    )

    def test_listener_started(self, configure):
        configure(self.QUEUED)
        logging.getLogger("logcfg_test.q").error("queued")
        log_config.stop_listeners()  # drains the queue
        assert [r.getMessage() for r in logging.getHandlerByName("mem").buffer] == ["queued"]

    def test_reconfigure_replaces_listener(self, configure):
        configure(self.QUEUED)
        (first,) = log_config._listeners
        # Reconfiguring flushes (so empties) the old BufferingHandler: record
        # what reaches it instead.
        delivered = []
        logging.getHandlerByName("mem").emit = delivered.append
        logging.getLogger("logcfg_test.q").error("before")

        configure(self.QUEUED)
        (second,) = log_config._listeners

        assert second is not first
        assert first._thread is None  # stopped, after draining
        assert [r.getMessage() for r in delivered] == ["before"]
        assert len(logging.getLogger("logcfg_test.q").handlers) == 1

    def test_stop_without_listeners(self):
        log_config.stop_listeners()
        assert log_config._listeners == []


class TestFailure:
    @pytest.mark.parametrize(
        "content, reason",
        [
            (None, "cannot read"),
            ("key: [unclosed", "cannot read"),
            ("- not\n- a mapping\n", "not a mapping"),
            (_config(handlers={"bad": {"class": "no.such.Handler"}}), "cannot apply"),
        ],
    )
    def test_falls_back_with_warning(self, tmp_path, content, reason):
        path = tmp_path / "logging.yaml"
        if content is not None:
            path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        settings = Settings(log_format="{message}", smtp_host=None, log_config_file=str(path))
        with (
            patch("soliplex.agents.config.settings", settings),
            patch("soliplex.agents.config.logger.warning") as warning,
        ):
            configure_logging()

        assert _console_only(logging.getLogger())
        (message, error), _ = warning.call_args
        assert "built-in" in message
        assert reason in str(error)
        assert str(path) in str(error)

    def test_strict_raises(self, configure):
        with pytest.raises(log_config.LogConfigError, match="not a mapping"):
            configure("just a string", log_config_strict=True)
        # The console handler is in place to report it.
        assert _console_only(logging.getLogger())

    def test_partial_apply_undone(self, configure):
        configure(
            _config(
                handlers={"mem": MEMORY},
                root={"handlers": ["mem"]},
                loggers={
                    "logcfg_test.b": {"handlers": ["mem"], "level": "ERROR", "propagate": False},
                    "logcfg_test.c": {"handlers": ["missing"]},
                },
            )
        )
        configured = logging.getLogger("logcfg_test.b")
        assert configured.handlers == []
        assert configured.level == logging.NOTSET
        assert configured.propagate is True
        assert _console_only(logging.getLogger())

    def test_listener_failure_stops_started_ones(self, configure):
        with patch.object(logging.handlers.QueueListener, "start", side_effect=RuntimeError("no thread")):
            configure(TestQueueListeners.QUEUED)
        assert log_config._listeners == []
        assert logging.getLogger("logcfg_test.q").handlers == []
