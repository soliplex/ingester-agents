"""Tests for built-in manifest post-process callbacks — 100% branch coverage."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents.config import settings
from soliplex.agents.manifest import post_processors


@pytest.fixture
def lancedb_env(monkeypatch):
    """Point the per-source DB resolver at a known base dir."""
    monkeypatch.setattr(settings, "lancedb_dir", "/data/lance", raising=False)


# --- _load_app_config -----------------------------------------------------


def test_load_app_config_from_path():
    with (
        patch.object(post_processors, "load_yaml_config", return_value={"k": "v"}) as load_yaml,
        patch.object(post_processors.AppConfig, "model_validate", return_value="CFG") as validate,
    ):
        result = post_processors._load_app_config("cfg.yaml")

    load_yaml.assert_called_once_with("cfg.yaml")
    validate.assert_called_once_with({"k": "v"})
    assert result == "CFG"


def test_load_app_config_from_discovery():
    with patch.object(post_processors, "get_config", return_value="DISCOVERED") as get_cfg:
        result = post_processors._load_app_config(None)

    get_cfg.assert_called_once_with()
    assert result == "DISCOVERED"


# --- vacuum ---------------------------------------------------------------


def _fake_config(retention=999):
    return SimpleNamespace(storage=SimpleNamespace(vacuum_retention_seconds=retention))


@pytest.mark.asyncio
async def test_vacuum_resolves_db_forces_full_reclaim_and_runs(lancedb_env):
    cfg = _fake_config()
    app = MagicMock()
    app.vacuum = AsyncMock()
    with (
        patch.object(post_processors, "_load_app_config", return_value=cfg),
        patch.object(post_processors, "HaikuRAGApp", return_value=app) as app_cls,
    ):
        await post_processors.vacuum("my source")

    # DB resolved from the slugified source under $LANCEDB_DIR.
    kwargs = app_cls.call_args.kwargs
    assert isinstance(kwargs["db_path"], Path)
    assert kwargs["db_path"].name == "my-source.lancedb"
    assert kwargs["config"] is cfg
    # Default retention is forced to 0 (reclaim everything).
    assert cfg.storage.vacuum_retention_seconds == 0
    app.vacuum.assert_awaited_once()


@pytest.mark.asyncio
async def test_vacuum_custom_retention(lancedb_env):
    cfg = _fake_config()
    app = MagicMock()
    app.vacuum = AsyncMock()
    with (
        patch.object(post_processors, "_load_app_config", return_value=cfg),
        patch.object(post_processors, "HaikuRAGApp", return_value=app),
    ):
        await post_processors.vacuum("src", vacuum_retention_seconds=3600)

    assert cfg.storage.vacuum_retention_seconds == 3600
    app.vacuum.assert_awaited_once()


@pytest.mark.asyncio
async def test_vacuum_none_retention_leaves_config_unchanged(lancedb_env):
    cfg = _fake_config(retention=42)
    app = MagicMock()
    app.vacuum = AsyncMock()
    with (
        patch.object(post_processors, "_load_app_config", return_value=cfg),
        patch.object(post_processors, "HaikuRAGApp", return_value=app),
    ):
        await post_processors.vacuum("src", vacuum_retention_seconds=None)

    assert cfg.storage.vacuum_retention_seconds == 42  # not overridden
    app.vacuum.assert_awaited_once()


@pytest.mark.asyncio
async def test_vacuum_passes_config_path_through(lancedb_env):
    cfg = _fake_config()
    app = MagicMock()
    app.vacuum = AsyncMock()
    with (
        patch.object(post_processors, "_load_app_config", return_value=cfg) as load_cfg,
        patch.object(post_processors, "HaikuRAGApp", return_value=app),
    ):
        await post_processors.vacuum("src", config="/etc/haiku/haiku.rag.yaml")

    load_cfg.assert_called_once_with("/etc/haiku/haiku.rag.yaml")
