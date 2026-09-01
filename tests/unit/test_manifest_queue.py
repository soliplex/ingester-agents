"""Tests for the serialized manifest run queue."""

import asyncio
import logging
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
import pytest_asyncio

from soliplex.agents.server import manifest_queue


@pytest_asyncio.fixture(autouse=True)
async def _stopped_worker():
    """Guarantee no worker leaks between tests."""
    yield
    await manifest_queue.stop_worker()


def _write_manifest(tmp_path, name, mid):
    """Write a minimal manifest YAML file and return its path."""
    lines = [
        f"id: {mid}",
        f"name: Manifest {mid}",
        f"source: src-{mid}",
        "components:",
        "  - type: fs",
        "    name: comp",
        "    path: /data",
    ]
    (tmp_path / name).write_text("\n".join(lines) + "\n")
    return str(tmp_path / name)


async def _drain():
    """Let the worker run until the queue is empty."""
    await asyncio.wait_for(manifest_queue._queue.join(), timeout=5)


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_warns_and_drops_when_worker_not_started(self, caplog):
        with caplog.at_level(logging.WARNING):
            await manifest_queue.enqueue_manifest("x", "/nope.yml")
        assert "queue not started" in caplog.text

    @pytest.mark.asyncio
    async def test_coalesces_a_manifest_already_pending(self, caplog):
        manifest_queue.start_worker()
        # Park the worker on an item so the second enqueue sees it pending.
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                new_callable=AsyncMock,
                return_value={"results": []},
            ),
            caplog.at_level(logging.INFO),
        ):
            ms.haiku_load_enabled = False
            await manifest_queue.enqueue_manifest("dup", "/a.yml")
            await manifest_queue.enqueue_manifest("dup", "/a.yml")
            assert manifest_queue.pending_manifests() == frozenset({"dup"})
        assert "coalescing this run" in caplog.text

    @pytest.mark.asyncio
    async def test_pending_clears_after_the_run(self, tmp_path):
        path = _write_manifest(tmp_path, "a.yml", "aaa")
        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                new_callable=AsyncMock,
                return_value={"results": []},
            ),
        ):
            ms.haiku_load_enabled = False
            await manifest_queue.enqueue_manifest("aaa", path)
            await _drain()
        assert manifest_queue.pending_manifests() == frozenset()


class TestWorker:
    @pytest.mark.asyncio
    async def test_runs_queued_manifests_in_order(self, tmp_path):
        paths = [(_write_manifest(tmp_path, f"{m}.yml", m), m) for m in ("aaa", "bbb", "ccc")]
        started = []

        async def fake_run(manifest):
            started.append(manifest.id)
            await asyncio.sleep(0)  # a real run yields on I/O
            return {"results": []}

        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
        ):
            ms.haiku_load_enabled = False
            for path, mid in paths:
                await manifest_queue.enqueue_manifest(mid, path)
            await _drain()
        assert started == ["aaa", "bbb", "ccc"]

    @pytest.mark.asyncio
    async def test_only_one_manifest_runs_at_a_time(self, tmp_path):
        paths = [(_write_manifest(tmp_path, f"{m}.yml", m), m) for m in ("aaa", "bbb", "ccc")]
        concurrent = 0
        peak = 0

        async def fake_run(manifest):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            return {"results": []}

        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
        ):
            ms.haiku_load_enabled = False
            for path, mid in paths:
                await manifest_queue.enqueue_manifest(mid, path)
            await _drain()
        assert peak == 1

    @pytest.mark.asyncio
    async def test_a_failing_manifest_does_not_stop_the_queue(self, tmp_path, caplog):
        paths = [(_write_manifest(tmp_path, f"{m}.yml", m), m) for m in ("aaa", "bbb")]
        started = []

        async def fake_run(manifest):
            started.append(manifest.id)
            if manifest.id == "aaa":
                raise RuntimeError("boom")
            return {"results": []}

        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
            caplog.at_level(logging.ERROR),
        ):
            ms.haiku_load_enabled = False
            for path, mid in paths:
                await manifest_queue.enqueue_manifest(mid, path)
            await _drain()
        assert started == ["aaa", "bbb"]
        assert "Error running manifest 'aaa'" in caplog.text
        assert manifest_queue.pending_manifests() == frozenset()

    @pytest.mark.asyncio
    async def test_queues_a_haiku_load_when_enabled(self, tmp_path):
        path = _write_manifest(tmp_path, "a.yml", "aaa")
        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                new_callable=AsyncMock,
                return_value={"results": []},
            ),
            patch("soliplex.agents.server.manifest_queue.enqueue_load", new_callable=AsyncMock) as mock_load,
        ):
            ms.haiku_load_enabled = True
            await manifest_queue.enqueue_manifest("aaa", path)
            await _drain()
        assert mock_load.await_count == 1
        assert mock_load.await_args.args[0].id == "aaa"


class TestWorkerLifecycle:
    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        manifest_queue.start_worker()
        first = manifest_queue._worker_task
        manifest_queue.start_worker()
        assert manifest_queue._worker_task is first

    @pytest.mark.asyncio
    async def test_stop_without_start_is_a_noop(self):
        await manifest_queue.stop_worker()
        assert manifest_queue._worker_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_the_in_flight_run_and_clears_state(self, tmp_path):
        path = _write_manifest(tmp_path, "a.yml", "aaa")
        running = asyncio.Event()

        async def fake_run(manifest):
            running.set()
            await asyncio.sleep(60)  # never completes on its own
            return {"results": []}

        manifest_queue.start_worker()
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch("soliplex.agents.manifest.runner.run_manifest", side_effect=fake_run),
        ):
            ms.haiku_load_enabled = False
            await manifest_queue.enqueue_manifest("aaa", path)
            await asyncio.wait_for(running.wait(), timeout=5)
            await asyncio.wait_for(manifest_queue.stop_worker(), timeout=5)

        assert manifest_queue._worker_task is None
        assert manifest_queue._queue is None
        assert manifest_queue.pending_manifests() == frozenset()
