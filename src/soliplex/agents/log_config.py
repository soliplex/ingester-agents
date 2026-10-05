"""A deployment's own logging configuration, from a ``dictConfig`` file.

``LOG_CONFIG_FILE`` names a YAML (or JSON) file in
:func:`logging.config.dictConfig` form. :func:`~soliplex.agents.config.configure_logging`
applies it on top of the built-in setup, so a deployment -- or a package
installed alongside this one -- can add handlers for particular loggers
without code here knowing about them: ``()`` names any factory importable in
the environment.

The file only adds. The console handler, ``LOG_LEVEL`` (unless the file sets
the root level), the SMTP handler and Logfire are all still installed, so a
client file cannot turn off console output by accident.

Two departures from a bare ``dictConfig`` call:

- ``disable_existing_loggers`` defaults to ``False``. Every module logger
  already exists by the time logging is configured, so the stdlib default
  (``True``) would silence all of them.
- A ``QueueHandler``'s listener is started here. ``dictConfig`` builds the
  listener but leaves it stopped, and never stops the previous one when it
  reconfigures; ``configure_logging()`` runs twice under ``serve``, so the
  listeners are stopped before each apply and again at exit, which also
  drains whatever is still queued.
"""

import atexit
import logging
import logging.config
import logging.handlers
from pathlib import Path

import yaml

# Listeners started by the last apply, stopped before the next one.
_listeners: list[logging.handlers.QueueListener] = []


class LogConfigError(Exception):
    """The logging config file could not be applied."""


def apply_file(path: str) -> None:
    """Apply the ``dictConfig`` file at *path* and start its queue listeners.

    On failure, nothing the file configured is left behind: the root's
    handlers are cleared and every logger the file names is reset, so the
    caller can install its defaults over a clean slate.

    Raises:
        LogConfigError: the file is missing, not a mapping, or rejected by
            ``dictConfig``.
    """
    try:
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LogConfigError(f"cannot read logging config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise LogConfigError(f"logging config {path} is not a mapping")
    config.setdefault("disable_existing_loggers", False)
    try:
        logging.config.dictConfig(config)
        _start_listeners()
    except Exception as exc:
        _undo(config)
        raise LogConfigError(f"cannot apply logging config {path}: {exc}") from exc


def _start_listeners() -> None:
    for name in logging.getHandlerNames():
        handler = logging.getHandlerByName(name)
        if isinstance(handler, logging.handlers.QueueHandler) and handler.listener is not None:
            handler.listener.start()
            _listeners.append(handler.listener)


def stop_listeners() -> None:
    """Stop the queue listeners the last apply started, draining their queues."""
    while _listeners:
        _listeners.pop().stop()


def _undo(config: dict) -> None:
    """Remove what a failed apply may have attached."""
    stop_listeners()
    logging.getLogger().handlers.clear()
    for name in config.get("loggers") or {}:
        named = logging.getLogger(name)
        named.handlers.clear()
        named.setLevel(logging.NOTSET)
        named.propagate = True
        named.disabled = False


atexit.register(stop_listeners)
