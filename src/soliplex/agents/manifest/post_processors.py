"""Built-in manifest post-process callbacks.

A post-process callback is referenced from a manifest's ``config.post_process``
list by dotted path and invoked as ``method(source, **kwargs)`` after the
``haiku-ingester`` load for that source completes (see
``docs/post-process-plan.md``). This module holds the callbacks that ship with
ingester-agents.
"""

import logging
from pathlib import Path

from haiku.rag.app import HaikuRAGApp
from haiku.rag.config import AppConfig
from haiku.rag.config import get_config
from haiku.rag.config import load_yaml_config

from soliplex.agents.manifest.haiku_loader import resolve_db_path

logger = logging.getLogger(__name__)


def _load_app_config(config: str | None) -> AppConfig:
    """Load an ``AppConfig`` from a path, or haiku's own config discovery."""
    if config:
        return AppConfig.model_validate(load_yaml_config(config))
    return get_config()


async def vacuum(
    source: str,
    *,
    config: str | None = None,
    vacuum_retention_seconds: int | None = 0,
) -> None:
    """Vacuum the per-source LanceDB (optimize + clean up table history).

    A post-process callback: resolves the same ``${LANCEDB_DIR}/<slug>.lancedb``
    the load wrote (via :func:`resolve_db_path`), opens a
    :class:`~haiku.rag.app.HaikuRAGApp`, and runs its ``vacuum``.

    Args:
        source: The manifest source; slugified to locate the database.
        config: Optional haiku.rag config path. When omitted, the manifest's
            resolved config is auto-injected by the post-process runner (or
            haiku's own discovery is used). It must match the DB embedder.
        vacuum_retention_seconds: Overrides the config's retention window before
            vacuuming. Defaults to ``0`` -- reclaim everything
    """
    db_path = Path(resolve_db_path(source))
    app_config = _load_app_config(config)
    if vacuum_retention_seconds is not None:
        app_config.storage.vacuum_retention_seconds = vacuum_retention_seconds
    logger.info("Vacuuming LanceDB for source '%s' -> %s", source, db_path)
    app = HaikuRAGApp(db_path=db_path, config=app_config)
    await app.vacuum()
    logger.info("Vacuum completed for source '%s'", source)
