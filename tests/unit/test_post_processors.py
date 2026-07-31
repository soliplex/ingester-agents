"""Tests for built-in manifest post-process callbacks — 100% branch coverage."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents.config import settings
from soliplex.agents.manifest import post_processors

_EXEC = "soliplex.agents.manifest.post_processors.asyncio.create_subprocess_exec"
_TIMEOUT = "soliplex.agents.manifest.post_processors.asyncio.timeout"


class _FakeStream:
    """Minimal async-iterable stand-in for asyncio.StreamReader."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


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


@pytest.mark.asyncio
async def test_vacuum_runs_subprocess_with_config(lancedb_env):
    proc = _fake_proc(returncode=0)
    with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
        await post_processors.vacuum("army-airfield", config="/etc/haiku/haiku.rag.yaml")

    argv = list(mock_exec.call_args.args)
    assert argv[0] == "haiku-rag"
    assert "--config" in argv
    assert "/etc/haiku/haiku.rag.yaml" in argv
    assert argv[-2] == "--db"
    # DB resolved to the slugified source under $LANCEDB_DIR.
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

    assert "--config" not in mock_exec.call_args.args


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
