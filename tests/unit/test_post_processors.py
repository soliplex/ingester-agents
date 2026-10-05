"""Tests for built-in manifest post-process callbacks — 100% branch coverage."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
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
    assert env["DOWNLOAD_DIR"] == str(Path("downloads").resolve())
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


# --- backfill_metadata ----------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_metadata_runs_the_backfill_subprocess(lancedb_env, caplog):
    proc = _fake_proc(returncode=0, stdout_lines=[b'BACKFILL_SUMMARY {"updated": 2, "errors": []}\n'])
    with caplog.at_level(logging.INFO), patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
        await post_processors.backfill_metadata(
            "src",
            config="/etc/haiku/h.yaml",
            missing=["page_count"],
            content_types=["application/pdf"],
            doc_filter="uri LIKE '%.pdf'",
            database="db",
            batch_size=100,
        )

    argv = list(mock_exec.call_args.args)
    assert argv[1:] == [
        "-m",
        "soliplex.agents.haiku_backfill",
        "--config=/etc/haiku/h.yaml",
        "--db-name=db",
        "--missing=page_count",
        "--content-type=application/pdf",
        "--filter=uri LIKE '%.pdf'",
        "--batch-size=100",
    ]
    assert mock_exec.call_args.kwargs["env"]["SOURCE"] == "src"
    assert "Metadata back-fill completed for source 'src'" in caplog.text


@pytest.mark.asyncio
async def test_backfill_metadata_refuses_an_unscoped_pass(lancedb_env):
    with patch(_EXEC, new_callable=AsyncMock) as mock_exec, pytest.raises(ValueError, match="`full: true`"):
        await post_processors.backfill_metadata("src")
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_metadata_full_pass_when_asked(lancedb_env):
    with patch(_EXEC, new_callable=AsyncMock, return_value=_fake_proc()) as mock_exec:
        await post_processors.backfill_metadata("src", full=True)
    assert list(mock_exec.call_args.args)[3:] == []


@pytest.mark.asyncio
async def test_backfill_metadata_can_leave_attachments_alone(lancedb_env):
    with patch(_EXEC, new_callable=AsyncMock, return_value=_fake_proc()) as mock_exec:
        await post_processors.backfill_metadata("src", full=True, attachments=False)
    assert list(mock_exec.call_args.args)[3:] == ["--no-attachments"]


@pytest.mark.asyncio
async def test_backfill_metadata_doc_filter_alone_scopes_it(lancedb_env):
    with patch(_EXEC, new_callable=AsyncMock, return_value=_fake_proc()) as mock_exec:
        await post_processors.backfill_metadata("src", doc_filter="uri LIKE '%.pdf'")
    assert list(mock_exec.call_args.args)[3:] == ["--filter=uri LIKE '%.pdf'"]


@pytest.mark.asyncio
async def test_backfill_metadata_logs_partial_failure_without_raising(lancedb_env, caplog):
    summary = b'BACKFILL_SUMMARY {"errors": [{"uri": "file:///a.pdf", "error": "boom"}]}\n'
    proc = _fake_proc(returncode=3, stdout_lines=[summary])
    with caplog.at_level(logging.WARNING), patch(_EXEC, new_callable=AsyncMock, return_value=proc):
        await post_processors.backfill_metadata("src", missing=["page_count"])
    assert "could not fill 1 document(s): file:///a.pdf" in caplog.text


@pytest.mark.asyncio
async def test_backfill_metadata_raises_on_crash(lancedb_env):
    proc = _fake_proc(returncode=1, stdout_lines=[], stderr_lines=[b"Traceback\n"])
    with patch(_EXEC, new_callable=AsyncMock, return_value=proc), pytest.raises(RuntimeError, match="rc=1"):
        await post_processors.backfill_metadata("src", missing=["page_count"])


@pytest.mark.asyncio
async def test_backfill_metadata_raises_on_timeout(lancedb_env):
    proc = _fake_proc(returncode=0)
    with (
        patch(_EXEC, new_callable=AsyncMock, return_value=proc),
        patch(_TIMEOUT, _RaisingTimeout),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        await post_processors.backfill_metadata("src", missing=["page_count"], timeout=1)
    proc.kill.assert_called_once()


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
