"""Tests for the per-document pre-process runner -- 100% branch coverage required.

Most tests go through :func:`local_store.write_document`, the one place the
hook lives, and run against both download backends: a step works on a local
spooled copy, so what it decides must land the same way on disk and in S3.
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents import store as agent_store
from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PreProcessStep
from soliplex.agents.config import settings
from soliplex.agents.manifest import pre_process
from soliplex.agents.manifest.pre_process import PreProcessFailed
from soliplex.agents.manifest.pre_process import PreProcessResult
from soliplex.agents.manifest.pre_process import PreProcessRun
from soliplex.agents.manifest.pre_process import PreProcessStatus
from soliplex.agents.manifest.pre_process import ResolvedStep

SOURCE = "src"
URI = "docs/note.txt"
KEY = "docs/note.txt"
MIME = "text/plain"


@pytest.fixture(params=["local", "s3"])
def env(request, tmp_path, monkeypatch, memory_store):
    """State, downloads and spool under tmp_path, on the parametrized backend."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "download_dir", str(tmp_path / "dl"))
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(settings, "pre_process_spool_dir", str(spool))
    monkeypatch.setattr(settings, "download_s3_bucket", "bucket" if request.param == "s3" else None)
    agent_store.reset_store_cache()
    yield spool
    agent_store.reset_store_cache()


def _store():
    return agent_store.get_document_store(SOURCE)


async def _read(key=KEY):
    return await _store().read(key)


async def _meta(key=KEY):
    return json.loads(await _store().read(key + ".meta.json"))


def _step(func, name, *, index=0, mime_types=None, on_error="continue", kwargs=None):
    step = PreProcessStep(method=f"tests:{name}", mime_types=mime_types, on_error=on_error, kwargs=kwargs or {})
    return ResolvedStep(index, step, func)


def _run(*steps, context=None):
    resolved = [ResolvedStep(i, s.step, s.method) for i, s in enumerate(steps)]
    return PreProcessRun(manifest_id="m", source=SOURCE, steps=resolved, context=context)


async def _write(run, content=b"hello", *, uri=URI, mime=MIME, metadata=None):
    with pre_process.activate(run):
        return await local_store.write_document(SOURCE, uri, content, mime, metadata)


def _spool_is_empty(spool: Path) -> bool:
    return not any(spool.iterdir())


# --- resolve_steps -------------------------------------------------------------


def _manifest(config):
    return Manifest(id="m", name="M", source=SOURCE, config=config, components=[{"type": "fs", "name": "c", "path": "/x"}])


def test_resolve_steps_nothing_unless_listed():
    for config in (None, ManifestConfig(), ManifestConfig(pre_process=[])):
        assert pre_process.resolve_steps(_manifest(config)) == []


def test_resolve_steps_listed():
    steps = pre_process.resolve_steps(_manifest(ManifestConfig(pre_process=[PreProcessStep(method="os:getcwd")])))
    assert [(s.index, s.step.method) for s in steps] == [(0, "os:getcwd")]


def test_resolve_steps_bad_path_raises():
    with pytest.raises(ModuleNotFoundError):
        pre_process.resolve_steps(_manifest(ManifestConfig(pre_process=[PreProcessStep(method="no.such.module:f")])))


# --- matching / activate -------------------------------------------------------


def test_matches():
    any_type = _step(None, "a")
    pdf = _step(None, "b", mime_types=["Application/PDF"])
    assert any_type.matches(None)
    assert any_type.matches("text/plain")
    assert pdf.matches("application/pdf")
    assert not pdf.matches("text/plain")
    assert not pdf.matches(None)
    run = _run(any_type, pdf)
    assert [s.step.method for s in run.matching("text/plain")] == ["tests:a"]


def test_activate_sets_and_clears_even_on_error():
    run = _run()
    assert pre_process.current() is None
    seen = []

    def body():
        with pre_process.activate(run):
            seen.append(pre_process.current())
            raise RuntimeError

    with pytest.raises(RuntimeError):
        body()
    assert seen == [run]
    assert pre_process.current() is None


