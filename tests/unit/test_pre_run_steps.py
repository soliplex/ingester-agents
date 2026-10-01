"""Tests for the built-in pre-run steps and the shared webhook helper.

The webhook is exercised against a real aiohttp server on localhost, so the
request that leaves the agent -- body, headers, status handling -- is the one
a receiver would see.
"""

import json
from collections import namedtuple
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from soliplex.agents.config import Manifest
from soliplex.agents.config import settings
from soliplex.agents.manifest import pre_run_steps
from soliplex.agents.manifest import webhook
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.manifest.pre_run import PreRunContext
from soliplex.agents.manifest.pre_run import PreRunStatus
from soliplex.agents.store import DownloadTarget
from soliplex.agents.store import LocalDocumentStore
from soliplex.agents.store import S3DocumentStore

Usage = namedtuple("Usage", "total used free")
MB = 1024 * 1024


@asynccontextmanager
async def receiver(status=200, body="ok"):
    """A local webhook receiver; yields (url, received-requests list)."""
    received = []

    async def handle(request):
        received.append({"headers": dict(request.headers), "body": await request.json()})
        return web.Response(status=status, text=body)

    app = web.Application()
    app.router.add_post("/hook", handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/hook")), received
    finally:
        await server.close()


def _context(tmp_path, *, s3=False, memory_store=None):
    target = (
        DownloadTarget(dir="downloads", source="src", bucket="bucket")
        if s3
        else DownloadTarget(dir=str(tmp_path / "dl"), source="src")
    )
    store = S3DocumentStore(target) if s3 else LocalDocumentStore(target)
    from soliplex.agents.sidecar import Sidecars

    load = LoadContext(source="src", sanitized="src", store=store, sidecars=Sidecars(store))
    manifest = Manifest(id="m", name="M", source="src", components=[{"type": "fs", "name": "c", "path": "/x"}])
    return PreRunContext(manifest=manifest, load=load, started_at="2026-10-01T00:00:00+00:00")


# --- webhook.resolve_target -------------------------------------------------------


def test_resolve_target_literal_and_secret(monkeypatch):
    monkeypatch.setenv("HOOK_URL", "https://hooks.example/x")
    monkeypatch.setenv("HOOK_TOKEN", "Bearer abc")
    assert webhook.resolve_target("https://a/b", None, {"X-Team": "docs"}, {"Authorization": "HOOK_TOKEN"}) == (
        "https://a/b",
        {"X-Team": "docs", "Authorization": "Bearer abc"},
    )
    assert webhook.resolve_target(None, "HOOK_URL", None, None) == ("https://hooks.example/x", {})


@pytest.mark.parametrize("url, url_secret", [(None, None), ("https://a", "HOOK_URL")])
def test_resolve_target_needs_exactly_one_url(url, url_secret):
    with pytest.raises(ValueError, match="exactly one of 'url' or 'url_secret'"):
        webhook.resolve_target(url, url_secret, None, None)


def test_resolve_target_unknown_secret():
    with pytest.raises(ValueError, match="not found"):
        webhook.resolve_target(None, "NO_SUCH_SECRET_FOR_TESTS", None, None)


# --- webhook.post_json -----------------------------------------------------------


@pytest.mark.asyncio
async def test_post_json_delivers():
    async with receiver(status=204) as (url, received):
        assert await webhook.post_json(url, {"a": 1}, headers={"X-Test": "1"}) == 204
    (request,) = received
    assert request["body"] == {"a": 1}
    assert request["headers"]["X-Test"] == "1"


@pytest.mark.asyncio
async def test_post_json_non_2xx_raises_with_body(monkeypatch):
    monkeypatch.setattr(settings, "ssl_verify", False)
    async with receiver(status=500, body="nope " * 100) as (url, _):
        with pytest.raises(RuntimeError, match="webhook returned HTTP 500: nope") as excinfo:
            await webhook.post_json(url, {})
    assert len(str(excinfo.value)) < 250


# --- notify_webhook ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_webhook_posts_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK_TOKEN", "secret-token")
    context = _context(tmp_path)
    async with receiver() as (url, received):
        status = await pre_run_steps.notify_webhook(context, url=url, secret_headers={"Authorization": "HOOK_TOKEN"})
    assert status is PreRunStatus.CONTINUE
    (request,) = received
    assert request["headers"]["Authorization"] == "secret-token"
    assert request["body"] == {
        "event": "manifest.started",
        "manifest_id": "m",
        "manifest_name": "M",
        "source": "src",
        "started_at": "2026-10-01T00:00:00+00:00",
        "download_uri": context.load.download_uri,
    }
    json.dumps(request["body"])


@pytest.mark.asyncio
async def test_notify_webhook_failure_raises(tmp_path):
    async with receiver(status=503) as (url, _):
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await pre_run_steps.notify_webhook(_context(tmp_path), url=url)


# --- check_free_space ----------------------------------------------------------------


def test_free_space_enough_on_local(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pre_process_spool_dir", str(tmp_path))
    with patch.object(pre_run_steps.shutil, "disk_usage", return_value=Usage(0, 0, 5000 * MB)) as usage:
        assert pre_run_steps.check_free_space(_context(tmp_path), min_free_mb=2048) is PreRunStatus.CONTINUE
    # Both the (not yet created) download dir and the spool dir were checked,
    # each via its nearest existing directory.
    assert [Path(call.args[0]) for call in usage.call_args_list] == [tmp_path.resolve(), tmp_path.resolve()]


def test_free_space_short_on_the_download_dir(tmp_path):
    with patch.object(pre_run_steps.shutil, "disk_usage", return_value=Usage(0, 0, 1024 * MB)):
        status, message = pre_run_steps.check_free_space(_context(tmp_path), min_free_mb=2048, include_spool=False)
    assert status is PreRunStatus.SKIP
    assert message == f"1.0 GB free under {tmp_path / 'dl'} (download dir), need 2.0 GB"


def test_free_space_s3_checks_only_the_spool(tmp_path, monkeypatch, memory_store):
    monkeypatch.setattr(settings, "pre_process_spool_dir", None)
    with patch.object(pre_run_steps.shutil, "disk_usage", return_value=Usage(0, 0, 5000 * MB)) as usage:
        status, message = pre_run_steps.check_free_space(_context(tmp_path, s3=True), min_free_mb=10)
    assert status is PreRunStatus.CONTINUE
    assert message == "free-space check not applicable to object storage"
    assert usage.call_count == 1


def test_free_space_s3_spool_short(tmp_path, monkeypatch, memory_store):
    monkeypatch.setattr(settings, "pre_process_spool_dir", str(tmp_path))
    with patch.object(pre_run_steps.shutil, "disk_usage", return_value=Usage(0, 0, 0)):
        status, message = pre_run_steps.check_free_space(_context(tmp_path, s3=True), min_free_mb=1)
    assert status is PreRunStatus.SKIP
    assert "(spool dir)" in message


def test_existing_walks_up_to_a_real_directory(tmp_path):
    assert pre_run_steps._existing(tmp_path / "a" / "b" / "c") == tmp_path.resolve()
