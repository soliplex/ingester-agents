"""Tests for manifest scheduling and hot-reload reconciliation.

The reconciler's only job is to diff the manifest directory and hand due
manifests to :mod:`soliplex.agents.server.manifest_queue`; the queue's own
behaviour (serialization, coalescing, failure isolation) is covered in
``test_manifest_queue.py``.
"""

import asyncio
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
import pytest_asyncio

import soliplex.agents.server as server
from soliplex.agents.manifest.schedule_registry import ScheduleRegistry
from soliplex.agents.server import manifest_queue
from soliplex.agents.server import reconcile_manifest_schedules


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the schedule registry between tests."""
    server._schedule_registry = ScheduleRegistry()
    yield
    server._schedule_registry = ScheduleRegistry()


@pytest_asyncio.fixture(autouse=True)
async def _stopped_worker():
    """Guarantee no manifest worker leaks between tests."""
    yield
    await manifest_queue.stop_worker()


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


def _enqueued(mock_enqueue):
    """Manifest ids handed to the queue, in order."""
    return [c.args[0] for c in mock_enqueue.await_args_list]


class TestReconcileManifestSchedules:
    """The reconciler enqueues due manifests; it never runs them inline."""

    @pytest.mark.asyncio
    async def test_no_manifest_dir_returns_early(self):
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock) as mock_enq,
        ):
            ms.manifest_dir = None
            await reconcile_manifest_schedules()
        mock_enq.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_directory_warns(self, tmp_path, caplog):
        fake_file = tmp_path / "not_a_dir.txt"
        fake_file.write_text("hi")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock) as mock_enq,
            caplog.at_level(logging.WARNING),
        ):
            ms.manifest_dir = str(fake_file)
            await reconcile_manifest_schedules()
        assert "not a directory" in caplog.text
        mock_enq.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_ids_logs_error_and_skips(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "dup", schedule="0 0 * * *")
        _write_manifest(tmp_path, "b.yml", "dup", schedule="0 0 * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock) as mock_enq,
            caplog.at_level(logging.ERROR, logger="soliplex.agents.server"),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
        assert "Error loading manifests" in caplog.text
        mock_enq.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_registers_scheduled_and_runs_unscheduled(self, tmp_path, caplog):
        _write_manifest(tmp_path, "sched.yml", "sched-a", schedule="*/5 * * * *")
        _write_manifest(tmp_path, "un.yml", "unsched-b")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock) as mock_enq,
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
        # Scheduled manifest registered but not due; unscheduled runs once.
        assert _enqueued(mock_enq) == ["unsched-b"]
        assert "Scheduled manifest 'sched-a' cron='*/5 * * * *'" in caplog.text
        assert "Registered manifest 'unsched-b'" in caplog.text

    @pytest.mark.asyncio
    async def test_new_file_picked_up_across_reconciles(self, tmp_path):
        _write_manifest(tmp_path, "a.yml", "a", schedule="*/5 * * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock) as mock_enq,
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
            mock_enq.reset_mock()
            _write_manifest(tmp_path, "b.yml", "b")  # new unscheduled file
            await reconcile_manifest_schedules()
        assert _enqueued(mock_enq) == ["b"]

    @pytest.mark.asyncio
    async def test_removed_file_unregistered(self, tmp_path, caplog):
        _write_manifest(tmp_path, "a.yml", "a", schedule="*/5 * * * *")
        _write_manifest(tmp_path, "b.yml", "b", schedule="*/5 * * * *")
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock),
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
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock),
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            await reconcile_manifest_schedules()
            _write_manifest(tmp_path, "a.yml", "a", schedule="*/5 * * * *")
            caplog.clear()
            await reconcile_manifest_schedules()
        assert "Rescheduled manifest 'a' cron='*/5 * * * *'" in caplog.text


class _FrozenClock:
    """A patchable stand-in for ``datetime`` that only advances on demand."""

    def __init__(self, start):
        self.now_value = start

    def now(self, tz=None):
        return self.now_value

    def advance(self, **kwargs):
        self.now_value += timedelta(**kwargs)


class TestSchedulerExpectations:
    """End-to-end checks of the three scheduler guarantees.

    These drive the real reconciler and the real queue worker over a frozen
    clock, so they cover the wiring between them rather than either alone.
    """

    @pytest.mark.asyncio
    async def test_same_time_manifests_all_run_sequentially(self, tmp_path):
        """Every manifest due at the same time runs, one at a time, in order."""
        for name, mid in (("a.yml", "aaa"), ("b.yml", "bbb"), ("c.yml", "ccc")):
            _write_manifest(tmp_path, name, mid, schedule="*/5 * * * *")
        started = []
        concurrent = 0
        peak = 0

        async def fake_run(manifest):
            nonlocal concurrent, peak
            started.append(manifest.id)
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0)
            concurrent -= 1
            return {"results": []}

        clock = _FrozenClock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))
        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.settings") as mqs,
            patch("soliplex.agents.server.datetime", clock),
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
        ):
            ms.manifest_dir = str(tmp_path)
            mqs.haiku_load_enabled = False
            for _ in range(11):  # 10:00 -> 10:10, two due boundaries
                await reconcile_manifest_schedules()
                await asyncio.wait_for(manifest_queue._queue.join(), timeout=5)
                clock.advance(minutes=1)

        assert started == ["aaa", "bbb", "ccc", "aaa", "bbb", "ccc"]
        assert peak == 1

    @pytest.mark.asyncio
    async def test_a_manifest_due_while_another_runs_is_queued_not_skipped(self, tmp_path, caplog):
        """A manifest that comes due mid-run waits its turn instead of vanishing.

        This is the case the old lock-and-skip scheduler dropped outright:
        the queue was empty, another manifest simply happened to be running.
        """
        _write_manifest(tmp_path, "aaa.yml", "aaa", schedule="*/5 * * * *")
        started = []
        hold = asyncio.Event()

        async def fake_run(manifest):
            started.append(manifest.id)
            if manifest.id == "aaa":
                # Stall 'aaa' so 'zzz' arrives while a run is in flight.
                await hold.wait()
            return {"results": []}

        clock = _FrozenClock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))
        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.settings") as mqs,
            patch("soliplex.agents.server.datetime", clock),
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            mqs.haiku_load_enabled = False
            # 10:00 -> 10:05: 'aaa' comes due and stalls.
            for _ in range(6):
                await reconcile_manifest_schedules()
                for _ in range(10):
                    await asyncio.sleep(0)
                clock.advance(minutes=1)
            assert started == ["aaa"]

            # A new unscheduled manifest appears while 'aaa' is still running.
            _write_manifest(tmp_path, "zzz.yml", "zzz")
            await reconcile_manifest_schedules()
            for _ in range(10):
                await asyncio.sleep(0)
            # It is waiting, not discarded.
            assert manifest_queue.pending_manifests() == frozenset({"aaa", "zzz"})
            assert started == ["aaa"]

            hold.set()
            await asyncio.wait_for(manifest_queue._queue.join(), timeout=5)

        assert started == ["aaa", "zzz"]
        assert manifest_queue.pending_manifests() == frozenset()

    @pytest.mark.asyncio
    async def test_repeat_occurrence_of_a_pending_manifest_coalesces(self, tmp_path, caplog):
        """A second occurrence of a still-pending manifest folds into the first.

        Both manifests come due at 10:05; 'aaa' stalls past the 10:10 tick
        with 'bbb' still queued behind it. Neither 10:10 occurrence is
        dropped for being busy -- each merges into the pending run, which
        reloads from disk and so covers the newer occurrence anyway.
        """
        for name, mid in (("a.yml", "aaa"), ("b.yml", "bbb")):
            _write_manifest(tmp_path, name, mid, schedule="*/5 * * * *")
        started = []
        hold = asyncio.Event()

        async def fake_run(manifest):
            started.append(manifest.id)
            if manifest.id == "aaa" and not hold.is_set():
                await hold.wait()
            return {"results": []}

        clock = _FrozenClock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))
        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.settings") as mqs,
            patch("soliplex.agents.server.datetime", clock),
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
            caplog.at_level(logging.INFO),
        ):
            ms.manifest_dir = str(tmp_path)
            mqs.haiku_load_enabled = False
            for _ in range(11):  # 10:00 -> 10:10, 'aaa' stalled throughout
                await reconcile_manifest_schedules()
                for _ in range(10):
                    await asyncio.sleep(0)
                clock.advance(minutes=1)
            hold.set()
            await asyncio.wait_for(manifest_queue._queue.join(), timeout=5)

        # Both ran despite 'aaa' stalling across a tick; the 10:10 pair
        # coalesced rather than queueing a redundant second run of each.
        assert started == ["aaa", "bbb"]
        assert caplog.text.count("coalescing this run") == 2
        assert manifest_queue.pending_manifests() == frozenset()