def test_document_read_bytes(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    doc = pre_process.PreProcessDocument("s", "u", "k", None, path, tmp_path, "sha")
    assert doc.read_bytes() == b"abc"


# --- write_document: no run / no match -----------------------------------------


@pytest.mark.asyncio
async def test_no_active_run_writes_as_before(env):
    await local_store.write_document(SOURCE, URI, b"plain", MIME)
    assert await _read() == b"plain"
    assert _spool_is_empty(env)
    assert local_state.get_pre_process_document(SOURCE, URI) is None


@pytest.mark.asyncio
async def test_no_matching_step_does_not_spool(env):
    def never(document):
        raise AssertionError("must not run")

    run = _run(_step(never, "never", mime_types=["application/pdf"]))
    await _write(run, b"plain")
    assert await _read() == b"plain"
    assert run.checked == 0
    assert _spool_is_empty(env)


# --- CONTINUE ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_stores_the_original_and_audits(env):
    seen = {}

    def look(document):
        seen["doc"] = document
        seen["content"] = document.read_bytes()
        seen["workdir_exists"] = document.workdir.is_dir()

    run = _run(_step(look, "look"))
    await _write(run, b"hello")

    assert await _read() == b"hello"
    assert seen["content"] == b"hello"
    assert seen["workdir_exists"]
    doc = seen["doc"]
    assert (doc.source, doc.uri, doc.key, doc.mime_type) == (SOURCE, URI, KEY, MIME)
    assert doc.path.name == "note.txt"
    assert run.report() == {"checked": 1, "modified": [], "skipped": [], "errors": []}

    audit = local_state.get_pre_process_document(SOURCE, URI)
    assert audit["status"] == "continue"
    assert audit["method"] is None
    assert audit["input_sha256"] == audit["output_sha256"]
    assert audit["previous_input_sha256"] is None
    (row,) = local_state.get_pre_process_steps(SOURCE, URI)
    assert (row["method"], row["status"], row["message"]) == ("tests:look", "continue", None)
    assert _spool_is_empty(env)


@pytest.mark.asyncio
async def test_async_step_and_context_injection(env):
    seen = {}

    async def look(document, *, context=None, flag=False):
        seen["context"] = context
        seen["flag"] = flag
        return PreProcessStatus.CONTINUE, "looked"

    run = _run(_step(look, "look", kwargs={"flag": True}), context="CTX")
    await _write(run)
    assert seen == {"context": "CTX", "flag": True}
    (row,) = local_state.get_pre_process_steps(SOURCE, URI)
    assert row["message"] == "looked"


# --- SKIP ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_writes_nothing_and_removes_the_stored_version(env, caplog):
    await local_store.write_document(SOURCE, URI, b"old version", MIME)
    assert await _store().exists(KEY)

    ran = []

    def skip(document):
        return PreProcessStatus.SKIP, "password protected"

    def after(document):
        ran.append(True)

    run = _run(_step(skip, "skip"), _step(after, "after"))
    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_process"):
        path = await _write(run, b"new version")

    assert path.name == "note.txt"
    assert not await _store().exists(KEY)
    assert not await _store().exists(KEY + ".meta.json")
    assert ran == []
    assert run.skipped == [{"uri": URI, "method": "tests:skip", "message": "password protected"}]
    assert "pre-process tests:skip skipped docs/note.txt: password protected" in caplog.text
    audit = local_state.get_pre_process_document(SOURCE, URI)
    assert (audit["status"], audit["method"], audit["message"], audit["output_sha256"]) == (
        "skip",
        "tests:skip",
        "password protected",
        None,
    )
    assert len(local_state.get_pre_process_steps(SOURCE, URI)) == 1
    assert _spool_is_empty(env)


@pytest.mark.asyncio
async def test_skipped_again_unchanged_logs_debug_changed_logs_info(env, caplog):
    def skip(document):
        return PreProcessStatus.SKIP

    run = _run(_step(skip, "skip"))
    await _write(run, b"locked")
    first = local_state.get_pre_process_document(SOURCE, URI)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="soliplex.agents.manifest.pre_process"):
        await _write(run, b"locked")
    again = [r for r in caplog.records if "skipped docs/note.txt again" in r.getMessage()]
    assert [r.levelno for r in again] == [logging.DEBUG]
    assert "re-fetched with unchanged content" in caplog.text
    assert local_state.get_pre_process_document(SOURCE, URI)["hash_changed_at"] == first["hash_changed_at"]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_process"):
        await _write(run, b"locked v2")
    assert "new version of docs/note.txt still skipped" in caplog.text
    assert "content of docs/note.txt changed" in caplog.text
    latest = local_state.get_pre_process_document(SOURCE, URI)
    assert latest["previous_input_sha256"] == first["input_sha256"]
    assert latest["input_sha256"] != first["input_sha256"]


