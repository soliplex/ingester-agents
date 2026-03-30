"""Tests for manifest scheduling in server startup."""

import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents.server import _run_manifest_at_startup
from soliplex.agents.server import setup_manifest_schedules
from soliplex.agents.server.locks import get_manifest_lock
from soliplex.agents.server.locks import is_manifest_running
from soliplex.agents.server.locks import reset_locks


@pytest.fixture(autouse=True)
def _clean_locks():
    """Reset the lock registry between tests."""
    reset_locks()
    yield
    reset_locks()


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


class TestRunManifestAtStartup:
    @pytest.mark.asyncio
    async def test_runs_manifest_and_logs(self, tmp_path, caplog):
        _write_manifest(tmp_path, "m.yml", "startup-m")
        with (
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                new_callable=AsyncMock,
                return_value={
                    "results": [{"component": "comp"}],
                },
            ),
            caplog.at_level(logging.INFO),
        ):
            await _run_manifest_at_startup(str(tmp_path / "m.yml"))
        assert "Startup manifest 'startup-m' completed" in caplog.text
        assert "1 components" in caplog.text

    @pytest.mark.asyncio
    async def test_logs_error_on_failure(self, caplog):
        with caplog.at_level(logging.ERROR):
            await _run_manifest_at_startup("/nonexistent.yml")
        assert "Error running startup manifest" in caplog.text

    @pytest.mark.asyncio
    async def test_acquires_manifest_lock(self, tmp_path):
        """Startup task holds the manifest lock while running."""
        _write_manifest(tmp_path, "m.yml", "lock-test")
        lock_was_held = False

        async def fake_run(manifest):
            nonlocal lock_was_held
            lock_was_held = is_manifest_running("lock-test")
            return {"results": []}

        with patch(
            "soliplex.agents.manifest.runner.run_manifest",
            side_effect=fake_run,
        ):
            await _run_manifest_at_startup(str(tmp_path / "m.yml"))

        assert lock_was_held


class TestSetupManifestSchedules:
    def test_no_manifest_dir_returns_early(self):
        crons = MagicMock()
        with patch("soliplex.agents.server.settings") as mock_settings:
            mock_settings.manifest_dir = None
            setup_manifest_schedules(crons)
        crons.cron.assert_not_called()

    def test_non_directory_warns(self, tmp_path, caplog):
        crons = MagicMock()
        fake_file = tmp_path / "not_a_dir.txt"
        fake_file.write_text("hi")
        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            caplog.at_level(logging.WARNING),
        ):
            mock_settings.manifest_dir = str(fake_file)
            setup_manifest_schedules(crons)
        assert "not a directory" in caplog.text
        crons.cron.assert_not_called()

    def test_duplicate_ids_logs_error(self, tmp_path, caplog):
        for name in ["a.yml", "b.yml"]:
            _write_manifest(tmp_path, name, "same-id", schedule="0 0 * * *")
        crons = MagicMock()
        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            caplog.at_level(logging.ERROR, logger="soliplex.agents.server"),
        ):
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)
        assert "Error loading manifests" in caplog.text
        crons.cron.assert_not_called()

    def test_scheduled_manifest_registers_cron(self, tmp_path, caplog):
        _write_manifest(tmp_path, "cron.yml", "cron-m", schedule="0 0 * * *")
        crons = MagicMock()
        decorator = MagicMock(side_effect=lambda fn: fn)
        crons.cron.return_value = decorator
        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            caplog.at_level(logging.INFO),
        ):
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)
        crons.cron.assert_called_once_with("0 0 * * *", name="manifest_cron-m")
        assert "Scheduled manifest 'cron-m'" in caplog.text

    def test_unscheduled_manifest_creates_startup_task(self, tmp_path, caplog):
        _write_manifest(tmp_path, "no-sched.yml", "nosched-m")
        crons = MagicMock()

        mock_task = MagicMock()
        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            patch(
                "soliplex.agents.server.asyncio.create_task",
                return_value=mock_task,
            ) as mock_create,
            caplog.at_level(logging.INFO),
        ):
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["name"] == ("startup_manifest_nosched-m")
        crons.cron.assert_not_called()
        assert "Queued startup run for manifest 'nosched-m'" in (caplog.text)

    def test_mixed_scheduled_and_unscheduled(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "sched-a", schedule="*/5 * * * *")
        _write_manifest(tmp_path, "b.yml", "nosched-b")
        crons = MagicMock()
        decorator = MagicMock(side_effect=lambda fn: fn)
        crons.cron.return_value = decorator

        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            patch(
                "soliplex.agents.server.asyncio.create_task",
            ) as mock_create,
            caplog.at_level(logging.INFO),
        ):
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)
        crons.cron.assert_called_once_with("*/5 * * * *", name="manifest_sched-a")
        mock_create.assert_called_once()

    def test_empty_directory(self, tmp_path):
        crons = MagicMock()
        with patch("soliplex.agents.server.settings") as mock_settings:
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)
        crons.cron.assert_not_called()


class TestCronHandlerSkipsWhenBusy:
    """Cron handler must skip execution when the manifest is already running."""

    @pytest.mark.asyncio
    async def test_cron_handler_skips_if_locked(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "busy-cron", schedule="* * * * *")
        crons = MagicMock()
        registered_handler = None

        def capture_handler(fn):
            nonlocal registered_handler
            registered_handler = fn
            return fn

        crons.cron.return_value = capture_handler

        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            caplog.at_level(logging.WARNING),
        ):
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)

        assert registered_handler is not None

        # Simulate the manifest already running by holding its lock
        lock = get_manifest_lock("busy-cron")
        await lock.acquire()
        try:
            await registered_handler()
        finally:
            lock.release()

        assert "previous run still in progress" in caplog.text

    @pytest.mark.asyncio
    async def test_cron_handler_runs_when_free(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "free-cron", schedule="* * * * *")
        crons = MagicMock()
        registered_handler = None

        def capture_handler(fn):
            nonlocal registered_handler
            registered_handler = fn
            return fn

        crons.cron.return_value = capture_handler

        with (
            patch("soliplex.agents.server.settings") as mock_settings,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                new_callable=AsyncMock,
                return_value={"results": [{"component": "comp"}]},
            ),
            caplog.at_level(logging.INFO),
        ):
            mock_settings.manifest_dir = str(tmp_path)
            setup_manifest_schedules(crons)
            await registered_handler()

        assert "completed" in caplog.text
        assert "previous run still in progress" not in caplog.text
