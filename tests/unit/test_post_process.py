"""Tests for the manifest post-process runner — 100% branch coverage required."""

import os

import pytest

from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PostProcessStep
from soliplex.agents.manifest import post_process


def _manifest(steps=None, *, with_config=True, source="src"):
    config = ManifestConfig(post_process=steps or []) if with_config else None
    return Manifest(
        id="m",
        name="M",
        source=source,
        config=config,
        components=[{"type": "fs", "name": "c", "path": "/data"}],
    )


# --- _resolve_method ---


def test_resolve_method_colon():
    assert post_process._resolve_method("os:getcwd") is os.getcwd


def test_resolve_method_dotted():
    assert post_process._resolve_method("os.getcwd") is os.getcwd


# --- _wants_config ---


def test_wants_config_named_param():
    def method(source, *, config=None): ...

    assert post_process._wants_config(method) is True


def test_wants_config_var_keyword():
    def method(source, **kwargs): ...

    assert post_process._wants_config(method) is True


def test_wants_config_absent():
    def method(source, *, x=1): ...

    assert post_process._wants_config(method) is False


# --- run_post_process ---


@pytest.mark.asyncio
async def test_no_config_returns_empty():
    assert await post_process.run_post_process(_manifest(with_config=False)) == []


@pytest.mark.asyncio
async def test_empty_steps_returns_empty():
    assert await post_process.run_post_process(_manifest(steps=[])) == []


@pytest.mark.asyncio
async def test_runs_in_order_with_inject_and_sync_async(monkeypatch):
    calls: list[tuple] = []

    async def async_cb(source, **kwargs):  # **kwargs -> wants config
        calls.append(("async", source, kwargs))

    def sync_cb(source, *, config=None, x=None):  # named config -> wants config
        calls.append(("sync", source, {"config": config, "x": x}))

    def no_config_cb(source, *, y=None):  # no config param -> no inject
        calls.append(("noconf", source, {"y": y}))

    registry = {"a": async_cb, "s": sync_cb, "n": no_config_cb}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "CFG")

    steps = [
        PostProcessStep(method="a"),  # inject CFG (via **kwargs), awaited
        PostProcessStep(method="s", kwargs={"x": 1}),  # inject CFG (named), sync
        PostProcessStep(method="s", kwargs={"config": "OWN", "x": 2}),  # no inject
        PostProcessStep(method="n"),  # no inject (method rejects config)
    ]
    results = await post_process.run_post_process(_manifest(steps=steps, source="s1"))

    assert [r["ok"] for r in results] == [True, True, True, True]
    assert all(r["error"] is None for r in results)
    assert calls == [
        ("async", "s1", {"config": "CFG"}),
        ("sync", "s1", {"config": "CFG", "x": 1}),
        ("sync", "s1", {"config": "OWN", "x": 2}),
        ("noconf", "s1", {"y": None}),
    ]


@pytest.mark.asyncio
async def test_failing_step_is_logged_and_others_continue(monkeypatch):
    ran: list[str] = []

    def boom(source, **kwargs):
        raise RuntimeError("nope")

    def ok(source, **kwargs):
        ran.append(source)

    registry = {"boom": boom, "ok": ok}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "CFG")

    steps = [PostProcessStep(method="boom"), PostProcessStep(method="ok")]
    results = await post_process.run_post_process(_manifest(steps=steps))

    assert results[0]["ok"] is False
    assert "nope" in results[0]["error"]
    assert results[1]["ok"] is True
    assert ran == ["src"]  # the step after the failure still ran