# --- MODIFIED ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modified_data_is_stored_and_seen_by_the_next_step(env, caplog):
    seen = {}

    def upper(document):
        return PreProcessResult(PreProcessStatus.MODIFIED, "upper-cased", data=document.read_bytes().upper())

    def look(document):
        seen["content"] = document.read_bytes()

    run = _run(_step(upper, "upper"), _step(look, "look", index=1))
    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_process"):
        await _write(run, b"hello")

    assert await _read() == b"HELLO"
    assert seen["content"] == b"HELLO"
    meta = await _meta()
    assert meta["size"] == 5
    assert run.modified == [{"uri": URI, "method": "tests:upper", "message": "upper-cased"}]
    assert "pre-process tests:upper modified docs/note.txt: upper-cased" in caplog.text
    audit = local_state.get_pre_process_document(SOURCE, URI)
    assert audit["status"] == "modified"
    assert audit["input_sha256"] != audit["output_sha256"]
    first, second = local_state.get_pre_process_steps(SOURCE, URI)
    assert first["status"] == "modified"
    assert first["output_sha256"] == second["input_sha256"] == second["output_sha256"]


@pytest.mark.asyncio
async def test_modified_path_relative_and_absolute(env):
    def relative(document):
        (document.workdir / "out.txt").write_bytes(b"one")
        return PreProcessResult(PreProcessStatus.MODIFIED, path="out.txt")

    def absolute(document):
        out = document.workdir / "out2.txt"
        out.write_bytes(document.read_bytes() + b" two")
        return PreProcessResult(PreProcessStatus.MODIFIED, path=out)

    run = _run(_step(relative, "rel"), _step(absolute, "abs", index=1))
    await _write(run, b"zero")
    assert await _read() == b"one two"
    assert run.modified == [{"uri": URI, "method": "tests:abs", "message": None}]


@pytest.mark.asyncio
async def test_modified_with_identical_content_is_continue(env):
    def same(document):
        return PreProcessResult(PreProcessStatus.MODIFIED, data=document.read_bytes())

    run = _run(_step(same, "same"))
    await _write(run, b"hello")
    assert await _read() == b"hello"
    assert run.modified == []
    (row,) = local_state.get_pre_process_steps(SOURCE, URI)
    assert row["status"] == "continue"


@pytest.mark.asyncio
async def test_modified_back_to_the_original_is_not_modified(env):
    def change(document):
        return PreProcessResult(PreProcessStatus.MODIFIED, data=b"changed")

    def revert(document):
        return PreProcessResult(PreProcessStatus.MODIFIED, data=b"hello")

    run = _run(_step(change, "change"), _step(revert, "revert", index=1))
    await _write(run, b"hello")
    assert await _read() == b"hello"
    assert run.modified == []
    assert local_state.get_pre_process_document(SOURCE, URI)["status"] == "continue"


@pytest.mark.asyncio
async def test_metadata_is_namespaced_in_the_sidecar(env):
    def tag(document):
        return PreProcessResult(PreProcessStatus.CONTINUE, metadata={"pages": 3})

    run = _run(_step(tag, "tag"))
    await _write(run, metadata={"team": "docs"})
    meta = await _meta()
    assert meta["metadata"] == {"team": "docs", "pre_process": {"tests:tag": {"pages": 3}}}


# --- invalid answers (handled by on_error) -------------------------------------


