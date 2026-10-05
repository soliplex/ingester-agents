"""How run_manifest wires the pre-run and pre-process hooks -- 100% branch coverage required.

The end-to-end tests run a real fs component over real PDFs, on both download
backends: a step only runs when the manifest lists it, and a listed step
reaches the agent's writes.
"""

import shutil
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from soliplex.agents import local_state
from soliplex.agents import store as agent_store
from soliplex.agents.config import FSComponent
from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PreProcessStep
from soliplex.agents.config import PreRunStep
from soliplex.agents.config import settings
from soliplex.agents.manifest import pre_process
from soliplex.agents.manifest import runner
from soliplex.agents.manifest.pre_run import PreRunFailed

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pdf"
CHECK_PDF = "soliplex.agents.manifest.pre_processors:check_pdf_password"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "download_dir", str(tmp_path / "dl"))
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(settings, "pre_process_spool_dir", str(spool))
    agent_store.reset_store_cache()
    yield tmp_path
    agent_store.reset_store_cache()


def _manifest(tmp_path, config=None, source="docs"):
    return Manifest(
        id="docs",
        name="Docs",
        source=source,
        config=config,
        components=[{"type": "fs", "name": "files", "path": str(tmp_path / "upstream")}],
    )


def _upstream(tmp_path, *names):
    upstream = tmp_path / "upstream"
    upstream.mkdir(exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES / name, upstream / name)
    return upstream


# --- pre-run -------------------------------------------------------------------


def skip_step(context):
    return "skip", "maintenance window"


def boom_step(context):
    raise RuntimeError("nope")


@pytest.mark.asyncio
async def test_a_skipped_run_runs_nothing(env):
    handler = AsyncMock()
    config = ManifestConfig(pre_run=[PreRunStep(method=f"{__name__}:skip_step")])
    with patch.dict(runner._DISPATCH, {FSComponent: handler}):
        result = await runner.run_manifest(_manifest(env, config))

    handler.assert_not_called()
    assert result["skipped"] == {"method": f"{__name__}:skip_step", "message": "maintenance window"}
    assert result["pre_process"] is None
    assert result["results"] == []
    assert result["summary"] == {"components": 1, "skipped": True}
    assert [s["status"] for s in result["pre_run"]] == ["skip"]


@pytest.mark.asyncio
async def test_a_skipped_run_is_not_loaded(env, tmp_path):
    config = ManifestConfig(pre_run=[PreRunStep(method=f"{__name__}:skip_step")])
    path = tmp_path / "m.yml"
    path.write_text(_manifest(env, config).model_dump_json(), encoding="utf-8")
    with patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock) as run_load:
        (result,) = await runner.run_manifests(str(path), load=True)
    run_load.assert_not_called()
    assert result["skipped"]["method"].endswith(":skip_step")


@pytest.mark.asyncio
async def test_a_failing_pre_run_fails_the_manifest(env, tmp_path):
    config = ManifestConfig(pre_run=[PreRunStep(method=f"{__name__}:boom_step")])
    path = tmp_path / "m.yml"
    path.write_text(_manifest(env, config).model_dump_json(), encoding="utf-8")
    with pytest.raises(PreRunFailed):
        await runner.run_manifest(_manifest(env, config))
    (result,) = await runner.run_manifests(str(path))
    assert "pre-run" in result["error"]


@pytest.mark.asyncio
async def test_a_bad_pre_process_path_fails_before_any_pre_run_step(env):
    calls = []
    config = ManifestConfig(
        pre_run=[PreRunStep(method=f"{__name__}:skip_step")],
        pre_process=[PreProcessStep(method="no.such.module:fn")],
    )
    with patch("soliplex.agents.manifest.pre_run.run_pre_run", side_effect=lambda *a, **k: calls.append(1)):
        with pytest.raises(ModuleNotFoundError):
            await runner.run_manifest(_manifest(env, config))
    assert calls == []


@pytest.mark.asyncio
async def test_run_result_is_handed_to_the_load(env, tmp_path):
    path = tmp_path / "m.yml"
    path.write_text(_manifest(env, ManifestConfig(pre_process=[])).model_dump_json(), encoding="utf-8")
    _upstream(env)
    with patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock, return_value={}) as run_load:
        (result,) = await runner.run_manifests(str(path), load=True)
    run_result = run_load.await_args.kwargs["run_result"]
    assert run_result["manifest_id"] == "docs"
    assert run_result["skipped"] is None
    assert "haiku_load" not in run_result
    assert result["haiku_load"] == {}


def boom_document(document):
    raise RuntimeError("cannot process")


def _write_run(tmp_path, config):
    path = tmp_path / "m.yml"
    path.write_text(_manifest(tmp_path, config).model_dump_json(), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_a_pre_process_fail_blocks_the_load(env, monkeypatch):
    """``on_error: fail`` stops the write, which is a file error, which holds back the load."""
    monkeypatch.setattr(settings, "haiku_load_on_error", False)
    _upstream(env, "valid.pdf")
    path = _write_run(env, ManifestConfig(pre_process=[PreProcessStep(method=f"{__name__}:boom_document", on_error="fail")]))
    with patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock) as run_load:
        (result,) = await runner.run_manifests(str(path), load=True)
    run_load.assert_not_called()
    assert result["summary"]["file_errors"] == 1
    assert result["haiku_load_skipped"]["errors"] == {"file_errors": 1}


