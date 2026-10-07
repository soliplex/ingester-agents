"""Tests for the manifest post-process runner — 100% branch coverage required."""

import os
from pathlib import Path

import pytest
from opentelemetry.trace import StatusCode

from soliplex.agents import store as agent_store
from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PostProcessStep
from soliplex.agents.manifest import post_process
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.manifest.haiku_process import HaikuRun


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
    assert [r["status"] for r in results] == ["ok"] * 4
    assert all(r["error"] is None for r in results)
    assert all(r["duration_s"] >= 0 for r in results)
    # **kwargs accepts config, ingester_exit_code and context -> all injected
    assert calls[0][:2] == ("async", "s1")
    assert calls[0][2]["config"] == "CFG"
    assert calls[0][2]["ingester_exit_code"] is None
    assert calls[0][2]["context"].source == "s1"
    assert calls[1:] == [
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
        assert kwargs["context"].source == "src"

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
    await post_process.run_post_process(
        _manifest(steps=steps), ingester=HaikuRun(returncode=2, timed_out=False, stdout="", stderr="")
    )

    assert calls == [("named", 2), ("kwargs", 2), ("none", None)]


@pytest.mark.asyncio
async def test_failing_step_terminates_and_skips_rest(monkeypatch):
    ran: list[str] = []

    def boom(source, **kwargs):
        raise RuntimeError("nope")

    def later(source, **kwargs):
        ran.append(source)

    registry = {"boom": boom, "later": later}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "CFG")

    steps = [PostProcessStep(method="later"), PostProcessStep(method="boom"), PostProcessStep(method="later")]

    # The error propagates (terminate on error) rather than being swallowed,
    # carrying every step's outcome.
    with pytest.raises(post_process.PostProcessFailed, match="post-process boom failed: RuntimeError: nope") as info:
        await post_process.run_post_process(_manifest(steps=steps))

    assert ran == ["src"]  # only the step before the failure ran
    assert isinstance(info.value.__cause__, RuntimeError)
    first, failed, skipped = info.value.steps
    assert (first["method"], first["status"], first["ok"], first["error"]) == ("later", "ok", True, None)
    assert (failed["method"], failed["status"], failed["ok"]) == ("boom", "error", False)
    assert failed["error"] == "RuntimeError: nope"
    assert failed["duration_s"] >= 0
    assert skipped == {"method": "later", "status": "not_run", "ok": False, "error": None, "duration_s": None}


@pytest.mark.asyncio
async def test_an_unresolvable_method_is_a_failed_step(monkeypatch):
    def unresolvable(spec):
        raise ImportError(f"no module for {spec}")

    monkeypatch.setattr(post_process, "_resolve_method", unresolvable)

    with pytest.raises(post_process.PostProcessFailed) as info:
        await post_process.run_post_process(_manifest(steps=[PostProcessStep(method="pkg:gone")]))

    (failed,) = info.value.steps
    assert failed["status"] == "error"
    assert failed["error"] == "ImportError: no module for pkg:gone"


@pytest.mark.asyncio
async def test_each_step_gets_a_span(monkeypatch, spans):
    registry = {"pkg:first": lambda source, **kw: None, "pkg:second": lambda source, **kw: None}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "CFG")
    steps = [PostProcessStep(method="pkg:first"), PostProcessStep(method="pkg:second")]

    await post_process.run_post_process(
        _manifest(steps=steps), ingester=HaikuRun(returncode=0, timed_out=False, stdout="", stderr="")
    )

    first, second = spans.named("post-process")
    assert first.attributes["logfire.msg"] == "post-process pkg:first"
    assert first.attributes["post_process.method"] == "pkg:first"
    assert first.attributes["post_process.index"] == 0
    assert first.attributes["manifest.id"] == "m"
    assert first.attributes["manifest.source"] == "src"
    assert first.attributes["haiku.returncode"] == 0
    assert second.attributes["post_process.index"] == 1
    assert first.status.status_code is StatusCode.UNSET


@pytest.mark.asyncio
async def test_the_failing_step_span_is_an_error_and_later_steps_have_none(monkeypatch, spans):
    def boom(source, **kwargs):
        raise RuntimeError("nope")

    registry = {"pkg:boom": boom, "pkg:later": lambda source, **kw: None}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "CFG")
    steps = [PostProcessStep(method="pkg:boom"), PostProcessStep(method="pkg:later")]

    with pytest.raises(post_process.PostProcessFailed):
        await post_process.run_post_process(_manifest(steps=steps))

    (failed,) = spans.named("post-process")
    assert failed.attributes["post_process.method"] == "pkg:boom"
    # A timed-out load passes no exit code; the attribute is left off, not None.
    assert "haiku.returncode" not in failed.attributes
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.events[0].name == "exception"


# --- _load_env ---


def test_load_env_sets_and_restores(monkeypatch):
    # SOURCE preexists (restored to old value); DOWNLOAD_DIR is unset (popped).
    monkeypatch.setenv("SOURCE", "preexisting")
    monkeypatch.delenv("DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(agent_store.settings, "download_dir", "downloads")

    monkeypatch.delenv("DOWNLOAD_URI", raising=False)

    manifest = _manifest(source="army-airfield")
    with post_process._load_env(manifest, LoadContext.for_source(manifest.source)):
        assert os.environ["SOURCE"] == "army-airfield"
        assert os.environ["DOWNLOAD_DIR"] == str(Path("downloads").resolve())
        # The resolved base URI is exposed too, so a config can use one form
        # regardless of backend.
        assert os.environ["DOWNLOAD_URI"].startswith("file://")

    assert os.environ["SOURCE"] == "preexisting"  # restored
    assert "DOWNLOAD_DIR" not in os.environ  # popped
    assert "DOWNLOAD_URI" not in os.environ  # popped


def test_load_env_sanitizes_source(monkeypatch):
    monkeypatch.delenv("SOURCE", raising=False)
    monkeypatch.setattr(agent_store.settings, "download_dir", "downloads")

    manifest = _manifest(source="gitea:admin:repo")
    with post_process._load_env(manifest, LoadContext.for_source(manifest.source)):
        assert os.environ["SOURCE"] == "gitea_admin_repo"  # ':' -> '_'


@pytest.mark.asyncio
async def test_injects_ingester_and_run_result(monkeypatch):
    seen = {}
    run = HaikuRun(returncode=1, timed_out=False, stdout="out", stderr="boom")

    def wants_both(source, *, ingester=None, run_result=None):
        seen["ingester"] = ingester
        seen["run_result"] = run_result

    def explicit(source, *, run_result=None):
        seen["explicit"] = run_result

    registry = {"a": wants_both, "b": explicit}
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: registry[spec])
    steps = [PostProcessStep(method="a"), PostProcessStep(method="b", kwargs={"run_result": "mine"})]

    await post_process.run_post_process(_manifest(steps=steps), ingester=run, run_result={"manifest_id": "m"})

    assert seen == {"ingester": run, "run_result": {"manifest_id": "m"}, "explicit": "mine"}


@pytest.mark.asyncio
async def test_no_ingester_means_no_exit_code(monkeypatch):
    seen = {}

    def wants_code(source, *, ingester_exit_code="unset", ingester="unset"):
        seen["code"] = ingester_exit_code
        seen["ingester"] = ingester

    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: wants_code)
    await post_process.run_post_process(_manifest(steps=[PostProcessStep(method="a")]))
    assert seen == {"code": None, "ingester": None}