@pytest.mark.parametrize(
    "answer, error",
    [
        (lambda d: (PreProcessStatus.MODIFIED, "no data"), "exactly one of data or path"),
        (lambda d: PreProcessResult(PreProcessStatus.MODIFIED, data=b"x", path="y"), "exactly one of data or path"),
        (lambda d: PreProcessResult(PreProcessStatus.MODIFIED, data="text"), "data must be bytes"),
        (lambda d: PreProcessResult(PreProcessStatus.MODIFIED, path="missing.txt"), "is not a file"),
        (lambda d: PreProcessResult(PreProcessStatus.CONTINUE, data=b"x"), "continue may not return data"),
        (lambda d: PreProcessResult(PreProcessStatus.CONTINUE, metadata=["x"]), "metadata must be a dict"),
        (lambda d: 42, "unsupported step return type"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_answers_are_errors(env, answer, error):
    run = _run(_step(answer, "bad"))
    await _write(run, b"hello")
    assert await _read() == b"hello"
    (item,) = run.errors
    assert error in item["message"]


# --- on_error ------------------------------------------------------------------


def _boom(document):
    raise ValueError("cannot parse")


@pytest.mark.asyncio
async def test_on_error_continue_keeps_the_content_before_the_failing_step(env, caplog):
    def upper(document):
        return PreProcessResult(PreProcessStatus.MODIFIED, "up", data=document.read_bytes().upper())

    ran = []

    def after(document):
        ran.append(document.read_bytes())

    run = _run(_step(upper, "upper"), _step(_boom, "boom", index=1), _step(after, "after", index=2))
    with caplog.at_level(logging.WARNING, logger="soliplex.agents.manifest.pre_process"):
        await _write(run, b"hello")

    assert await _read() == b"HELLO"
    assert ran == [b"HELLO"]
    assert run.errors == [{"uri": URI, "method": "tests:boom", "message": "ValueError: cannot parse"}]
    assert run.modified == [{"uri": URI, "method": "tests:upper", "message": "up"}]
    assert "pre-process tests:boom failed on docs/note.txt" in caplog.text
    audit = local_state.get_pre_process_document(SOURCE, URI)
    assert (audit["status"], audit["method"], audit["message"]) == ("error", "tests:boom", "ValueError: cannot parse")
    assert [r["status"] for r in local_state.get_pre_process_steps(SOURCE, URI)] == ["modified", "error", "continue"]


@pytest.mark.asyncio
async def test_on_error_skip(env):
    run = _run(_step(_boom, "boom", on_error="skip"))
    await _write(run, b"hello")
    assert not await _store().exists(KEY)
    assert run.skipped == [{"uri": URI, "method": "tests:boom", "message": "ValueError: cannot parse"}]
    assert run.errors == []


@pytest.mark.asyncio
async def test_on_error_fail_raises_and_stores_nothing(env):
    run = _run(_step(_boom, "boom", on_error="fail"))
    with pytest.raises(PreProcessFailed, match="pre-process tests:boom failed on docs/note.txt"):
        await _write(run, b"hello")
    assert not await _store().exists(KEY)
    assert run.errors == [{"uri": URI, "method": "tests:boom", "message": "ValueError: cannot parse"}]
    audit = local_state.get_pre_process_document(SOURCE, URI)
    assert (audit["status"], audit["output_sha256"]) == ("error", None)
    assert _spool_is_empty(env)


# --- change tracking -----------------------------------------------------------


@pytest.mark.asyncio
async def test_change_tracking(env, caplog, monkeypatch):
    times = iter(["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"])
    monkeypatch.setattr(pre_process, "_utcnow", lambda: next(times))
    run = _run(_step(lambda d: None, "noop"))

    await _write(run, b"v1")
    first = local_state.get_pre_process_document(SOURCE, URI)
    assert first["hash_changed_at"] == "2026-01-01T00:00:00+00:00"
    assert first["previous_input_sha256"] is None

    await _write(run, b"v1")
    same = local_state.get_pre_process_document(SOURCE, URI)
    assert same["run_at"] == "2026-01-02T00:00:00+00:00"
    assert same["hash_changed_at"] == "2026-01-01T00:00:00+00:00"
    assert same["previous_input_sha256"] is None

    with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.pre_process"):
        await _write(run, b"v2")
    changed = local_state.get_pre_process_document(SOURCE, URI)
    assert changed["hash_changed_at"] == "2026-01-03T00:00:00+00:00"
    assert changed["previous_input_sha256"] == first["input_sha256"]
    assert f"content of {URI} changed ({first['input_sha256'][:12]} -> {changed['input_sha256'][:12]})" in caplog.text


# --- serialization -------------------------------------------------------------


@pytest.mark.asyncio
async def test_steps_are_serialized_under_concurrent_writes(env):
    active = 0
    peak = 0

    async def slow(document):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    run = _run(_step(slow, "slow"))
    with pre_process.activate(run):
        await asyncio.gather(
            *(local_store.write_document(SOURCE, f"doc{i}.txt", b"x%d" % i, MIME) for i in range(5)),
        )
    assert peak == 1
    assert run.checked == 5
    for i in range(5):
        assert await _store().exists(f"doc{i}.txt")


@pytest.mark.asyncio
async def test_first_error_is_the_document_outcome(env):
    def other(document):
        raise KeyError("second")

    run = _run(_step(_boom, "boom"), _step(other, "other", index=1))
    await _write(run, b"hello")
    assert await _read() == b"hello"
    assert [e["method"] for e in run.errors] == ["tests:boom", "tests:other"]
    audit = local_state.get_pre_process_document(SOURCE, URI)
    assert (audit["status"], audit["method"]) == ("error", "tests:boom")
