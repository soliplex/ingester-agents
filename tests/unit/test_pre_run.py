"""Tests for the pre-run runner -- 100% branch coverage required."""

import asyncio
import logging

import pytest
from opentelemetry.trace import StatusCode

from soliplex.agents import local_state
from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PreRunStep
from soliplex.agents.config import settings
from soliplex.agents.manifest import pre_run
from soliplex.agents.manifest.pre_run import PreRunFailed
from soliplex.agents.manifest.pre_run import PreRunStatus
from soliplex.agents.manifest.pre_run import ResolvedPreRunStep

STARTED = "2026-10-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "download_dir", str(tmp_path / "dl"))


def _manifest(steps=None):
    config = ManifestConfig(pre_run=steps) if steps is not None else None
    return Manifest(id="m", name="M", source="src", config=config, components=[{"type": "fs", "name": "c", "path": "/x"}])


def _steps(*funcs, on_error="fail", timeout=300):
    return [
        ResolvedPreRunStep(i, PreRunStep(method=f"tests:{f.__name__}", on_error=on_error, timeout=timeout), f)
        for i, f in enumerate(funcs)
    ]


async def _run(steps, manifest=None, load="LOAD"):
    return await pre_run.run_pre_run(manifest or _manifest(), steps, load=load, started_at=STARTED)


# --- resolve_steps ---


def test_resolve_steps():
    assert pre_run.resolve_steps(_manifest()) == []
    (step,) = pre_run.resolve_steps(_manifest([PreRunStep(method="os:getcwd")]))
    assert step.index == 0
    with pytest.raises(ModuleNotFoundError):
        pre_run.resolve_steps(_manifest([PreRunStep(method="no.such:f")]))


# --- outcomes ---


@pytest.mark.asyncio
async def test_no_steps():
    assert await _run([]) == {"steps": [], "skipped": None}
    assert local_state.list_pre_run("src") == []


@pytest.mark.asyncio
async def test_continue_runs_every_step_and_records(caplog):
    seen = []

    def first(context, **kwargs):
        seen.append((context.load, context.started_at, context.manifest.id, kwargs))

    async def second(context):
        return PreRunStatus.CONTINUE, "all good"

    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_run"):
        outcome = await _run(_steps(first, second))

    assert seen == [("LOAD", STARTED, "m", {})]
    assert outcome["skipped"] is None
    assert [(s["method"], s["status"], s["message"]) for s in outcome["steps"]] == [
        ("tests:first", "continue", None),
        ("tests:second", "continue", "all good"),
    ]
    assert "pre-run tests:second for manifest 'm': all good" in caplog.text
    rows = local_state.list_pre_run("src")
    assert [(r["started_at"], r["step"], r["status"]) for r in rows] == [(STARTED, 0, "continue"), (STARTED, 1, "continue")]
    assert all(r["duration_s"] >= 0 for r in rows)


@pytest.mark.asyncio
async def test_skip_stops_later_steps(caplog):
    ran = []

    def gate(context):
        return PreRunStatus.SKIP, "maintenance window"

    def later(context):
        ran.append(True)

    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_run"):
        outcome = await _run(_steps(gate, later))

    assert ran == []
    assert outcome["skipped"] == {"method": "tests:gate", "message": "maintenance window"}
    assert "manifest 'm' skipped by tests:gate: maintenance window" in caplog.text
    assert [r["status"] for r in local_state.list_pre_run("src")] == ["skip"]


@pytest.mark.asyncio
async def test_skip_without_message(caplog):
    def gate(context):
        return "skip"

    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_run"):
        outcome = await _run(_steps(gate))
    assert outcome["skipped"] == {"method": "tests:gate", "message": None}
    assert "manifest 'm' skipped by tests:gate\n" in caplog.text


@pytest.mark.asyncio
async def test_a_step_cannot_change_the_manifest():
    manifest = _manifest()

    def meddle(context):
        context.manifest.source = "elsewhere"

    await _run(_steps(meddle), manifest=manifest)
    assert manifest.source == "src"


# --- on_error ---


def _boom(context):
    raise RuntimeError("upstream down")


def _modified(context):
    return "modified"


@pytest.mark.asyncio
async def test_on_error_continue_moves_on(caplog):
    ran = []

    def later(context):
        ran.append(True)

    with caplog.at_level(logging.WARNING, logger="soliplex.agents.manifest.pre_run"):
        outcome = await _run(_steps(_boom, later, on_error="continue"))
    assert ran == [True]
    assert outcome["skipped"] is None
    assert outcome["steps"][0]["status"] == "error"
    assert outcome["steps"][0]["message"] == "RuntimeError: upstream down"
    assert "pre-run tests:_boom failed for manifest 'm'" in caplog.text


@pytest.mark.asyncio
async def test_on_error_skip():
    outcome = await _run(_steps(_boom, on_error="skip"))
    assert outcome["skipped"] == {"method": "tests:_boom", "message": "RuntimeError: upstream down"}
    assert [r["status"] for r in local_state.list_pre_run("src")] == ["error"]


@pytest.mark.asyncio
async def test_on_error_fail_raises_after_recording(spans):
    with pytest.raises(PreRunFailed, match="pre-run tests:_boom failed: RuntimeError: upstream down"):
        await _run(_steps(_boom))
    assert [r["status"] for r in local_state.list_pre_run("src")] == ["error"]
    (span,) = spans.named("pre-run")
    assert span.attributes["pre_run.method"] == "tests:_boom"
    assert span.status.status_code is StatusCode.ERROR
    assert "upstream down" in span.status.description


@pytest.mark.asyncio
async def test_invalid_status_is_an_error():
    outcome = await _run(_steps(_modified, on_error="continue"))
    assert outcome["steps"][0]["status"] == "error"
    assert "'modified' is not a valid PreRunStatus" in outcome["steps"][0]["message"]


@pytest.mark.asyncio
async def test_timeout_is_an_error():
    async def slow(context):
        await asyncio.sleep(10)

    outcome = await _run(_steps(slow, on_error="continue", timeout=0.01))
    assert outcome["steps"][0]["message"] == "timed out after 0.01s"


@pytest.mark.asyncio
async def test_spans_record_status(spans):
    def ok(context):
        return None

    await _run(_steps(ok))
    (span,) = spans.named("pre-run")
    assert span.attributes["pre_run.status"] == "continue"
    assert span.attributes["pre_run.index"] == 0
    assert span.attributes["manifest.id"] == "m"
