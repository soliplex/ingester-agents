"""Tests for the haiku-rag loader — 100% branch coverage required."""

import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PostProcessStep
from soliplex.agents.config import settings
from soliplex.agents.manifest import haiku_loader
from soliplex.agents.sidecar import META_SUFFIX
from soliplex.agents.store import reset_store_cache


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


class _FakeStream:
    """Minimal stand-in for asyncio.StreamReader: one chunk per read, then EOF."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


def _fake_proc(returncode=0, stdout_lines=(b"ok\n",), stderr_lines=()):
    proc = MagicMock()
    proc.stdout = _FakeStream(stdout_lines)
    proc.stderr = _FakeStream(stderr_lines)
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class _RaisingTimeout:
    """asyncio.timeout stand-in that trips immediately on entry."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        raise TimeoutError

    async def __aexit__(self, *args):
        return False


class TestRunLoad:
    @pytest.mark.asyncio
    async def test_success_logs_output_in_parts_and_returns(self, haiku_env, caplog):
        proc = _fake_proc(returncode=0, stdout_lines=[b"step 1\n", b"done\n"])
        # Root level: the parts are logged by haiku_process, not haiku_loader.
        with caplog.at_level(logging.INFO):
            with patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec:
                result = await haiku_loader.run_load(_manifest("composite source"))

        assert result["returncode"] == 0
        assert result["timed_out"] is False
        assert result["stdout"] == "step 1\ndone"
        assert result["db"].replace("\\", "/").endswith("composite-source.lancedb")
        # Output is logged in one part per stream, not one record per line.
        assert "haiku load composite source stdout output part 1:\nstep 1\ndone" in caplog.text
        assert "haiku load for source 'composite source' completed" in caplog.text

        kwargs = mock_exec.call_args.kwargs
        # SOURCE matches the sanitized download-folder name (spaces preserved).
        assert kwargs["env"]["SOURCE"] == "composite source"
        assert kwargs["env"]["DOWNLOAD_DIR"] == "downloads"
        assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
        assert kwargs["cwd"] is None

    @pytest.mark.asyncio
    async def test_logfire_token_passed_to_env(self, haiku_env, monkeypatch):
        monkeypatch.setattr(settings, "logfire_token", SecretStr("lf-secret"), raising=False)
        proc = _fake_proc(returncode=0)
        with patch(
            "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec:
            await haiku_loader.run_load(_manifest())
        assert mock_exec.call_args.kwargs["env"]["LOGFIRE_TOKEN"] == "lf-secret"

    @pytest.mark.asyncio
    async def test_queue_wait_is_recorded_on_the_load_span(self, haiku_env, spans):
        with patch(
            "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=[_fake_proc(returncode=0), _fake_proc(returncode=0)],
        ):
            await haiku_loader.run_load(_manifest(), queue_wait_s=4.5)
            await haiku_loader.run_load(_manifest())  # not queued, as from the CLI

        queued, direct = spans.named("haiku load")
        assert queued.attributes["haiku.queue_wait_s"] == 4.5
        assert queued.attributes["manifest.id"] == "m"
        assert "haiku.queue_wait_s" not in direct.attributes

    @pytest.mark.asyncio
    async def test_nonzero_returncode_quotes_stderr(self, haiku_env, caplog):
        proc = _fake_proc(returncode=2, stdout_lines=[], stderr_lines=[b"boom\n"])
        with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.haiku_loader"):
            with patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                result = await haiku_loader.run_load(_manifest())
        assert result["returncode"] == 2
        assert result["stderr"] == "boom"
        assert "failed (rc=2); last stderr:\nboom" in caplog.text

    @pytest.mark.asyncio
    async def test_signal_kill_reports_oom_hint(self, haiku_env, caplog):
        # rc=-9 => killed by SIGKILL, typically the cgroup OOM killer.
        proc = _fake_proc(returncode=-9)
        with caplog.at_level(logging.ERROR, logger="soliplex.agents.manifest.haiku_loader"):
            with patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                result = await haiku_loader.run_load(_manifest())
        assert result["returncode"] == -9
        assert result["timed_out"] is False
        assert "was killed by" in caplog.text
        assert "rc=-9" in caplog.text
        assert "memory limit" in caplog.text

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, haiku_env):
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.timeout",
                _RaisingTimeout,
            ),
        ):
            result = await haiku_loader.run_load(_manifest())
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        assert result["timed_out"] is True
        assert result["returncode"] is None

    @staticmethod
    def _pp_manifest():
        return Manifest(
            id="m",
            name="M",
            source="src",
            config=ManifestConfig(post_process=[PostProcessStep(method="pkg:fn")]),
            components=[{"type": "fs", "name": "c", "path": "/data"}],
        )

    @pytest.mark.asyncio
    async def test_post_process_runs_on_success(self, haiku_env):
        manifest = self._pp_manifest()
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch(
                "soliplex.agents.manifest.post_process.run_post_process",
                new_callable=AsyncMock,
                return_value=[{"method": "pkg:fn", "ok": True, "error": None}],
            ) as mock_pp,
        ):
            result = await haiku_loader.run_load(manifest)
        mock_pp.assert_awaited_once_with(manifest, ingester_exit_code=0)
        assert result["post_process"] == [{"method": "pkg:fn", "ok": True, "error": None}]

    @pytest.mark.asyncio
    async def test_post_process_runs_on_nonzero_with_exit_code(self, haiku_env):
        # Fires even when the load failed; the exit code is forwarded.
        manifest = self._pp_manifest()
        proc = _fake_proc(returncode=1, stdout_lines=[], stderr_lines=[b"x\n"])
        with (
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch(
                "soliplex.agents.manifest.post_process.run_post_process",
                new_callable=AsyncMock,
                return_value=[{"method": "pkg:fn", "ok": True, "error": None}],
            ) as mock_pp,
        ):
            result = await haiku_loader.run_load(manifest)
        mock_pp.assert_awaited_once_with(manifest, ingester_exit_code=1)
        assert result["post_process"] == [{"method": "pkg:fn", "ok": True, "error": None}]

    @pytest.mark.asyncio
    async def test_post_process_runs_on_timeout_with_none_exit_code(self, haiku_env):
        manifest = self._pp_manifest()
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.timeout",
                _RaisingTimeout,
            ),
            patch(
                "soliplex.agents.manifest.post_process.run_post_process",
                new_callable=AsyncMock,
                return_value=[{"method": "pkg:fn", "ok": True, "error": None}],
            ) as mock_pp,
        ):
            result = await haiku_loader.run_load(manifest)
        mock_pp.assert_awaited_once_with(manifest, ingester_exit_code=None)
        assert result["timed_out"] is True
        assert result["post_process"] == [{"method": "pkg:fn", "ok": True, "error": None}]