@pytest.mark.asyncio
async def test_a_pre_process_continue_error_does_not_block_the_load(env, monkeypatch):
    """``on_error: continue`` declared the failure tolerable: the document is stored and the load runs."""
    monkeypatch.setattr(settings, "haiku_load_on_error", False)
    _upstream(env, "valid.pdf")
    path = _write_run(
        env, ManifestConfig(pre_process=[PreProcessStep(method=f"{__name__}:boom_document", on_error="continue")])
    )
    with patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock, return_value={}) as run_load:
        (result,) = await runner.run_manifests(str(path), load=True)
    run_load.assert_awaited_once()
    assert result["summary"]["pre_process_errors"] == 1
    assert result["summary"]["file_errors"] == 0
    assert "haiku_load_skipped" not in result


@pytest.mark.asyncio
async def test_a_pre_run_continue_error_does_not_block_the_load(env, monkeypatch):
    monkeypatch.setattr(settings, "haiku_load_on_error", False)
    _upstream(env, "valid.pdf")
    # os.getcwd takes no context argument, so the step errors under `continue`.
    path = _write_run(env, ManifestConfig(pre_run=[PreRunStep(method="os:getcwd", on_error="continue")]))
    with patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock, return_value={}) as run_load:
        (result,) = await runner.run_manifests(str(path), load=True)
    run_load.assert_awaited_once()
    assert [s["status"] for s in result["pre_run"]] == ["error"]


@pytest.mark.asyncio
async def test_pre_process_is_cleared_after_the_run(env):
    _upstream(env)
    await runner.run_manifest(_manifest(env))
    assert pre_process.current() is None


# --- end to end: a listed step reaches the fs agent -------------------------------

PDF_CHECK = [PreProcessStep(method=CHECK_PDF, mime_types=["application/pdf"])]


@pytest.fixture(params=["local", "s3"])
def backend(request, env, monkeypatch, memory_store):
    monkeypatch.setattr(settings, "download_s3_bucket", "bucket" if request.param == "s3" else None)
    agent_store.reset_store_cache()
    return request.param


@pytest.mark.asyncio
async def test_without_pre_process_nothing_is_checked(env, backend):
    _upstream(env, "valid.pdf", "user_password.pdf")

    result = await runner.run_manifest(_manifest(env))

    store = agent_store.get_document_store("docs")
    assert await store.exists("user_password.pdf")
    assert result["pre_process"]["checked"] == 0
    assert local_state.get_pre_process_document("docs", "user_password.pdf") is None


@pytest.mark.asyncio
async def test_a_listed_step_skips_an_encrypted_pdf_end_to_end(env, backend):
    _upstream(env, "valid.pdf", "user_password.pdf", "owner_only.pdf")
    manifest = _manifest(env, ManifestConfig(pre_process=PDF_CHECK))

    result = await runner.run_manifest(manifest)

    store = agent_store.get_document_store("docs")
    assert await store.exists("valid.pdf")
    assert await store.exists("owner_only.pdf")
    assert not await store.exists("user_password.pdf")
    assert not await store.exists("user_password.pdf.meta.json")
    assert result["pre_process"]["checked"] == 3
    assert result["pre_process"]["skipped"] == [
        {"uri": "user_password.pdf", "method": CHECK_PDF, "message": "password protected"}
    ]
    summary = result["summary"]
    assert (summary["pre_process_checked"], summary["pre_process_skipped"], summary["pre_process_modified"]) == (3, 1, 0)
    # The skip has a state row, so the next run does not fetch it again ...
    assert "user_password.pdf" in local_state.load_file_state("docs")
    second = await runner.run_manifest(manifest)
    assert second["pre_process"]["checked"] == 0
    # ... and survives the reconcile, which only deletes what it did not expect.
    assert local_state.get_pre_process_document("docs", "user_password.pdf")["status"] == "skip"


@pytest.mark.asyncio
async def test_reports_and_reprocess(env):
    _upstream(env, "valid.pdf", "user_password.pdf")
    manifest = _manifest(
        env,
        ManifestConfig(pre_run=[PreRunStep(method="os:getcwd", on_error="continue")], pre_process=PDF_CHECK),
    )
    await runner.run_manifest(manifest)

    skipped = runner.pre_process_report(manifest, status="skip")
    assert [r["uri"] for r in skipped] == ["user_password.pdf"]
    assert runner.pre_process_report(manifest, message="password")[0]["message"] == "password protected"
    assert len(runner.pre_process_report(manifest, changed_since="2000-01-01")) == 2
    # os.getcwd takes no context argument, so the step errors -- and is recorded.
    assert [r["status"] for r in runner.pre_run_report(manifest)] == ["error"]
    assert runner.pre_run_report(manifest, status="skip") == []
    assert len(runner.pre_run_report(manifest, since="2000-01-01")) == 1

    dry = runner.reprocess(manifest, status="skip", dry_run=True)
    assert dry == {"manifest_id": "docs", "source": "docs", "uris": ["user_password.pdf"], "dry_run": True}
    assert runner.reprocess(manifest, status="skip")["uris"] == ["user_password.pdf"]

    # Forgotten, so the next run fetches and checks it again.
    again = await runner.run_manifest(manifest)
    assert again["pre_process"]["checked"] == 1
    assert [s["uri"] for s in again["pre_process"]["skipped"]] == ["user_password.pdf"]
