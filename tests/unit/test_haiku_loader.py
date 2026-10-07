"""Tests for the haiku-rag loader — 100% branch coverage required."""

import logging
from pathlib import Path
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
    @pytest.fixture(autouse=True)
    def _documents_present(self, monkeypatch):
        """These tests are about the subprocess, not the empty-location gate."""
        monkeypatch.setattr(haiku_loader, "_document_count", AsyncMock(return_value=1))

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
        # Exported resolved, so the subprocess's cwd can't change what it names.
        assert kwargs["env"]["DOWNLOAD_DIR"] == str(Path("downloads").resolve())
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
        mock_pp.assert_awaited_once()
        assert mock_pp.await_args.args == (manifest,)
        assert mock_pp.await_args.kwargs["ingester"].returncode == 0
        assert mock_pp.await_args.kwargs["ingester"].stdout.strip() == "ok"
        assert mock_pp.await_args.kwargs["run_result"] is None
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
        mock_pp.assert_awaited_once()
        assert mock_pp.await_args.kwargs["ingester"].returncode == 1
        assert "x" in mock_pp.await_args.kwargs["ingester"].stderr
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
        mock_pp.assert_awaited_once()
        assert mock_pp.await_args.kwargs["ingester"].returncode is None
        assert mock_pp.await_args.kwargs["ingester"].timed_out is True
        assert result["timed_out"] is True
        assert result["post_process"] == [{"method": "pkg:fn", "ok": True, "error": None}]

    @pytest.mark.asyncio
    async def test_a_failed_post_process_keeps_the_load_outcome(self, haiku_env):
        from soliplex.agents.manifest.post_process import PostProcessFailed

        steps = [{"method": "pkg:fn", "status": "error", "ok": False, "error": "RuntimeError: nope", "duration_s": 0.1}]
        with (
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_fake_proc(returncode=0),
            ),
            patch(
                "soliplex.agents.manifest.post_process.run_post_process",
                new_callable=AsyncMock,
                side_effect=PostProcessFailed("post-process pkg:fn failed: RuntimeError: nope", steps),
            ),
        ):
            result = await haiku_loader.run_load(self._pp_manifest())
        # Reported in the result, not raised, and the load's own outcome survives.
        assert result["returncode"] == 0
        assert result["timed_out"] is False
        assert result["db"]
        assert result["post_process"] == steps
        assert result["post_process_error"] == "post-process pkg:fn failed: RuntimeError: nope"

    @pytest.mark.asyncio
    async def test_other_post_process_errors_still_propagate(self, haiku_env):
        with (
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_fake_proc(returncode=0),
            ),
            patch(
                "soliplex.agents.manifest.post_process.run_post_process",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not a step"),
            ),
            pytest.raises(RuntimeError, match="not a step"),
        ):
            await haiku_loader.run_load(self._pp_manifest())


# --- empty download location gate ---


