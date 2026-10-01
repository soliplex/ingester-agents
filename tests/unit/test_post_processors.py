"""Tests for built-in manifest post-process callbacks — 100% branch coverage."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from soliplex.agents.config import settings
from soliplex.agents.manifest import post_processors
from soliplex.agents.manifest.haiku_process import HaikuRun

# `vacuum` delegates to haiku_maint.run_verb, whose subprocess haiku_process runs.
_EXEC = "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec"
_TIMEOUT = "soliplex.agents.manifest.haiku_process.asyncio.timeout"


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


@pytest.fixture
def lancedb_env(monkeypatch):
    monkeypatch.setattr(settings, "lancedb_dir", "/data/lance", raising=False)
    monkeypatch.setattr(settings, "download_dir", "downloads", raising=False)
    monkeypatch.setattr(
        settings,
        "haiku_maintenance_command",
        "haiku-rag --config={haiku_cfg} {verb} --db={db}",
        raising=False,
    )
    monkeypatch.setattr(settings, "haiku_maintenance_timeout", 3600, raising=False)
    monkeypatch.setattr(settings, "haiku_load_cwd", None, raising=False)


@pytest.mark.asyncio
async def test_vacuum_runs_subprocess_with_config(lancedb_env):
    proc = _fake_proc(returncode=0)
    with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
        await post_processors.vacuum("army-airfield", config="/etc/haiku/haiku.rag.yaml")

    argv = list(mock_exec.call_args.args)
    assert argv[0] == "haiku-rag"
    assert "--config=/etc/haiku/haiku.rag.yaml" in argv
    assert "vacuum" in argv
    # DB resolved to the slugified source under $LANCEDB_DIR.
    assert argv[-1].startswith("--db=")
    assert argv[-1].replace("\\", "/").endswith("army-airfield.lancedb")
    # Env carries SOURCE / DOWNLOAD_DIR so a config with ${SOURCE} resolves.
    env = mock_exec.call_args.kwargs["env"]
    assert env["SOURCE"] == "army-airfield"
    assert env["DOWNLOAD_DIR"] == "downloads"
    assert env["PYTHONUNBUFFERED"] == "1"


@pytest.mark.asyncio
async def test_vacuum_omits_config_when_none(lancedb_env):
    proc = _fake_proc(returncode=0)
    with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
        await post_processors.vacuum("src")

    assert not any(arg.startswith("--config") for arg in mock_exec.call_args.args)


@pytest.mark.asyncio
async def test_vacuum_raises_on_nonzero_exit(lancedb_env):
    proc = _fake_proc(returncode=2, stdout_lines=[], stderr_lines=[b"boom\n"])
    with patch(_EXEC, new_callable=AsyncMock, return_value=proc):
        with pytest.raises(RuntimeError, match="failed"):
            await post_processors.vacuum("src")


@pytest.mark.asyncio
async def test_vacuum_kills_and_raises_on_timeout(lancedb_env):
    proc = _fake_proc(returncode=0)
    with (
        patch(_EXEC, new_callable=AsyncMock, return_value=proc),
        patch(_TIMEOUT, _RaisingTimeout),
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            await post_processors.vacuum("src", timeout=1)

    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


# --- notify_webhook -------------------------------------------------------------


@asynccontextmanager
async def _receiver(status=200):
    received = []

    async def handle(request):
        received.append(await request.json())
        return web.Response(status=status)

    app = web.Application()
    app.router.add_post("/hook", handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/hook")), received
    finally:
        await server.close()


_RUN_RESULT = {
    "manifest_id": "docs",
    "summary": {"components": 2, "ingested": 5, "pre_process_skipped": 1, "not_reported": 9},
}


@pytest.mark.asyncio
async def test_notify_webhook_success():
    run = HaikuRun(returncode=0, timed_out=False, stdout="done", stderr="noise")
    async with _receiver() as (url, received):
        await post_processors.notify_webhook("src", url=url, ingester=run, run_result=_RUN_RESULT)
    assert received == [
        {
            "event": "load.finished",
            "source": "src",
            "status": "ok",
            "returncode": 0,
            "timed_out": False,
            "summary": {"components": 2, "ingested": 5, "pre_process_skipped": 1},
            "manifest_id": "docs",
        }
    ]


@pytest.mark.asyncio
async def test_notify_webhook_failure_includes_the_stderr_tail():
    stderr = "\n".join(f"line {i}" for i in range(30))
    run = HaikuRun(returncode=2, timed_out=False, stdout="", stderr=stderr)
    async with _receiver() as (url, received):
        await post_processors.notify_webhook("src", url=url, ingester=run, stderr_lines=3)
    (body,) = received
    assert body["status"] == "failed"
    assert body["stderr_tail"] == "line 27\nline 28\nline 29"
    assert body["summary"] == {}
    assert "manifest_id" not in body


@pytest.mark.asyncio
async def test_notify_webhook_timeout():
    run = HaikuRun(returncode=None, timed_out=True, stdout="", stderr="stuck")
    async with _receiver() as (url, received):
        await post_processors.notify_webhook("src", url=url, ingester=run, run_result={"summary": None})
    (body,) = received
    assert (body["status"], body["returncode"], body["timed_out"], body["stderr_tail"]) == ("timed_out", None, True, "stuck")


@pytest.mark.asyncio
async def test_notify_webhook_without_a_load():
    async with _receiver() as (url, received):
        await post_processors.notify_webhook("src", url=url)
    (body,) = received
    assert (body["status"], body["returncode"], body["timed_out"]) == ("no_load", None, False)


@pytest.mark.asyncio
async def test_notify_webhook_delivery_failure_raises():
    async with _receiver(status=500) as (url, _):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await post_processors.notify_webhook("src", url=url)