# --- empty download folder check ---


class TestLogIfNoDocuments:
    _LOGGER = "soliplex.agents.manifest.haiku_loader"
    _EMPTY = "finished with no documents"

    @pytest.fixture
    def source_dir(self, haiku_env, monkeypatch, tmp_path):
        """A real, local download folder for source 'src'."""
        monkeypatch.setattr(settings, "download_dir", str(tmp_path), raising=False)
        monkeypatch.setattr(settings, "download_s3_bucket", None, raising=False)
        reset_store_cache()
        yield tmp_path / "src"
        reset_store_cache()

    @staticmethod
    async def _run(caplog):
        with (
            caplog.at_level(logging.INFO, logger=TestLogIfNoDocuments._LOGGER),
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_fake_proc(returncode=0),
            ) as mock_exec,
        ):
            await haiku_loader.run_load(_manifest())
        return mock_exec

    @pytest.mark.asyncio
    async def test_missing_folder_logs_error_and_still_loads(self, source_dir, caplog):
        mock_exec = await self._run(caplog)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR and self._EMPTY in r.getMessage()]
        assert len(errors) == 1
        assert "Manifest 'm'" in errors[0].getMessage()
        assert "source 'src'" in errors[0].getMessage()
        mock_exec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_only_sidecars_counts_as_empty(self, source_dir, caplog):
        source_dir.mkdir(parents=True)
        (source_dir / f"doc.md{META_SUFFIX}").write_text("{}", encoding="utf-8")
        await self._run(caplog)
        assert self._EMPTY in caplog.text

    @pytest.mark.asyncio
    async def test_document_present_logs_nothing(self, source_dir, caplog):
        (source_dir / "nested").mkdir(parents=True)
        (source_dir / "nested" / "doc.md").write_text("# hi", encoding="utf-8")
        await self._run(caplog)
        assert self._EMPTY not in caplog.text

    @pytest.mark.asyncio
    async def test_listing_failure_is_logged_and_load_continues(self, source_dir, caplog):
        with patch(
            "soliplex.agents.store.LocalDocumentStore.list",
            new_callable=AsyncMock,
            side_effect=OSError("bucket unreachable"),
        ):
            mock_exec = await self._run(caplog)
        assert "Could not list documents" in caplog.text
        assert self._EMPTY not in caplog.text
        mock_exec.assert_awaited_once()