class TestEmptyLocationGate:
    _LOGGER = "soliplex.agents.manifest.haiku_loader"

    @pytest.fixture
    def source_dir(self, haiku_env, monkeypatch, tmp_path):
        """A real, local download folder for source 'src'."""
        monkeypatch.setattr(settings, "download_dir", str(tmp_path), raising=False)
        monkeypatch.setattr(settings, "download_s3_bucket", None, raising=False)
        reset_store_cache()
        yield tmp_path / "src"
        reset_store_cache()

    @staticmethod
    def _manifest(allow_empty_load=False):
        return Manifest(
            id="m",
            name="M",
            source="src",
            config=ManifestConfig(allow_empty_load=allow_empty_load, post_process=[PostProcessStep(method="pkg:fn")]),
            components=[{"type": "fs", "name": "c", "path": "/data"}],
        )

    @staticmethod
    async def _run(caplog, manifest, **kwargs):
        with (
            caplog.at_level(logging.INFO, logger=TestEmptyLocationGate._LOGGER),
            patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_fake_proc(returncode=0),
            ) as mock_exec,
            patch(
                "soliplex.agents.manifest.post_process.run_post_process", new_callable=AsyncMock, return_value=[]
            ) as mock_pp,
        ):
            result = await haiku_loader.run_load(manifest, **kwargs)
        return result, mock_exec, mock_pp

    def _skip_records(self, caplog):
        return [r for r in caplog.records if r.levelno == logging.ERROR and "Skipping haiku load" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_missing_folder_skips_the_load_and_its_post_process(self, source_dir, caplog):
        result, mock_exec, mock_pp = await self._run(caplog, self._manifest())
        mock_exec.assert_not_called()
        mock_pp.assert_not_called()
        assert result["skipped"] == {"reason": "no documents in download location"}
        assert result["returncode"] is None
        assert result["post_process"] == []
        (record,) = self._skip_records(caplog)
        message = record.getMessage()
        assert "manifest 'm' (source 'src'): no documents in download location" in message
        assert source_dir.resolve().as_uri() in message

    @pytest.mark.asyncio
    async def test_only_sidecars_counts_as_empty(self, source_dir, caplog):
        source_dir.mkdir(parents=True)
        (source_dir / f"doc.md{META_SUFFIX}").write_text("{}", encoding="utf-8")
        result, mock_exec, _ = await self._run(caplog, self._manifest())
        mock_exec.assert_not_called()
        assert result["skipped"]["reason"] == "no documents in download location"

    @pytest.mark.asyncio
    async def test_a_document_lets_the_load_run(self, source_dir, caplog):
        (source_dir / "nested").mkdir(parents=True)
        (source_dir / "nested" / "doc.md").write_text("# hi", encoding="utf-8")
        result, mock_exec, mock_pp = await self._run(caplog, self._manifest())
        mock_exec.assert_awaited_once()
        mock_pp.assert_awaited_once()
        assert "skipped" not in result
        assert self._skip_records(caplog) == []

    @pytest.mark.asyncio
    async def test_a_listing_failure_skips_the_load(self, source_dir, caplog):
        """If the agent can't list the location, haiku would most likely read it as empty."""
        with patch(
            "soliplex.agents.store.LocalDocumentStore.list",
            new_callable=AsyncMock,
            side_effect=OSError("bucket unreachable"),
        ):
            result, mock_exec, mock_pp = await self._run(caplog, self._manifest())
        mock_exec.assert_not_called()
        mock_pp.assert_not_called()
        assert result["skipped"] == {"reason": "download location could not be listed"}
        assert "Could not list documents" in caplog.text

    @pytest.mark.asyncio
    async def test_allow_empty_load_on_the_manifest_loads_anyway(self, source_dir, caplog):
        result, mock_exec, _ = await self._run(caplog, self._manifest(allow_empty_load=True))
        mock_exec.assert_awaited_once()
        assert "skipped" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("manifest_allows, argument, loads", [(False, True, True), (True, False, False)])
    async def test_the_argument_overrides_the_manifest(self, source_dir, caplog, manifest_allows, argument, loads):
        _, mock_exec, _ = await self._run(caplog, self._manifest(allow_empty_load=manifest_allows), allow_empty_load=argument)
        assert mock_exec.await_count == (1 if loads else 0)

    @pytest.mark.asyncio
    async def test_a_manifest_without_config_is_gated(self, source_dir, caplog):
        result, mock_exec, _ = await self._run(caplog, _manifest())
        mock_exec.assert_not_called()
        assert "skipped" in result

    @pytest.mark.asyncio
    async def test_load_on_error_does_not_bypass_the_gate(self, source_dir, caplog, monkeypatch):
        monkeypatch.setattr(settings, "haiku_load_on_error", True)
        result, mock_exec, _ = await self._run(caplog, self._manifest())
        mock_exec.assert_not_called()
        assert "skipped" in result

    @pytest.mark.asyncio
    async def test_a_skipped_load_gets_a_span_of_its_own(self, source_dir, caplog, spans):
        await self._run(caplog, self._manifest(), queue_wait_s=2.5)
        (span,) = spans.named("haiku load")
        assert span.attributes["haiku.load_skipped"] is True
        assert span.attributes["haiku.load_skipped.empty_location"] == 1
        assert span.attributes["haiku.source"] == "src"
        assert span.attributes["haiku.queue_wait_s"] == 2.5

    @pytest.mark.asyncio
    async def test_the_gate_applies_at_load_time_through_the_server_queue(self, source_dir, caplog):
        """A folder emptied while the load waited in the queue is caught when the load runs."""
        from soliplex.agents.server import haiku_queue

        source_dir.mkdir(parents=True)
        doc = source_dir / "doc.md"
        doc.write_text("# hi", encoding="utf-8")
        haiku_queue.start_worker()
        try:
            with patch(
                "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=_fake_proc(returncode=0),
            ) as mock_exec:
                # Queued while the folder held a document ...
                await haiku_queue.enqueue_load(self._manifest())
                # ... emptied before the worker got to it.
                doc.unlink()
                await haiku_queue._queue.join()
        finally:
            await haiku_queue.stop_worker()
        mock_exec.assert_not_called()
        assert "no documents in download location" in caplog.text
