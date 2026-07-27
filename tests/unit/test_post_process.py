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


# --- _accepts_kwarg ---


def test_accepts_kwarg_named_param():
    def method(source, *, config=None): ...

    assert post_process._accepts_kwarg(method, "config") is True


def test_accepts_kwarg_var_keyword():
    def method(source, **kwargs): ...

    assert post_process._accepts_kwarg(method, "anything") is True


def test_accepts_kwarg_absent():
    def method(source, *, x=1): ...

    assert post_process._accepts_kwarg(method, "config") is False


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
        # **kwargs accepts both config and ingester_exit_code -> both injected
        ("async", "s1", {"config": "CFG", "ingester_exit_code": None}),
        ("sync", "s1", {"config": "CFG", "x": 1}),
        ("sync", "s1", {"config": "OWN", "x": 2}),
        ("noconf", "s1", {"y": None}),
    ]


@pytest.mark.asyncio
async def test_injects_ingester_exit_code_when_accepted(monkeypatch):
    calls: list[tuple] = []

    def wants_code(source, *, ingester_exit_code=None):
        calls.append(("named", ingester_exit_code))

    def wants_kwargs(source, **kwargs):
        calls.append(("kwargs", kwargs.get("ingester_exit_code")))

    def no_code(source, *, x=None):  # no exit-code param, no **kwargs -> no inject
        calls.append(("none", x))

    registry = {"a": wants_code, "b": wants_kwargs, "c": no_code}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "CFG")

    steps = [
        PostProcessStep(method="a"),
        PostProcessStep(method="b"),
        PostProcessStep(method="c"),
    ]
    await post_process.run_post_process(_manifest(steps=steps), ingester_exit_code=2)

    assert calls == [("named", 2), ("kwargs", 2), ("none", None)]


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


# --- _load_env ---


def test_load_env_sets_and_restores(monkeypatch):
    # SOURCE preexists (restored to old value); DOWNLOAD_DIR is unset (popped).
    monkeypatch.setenv("SOURCE", "preexisting")
    monkeypatch.delenv("DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(post_process.settings, "download_dir", "downloads", raising=False)

    with post_process._load_env(_manifest(source="army-airfield")):
        assert os.environ["SOURCE"] == "army-airfield"
        assert os.environ["DOWNLOAD_DIR"] == "downloads"

    assert os.environ["SOURCE"] == "preexisting"  # restored
    assert "DOWNLOAD_DIR" not in os.environ  # popped


def test_load_env_sanitizes_source(monkeypatch):
    monkeypatch.delenv("SOURCE", raising=False)
    monkeypatch.setattr(post_process.settings, "download_dir", "downloads", raising=False)

    with post_process._load_env(_manifest(source="gitea:admin:repo")):
        assert os.environ["SOURCE"] == "gitea_admin_repo"  # ':' -> '_'
