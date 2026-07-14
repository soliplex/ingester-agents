"""Tests for manifest scheduling and hot-reload reconciliation."""

import asyncio
import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import soliplex.agents.server as server
from soliplex.agents.manifest.schedule_registry import ScheduleRegistry
from soliplex.agents.server import reconcile_manifest_schedules
from soliplex.agents.server import run_scheduled_manifest
from soliplex.agents.server.locks import get_global_manifest_semaphore
from soliplex.agents.server.locks import get_manifest_lock
from soliplex.agents.server.locks import is_manifest_running
from soliplex.agents.server.locks import reset_locks


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset locks and the schedule registry between tests."""
    reset_locks()
    server._schedule_registry = ScheduleRegistry()
    yield
    reset_locks()
    server._schedule_registry = ScheduleRegistry()


def _write_manifest(tmp_path, name, mid, schedule=None):
    """Write a minimal manifest YAML file."""
    lines = [
        f"id: {mid}",
        f"name: Manifest {mid}",
        f"source: src-{mid}",
    ]
    if schedule:
        lines.append("schedule:")
        lines.append(f'  cron: "{schedule}"')
    lines.append("components:")
    lines.append("  - type: fs")
    lines.append("    name: comp")
    lines.append("    path: /data")
    (tmp_path / name).write_text("\n".join(lines) + "\n")


class TestRunScheduledManifest:
    @pytest.mark.asyncio
    async def test_runs_and_logs(self, tmp_path, caplog):
        _write_manifest(tmp_path, "m.yml", "run-m")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                new_callable=AsyncMock,
                return_value={"results": [{"component": "comp"}]},
            ),
            caplog.at_level(logging.INFO),
        ):
            ms.haiku_load_enabled = False
            await run_scheduled_manifest("run-m", str(tmp_path / "m.yml"))
        assert "Manifest 'run-m' completed: 1 components" in caplog.text

    @pytest.mark.asyncio
    async def test_skips_when_same_manifest_running(self, caplog):
        lock = get_manifest_lock("busy")
        await lock.acquire()
        try:
            with caplog.at_level(logging.WARNING):
                await run_scheduled_manifest("busy", "/does-not-matter.yml")
        finally:
            lock.release()
        assert "previous run still in progress" in caplog.text

    @pytest.mark.asyncio
    async def test_skips_when_another_manifest_running(self, caplog):
        sem = get_global_manifest_semaphore()
        await sem.acquire()
        try:
            with caplog.at_level(logging.INFO):
                await run_scheduled_manifest("x", "/does-not-matter.yml")
        finally:
            sem.release()
        assert "another manifest is running" in caplog.text

    @pytest.mark.asyncio
    async def test_logs_error_on_failure(self, tmp_path, caplog):
        _write_manifest(tmp_path, "m.yml", "err-m")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                side_effect=RuntimeError("boom"),
            ),
            caplog.at_level(logging.ERROR),
        ):
            ms.haiku_load_enabled = False
            await run_scheduled_manifest("err-m", str(tmp_path / "m.yml"))
        assert "Error running manifest 'err-m'" in caplog.text

    @pytest.mark.asyncio
    async def test_acquires_manifest_lock(self, tmp_path):
        _write_manifest(tmp_path, "m.yml", "lock-m")
        held = False

        async def fake_run(manifest):
            nonlocal held
            held = is_manifest_running("lock-m")
            return {"results": []}

        with (
            patch("soliplex.agents.server.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                side_effect=fake_run,
            ),
        ):
            ms.haiku_load_enabled = False
            await run_scheduled_manifest("lock-m", str(tmp_path / "m.yml"))
        assert held


class TestReconcileManifestSchedules:
    @pytest.mark.asyncio
    async def test_no_manifest_dir_returns_early(self):
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.asyncio.create_task") as mock_create,
        ):
            ms.manifest_dir = None
            await reconcile_manifest_schedules()
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_directory_warns(self, tmp_path, caplog):
        fake_file = tmp_path / "not_a_dir.txt"
        fake_file.write_text("hi")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.asyncio.create_task") as mock_create,
            caplog.at_level(logging.WARNING),
        ):
            ms.manifest_dir = str(fake_file)
            await reconcile_manifest_schedules()
        assert "not a directory" in caplog.text
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_ids_logs_error_and_skips(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "dup", schedule="0 0 * * *")
        _write_manifest(tmp_path, "b.yml", "dup", schedule="0 0 * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.asyncio.create_task") as mock_create,
            caplog.at_level(logging.ERROR, logger="soliplex.agents.server"),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
        assert "Error loading manifests" in caplog.text
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_registers_scheduled_and_runs_unscheduled(self, tmp_path, caplog):
        _write_manifest(tmp_path, "sched.yml", "sched-a", schedule="*/5 * * * *")
        _write_manifest(tmp_path, "un.yml", "unsched-b")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.run_scheduled_manifest", new=MagicMock()),
            patch("soliplex.agents.server.asyncio.create_task") as mock_create,
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
        names = [c.kwargs["name"] for c in mock_create.call_args_list]
        # Scheduled manifest registered but not fired; unscheduled runs once.
        assert names == ["manifest_run_unsched-b"]
        assert "Scheduled manifest 'sched-a' cron='*/5 * * * *'" in caplog.text
        assert "Registered manifest 'unsched-b'" in caplog.text

    @pytest.mark.asyncio
    async def test_new_file_picked_up_across_reconciles(self, tmp_path):
        _write_manifest(tmp_path, "a.yml", "a", schedule="*/5 * * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.run_scheduled_manifest", new=MagicMock()),
            patch("soliplex.agents.server.asyncio.create_task") as mock_create,
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
            mock_create.reset_mock()
            _write_manifest(tmp_path, "b.yml", "b")  # new unscheduled file
            await reconcile_manifest_schedules()
            names = [c.kwargs["name"] for c in mock_create.call_args_list]
        assert names == ["manifest_run_b"]

    @pytest.mark.asyncio
    async def test_removed_file_unregistered(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "a", schedule="*/5 * * * *")
        _write_manifest(tmp_path, "b.yml", "b", schedule="*/5 * * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.run_scheduled_manifest", new=MagicMock()),
            patch("soliplex.agents.server.asyncio.create_task"),
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
            (tmp_path / "b.yml").unlink()
            caplog.clear()
            await reconcile_manifest_schedules()
        assert "Unregistered manifest 'b'" in caplog.text

    @pytest.mark.asyncio
    async def test_rescheduled_logged(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "a", schedule="0 0 * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.run_scheduled_manifest", new=MagicMock()),
            patch("soliplex.agents.server.asyncio.create_task"),
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
            _write_manifest(tmp_path, "a.yml", "a", schedule="*/5 * * * *")
            caplog.clear()
            await reconcile_manifest_schedules()
        assert "Rescheduled manifest 'a' cron='*/5 * * * *'" in caplog.text


class TestGlobalManifestLock:
    """A run must skip (not queue) while another manifest is running."""

    @pytest.mark.asyncio
    async def test_second_manifest_skips_while_first_runs(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "glob-a")
        _write_manifest(tmp_path, "b.yml", "glob-b")
        started = []
        release = asyncio.Event()

        async def fake_run(manifest):
            started.append(manifest.id)
            await release.wait()
            return {"results": []}

        with (
            patch("soliplex.agents.server.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                side_effect=fake_run,
            ),
            caplog.at_level(logging.INFO),
        ):
            ms.haiku_load_enabled = False
            first = asyncio.create_task(run_scheduled_manifest("glob-a", str(tmp_path / "a.yml")))
            # Let the first run acquire the global lock and start running.
            await asyncio.sleep(0)
            # Second run while the first is in progress must be dropped.
            await run_scheduled_manifest("glob-b", str(tmp_path / "b.yml"))
            release.set()
            await first

        assert started == ["glob-a"]
        assert "another manifest is running" in caplog.text
