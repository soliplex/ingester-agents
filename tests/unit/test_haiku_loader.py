"""Tests for the haiku-rag loader — 100% branch coverage required."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import settings
from soliplex.agents.manifest import haiku_loader


def _manifest(source="src", haiku_config=None):
    config = ManifestConfig(haiku_config=haiku_config) if haiku_config is not None else None
    return Manifest(
        id="m",
        name="M",
        source=source,
        config=config,
        components=[{"type": "fs", "name": "c", "path": "/data"}],
    )


@pytest.fixture
def haiku_env(monkeypatch):
    """Set haiku-related settings to known values for the test."""
    monkeypatch.setattr(settings, "haiku_path", "/opt/haiku", raising=False)
    monkeypatch.setattr(settings, "lancedb_dir", "/data/lance", raising=False)
    monkeypatch.setattr(settings, "haiku_default_config", "haiku.rag.default.yaml", raising=False)
    monkeypatch.setattr(
        settings,
        "haiku_load_command",
        "haiku-ingester --config={haiku_cfg} run-batch --db={db}",
        raising=False,
    )
    monkeypatch.setattr(settings, "haiku_load_timeout", 1800, raising=False)
    monkeypatch.setattr(settings, "haiku_load_cwd", None, raising=False)
    monkeypatch.setattr(settings, "download_dir", "downloads", raising=False)


# --- slugify_source ---


class TestSlugifySource:
    def test_spaces_become_hyphens(self):
        assert haiku_loader.slugify_source("composite source") == "composite-source"

    def test_collapses_and_trims(self):
        assert haiku_loader.slugify_source("  a   b  ") == "a-b"

    def test_already_clean(self):
        assert haiku_loader.slugify_source("plain") == "plain"

    def test_empty_falls_back(self):
        assert haiku_loader.slugify_source("   ") == "source"


# --- resolve_haiku_cfg ---


class TestResolveHaikuCfg:
    def test_default_under_haiku_path(self, haiku_env):
        cfg = haiku_loader.resolve_haiku_cfg(_manifest())
        assert cfg.replace("\\", "/") == "/opt/haiku/haiku.rag.default.yaml"

    def test_manifest_override_relative(self, haiku_env):
        cfg = haiku_loader.resolve_haiku_cfg(_manifest(haiku_config="custom.yaml"))
        assert cfg.replace("\\", "/") == "/opt/haiku/custom.yaml"

    def test_absolute_override_used_as_is(self, haiku_env, tmp_path):
        abs_cfg = tmp_path / "x.yaml"  # absolute on any platform
        cfg = haiku_loader.resolve_haiku_cfg(_manifest(haiku_config=str(abs_cfg)))
        assert cfg == str(abs_cfg)

    def test_relative_without_haiku_path_raises(self, haiku_env, monkeypatch):
        monkeypatch.setattr(settings, "haiku_path", None, raising=False)
        with pytest.raises(ValueError, match="HAIKU_PATH"):
            haiku_loader.resolve_haiku_cfg(_manifest())


# --- resolve_db_path ---


class TestResolveDbPath:
    def test_slugified_filename(self, haiku_env):
        db = haiku_loader.resolve_db_path("composite source")
        assert db.replace("\\", "/") == "/data/lance/composite-source.lancedb"

    def test_unset_lancedb_dir_raises(self, haiku_env, monkeypatch):
        monkeypatch.setattr(settings, "lancedb_dir", None, raising=False)
        with pytest.raises(ValueError, match="LANCEDB_DIR"):
            haiku_loader.resolve_db_path("src")


# --- build_load_argv ---


class TestBuildLoadArgv:
    def test_default_template(self, haiku_env):
        argv = haiku_loader.build_load_argv("/cfg.yaml", "/db.lancedb", "src")
        assert argv == [
            "haiku-ingester",
            "--config=/cfg.yaml",
            "run-batch",
            "--db=/db.lancedb",
        ]

    def test_custom_template_all_placeholders(self, haiku_env, monkeypatch):
        monkeypatch.setattr(
            settings,
            "haiku_load_command",
            "load {source} {lancedb_dir} {haiku_path}",
            raising=False,
        )
        argv = haiku_loader.build_load_argv("/cfg", "/db", "composite source")
        assert argv == ["load", "composite-source", "/data/lance", "/opt/haiku"]


# --- run_load ---


def _fake_proc(returncode=0, stdout=b"ok", stderr=b""):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestRunLoad:
    @pytest.mark.asyncio
    async def test_success_sets_env_and_returns(self, haiku_env):
        proc = _fake_proc(returncode=0, stdout=b"done", stderr=b"")
        with patch(
            "soliplex.agents.manifest.haiku_loader.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec:
            result = await haiku_loader.run_load(_manifest("composite source"))

        assert result["returncode"] == 0
        assert result["timed_out"] is False
        assert result["stdout"] == "done"
        assert result["db"].replace("\\", "/").endswith("composite-source.lancedb")

        kwargs = mock_exec.call_args.kwargs
        # SOURCE matches the sanitized download-folder name (spaces preserved).
        assert kwargs["env"]["SOURCE"] == "composite source"
        assert kwargs["env"]["DOWNLOAD_DIR"] == "downloads"
        assert kwargs["cwd"] is None

    @pytest.mark.asyncio
    async def test_nonzero_returncode_logged(self, haiku_env, caplog):
        proc = _fake_proc(returncode=2, stdout=b"", stderr=b"boom")
        with patch(
            "soliplex.agents.manifest.haiku_loader.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await haiku_loader.run_load(_manifest())
        assert result["returncode"] == 2
        assert "failed" in caplog.text

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, haiku_env):
        proc = MagicMock()
        proc.communicate = MagicMock()  # never awaited; wait_for is mocked
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with (
            patch(
                "soliplex.agents.manifest.haiku_loader.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch(
                "soliplex.agents.manifest.haiku_loader.asyncio.wait_for",
                new_callable=AsyncMock,
                side_effect=TimeoutError,
            ),
        ):
            result = await haiku_loader.run_load(_manifest())
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        assert result["timed_out"] is True
        assert result["returncode"] is None
