"""Tests for the manifest-hook audit tables in local_state -- 100% branch coverage required."""

import pytest

from soliplex.agents import local_state
from soliplex.agents.config import settings

SOURCE = "src"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "download_dir", str(tmp_path / "dl"))


def _doc(uri, status="continue", *, method=None, message=None, changed="2026-01-01T00:00:00+00:00"):
    return {
        "uri": uri,
        "input_sha256": f"in-{uri}",
        "output_sha256": None if status == "skip" else f"out-{uri}",
        "previous_input_sha256": None,
        "status": status,
        "method": method,
        "message": message,
        "hash_changed_at": changed,
        "run_at": changed,
    }


def _step(method, status="continue", step=0):
    return {
        "step": step,
        "method": method,
        "status": status,
        "message": None,
        "input_sha256": "a",
        "output_sha256": "a",
        "run_at": "2026-01-01T00:00:00+00:00",
    }


def _seed():
    for uri, status, method, message, changed in [
        ("a.pdf", "skip", "pdf:check", "password protected", "2026-01-01T00:00:00+00:00"),
        ("b.pdf", "continue", None, None, "2026-02-01T00:00:00+00:00"),
        ("c.adoc", "modified", "adoc:fix", "stripped 2", "2026-03-01T00:00:00+00:00"),
    ]:
        local_state.upsert_file(SOURCE, uri, f"up-{uri}")
        step_method = method or "pdf:check"
        local_state.record_pre_process(
            SOURCE,
            _doc(uri, status, method=method, message=message, changed=changed),
            [_step(step_method, status)],
        )


# --- record / get ---------------------------------------------------------------


def test_record_replaces_step_rows():
    local_state.record_pre_process(SOURCE, _doc("a"), [_step("one"), _step("two", step=1)])
    local_state.record_pre_process(SOURCE, _doc("a", "skip"), [_step("one", "skip")])
    assert local_state.get_pre_process_document(SOURCE, "a")["status"] == "skip"
    assert [r["method"] for r in local_state.get_pre_process_steps(SOURCE, "a")] == ["one"]
    assert local_state.get_pre_process_document(SOURCE, "missing") is None
    assert local_state.get_pre_process_steps(SOURCE, "missing") == []


# --- list_pre_process -------------------------------------------------------------


def test_list_pre_process_filters():
    _seed()
    assert [r["uri"] for r in local_state.list_pre_process(SOURCE)] == ["a.pdf", "b.pdf", "c.adoc"]
    assert [r["uri"] for r in local_state.list_pre_process(SOURCE, status="skip")] == ["a.pdf"]
    assert [r["uri"] for r in local_state.list_pre_process(SOURCE, message="password")] == ["a.pdf"]
    assert [r["uri"] for r in local_state.list_pre_process(SOURCE, changed_since="2026-02-01")] == ["b.pdf", "c.adoc"]
    assert local_state.list_pre_process(SOURCE, status="skip", message="stripped") == []


# --- cascade ------------------------------------------------------------------------


def test_delete_file_removes_the_audit():
    _seed()
    local_state.delete_file(SOURCE, "a.pdf")
    assert local_state.get_pre_process_document(SOURCE, "a.pdf") is None
    assert local_state.get_pre_process_steps(SOURCE, "a.pdf") == []
    assert local_state.get_pre_process_document(SOURCE, "b.pdf") is not None


def test_prune_files_removes_audit_of_pruned_and_unrecorded_documents():
    _seed()
    # A write that failed under on_error: fail has audit rows but no files row.
    local_state.record_pre_process(SOURCE, _doc("failed.pdf", "error"), [_step("pdf:check", "error")])
    removed = local_state.prune_files(SOURCE, {"b.pdf", "c.adoc"})
    assert removed == ["a.pdf"]
    assert [r["uri"] for r in local_state.list_pre_process(SOURCE)] == ["b.pdf", "c.adoc"]
    assert local_state.get_pre_process_steps(SOURCE, "failed.pdf") == []


def test_prune_files_with_nothing_removed_still_drops_orphans():
    _seed()
    local_state.record_pre_process(SOURCE, _doc("failed.pdf", "error"), [])
    assert local_state.prune_files(SOURCE, {"a.pdf", "b.pdf", "c.adoc"}) == []
    assert local_state.get_pre_process_document(SOURCE, "failed.pdf") is None


# --- reprocess ------------------------------------------------------------------------


def test_reprocess_by_status_forgets_files_and_clears_the_cursor():
    _seed()
    local_state.set_sync_meta(SOURCE, "abc123")
    assert local_state.reprocess(SOURCE, status="skip") == ["a.pdf"]
    assert "a.pdf" not in local_state.load_file_state(SOURCE)
    assert "b.pdf" in local_state.load_file_state(SOURCE)
    assert local_state.get_pre_process_document(SOURCE, "a.pdf") is None
    assert local_state.get_sync_meta(SOURCE)["last_commit_sha"] is None


def test_reprocess_by_method_and_any_status():
    _seed()
    assert local_state.reprocess(SOURCE, method="pdf:check", dry_run=True) == ["a.pdf", "b.pdf"]
    assert local_state.reprocess(SOURCE, method="adoc:fix", status="modified", dry_run=True) == ["c.adoc"]
    assert local_state.reprocess(SOURCE, dry_run=True) == ["a.pdf", "b.pdf", "c.adoc"]
    # Dry runs changed nothing.
    assert len(local_state.load_file_state(SOURCE)) == 3


def test_reprocess_nothing_selected():
    _seed()
    local_state.set_sync_meta(SOURCE, "abc123")
    assert local_state.reprocess(SOURCE, status="error") == []
    assert local_state.get_sync_meta(SOURCE)["last_commit_sha"] == "abc123"


# --- pre_run -----------------------------------------------------------------------------


def _run_rows(started, *statuses):
    return [
        {"started_at": started, "step": i, "method": f"m{i}", "status": s, "message": None, "duration_s": 0.1}
        for i, s in enumerate(statuses)
    ]


def test_record_pre_run_empty_is_a_no_op():
    local_state.record_pre_run(SOURCE, [])
    assert local_state.list_pre_run(SOURCE) == []


def test_pre_run_history_is_capped_and_filterable():
    for day in range(1, 6):
        status = "skip" if day == 3 else "continue"
        local_state.record_pre_run(SOURCE, _run_rows(f"2026-01-0{day}", "continue", status), keep=3)
    rows = local_state.list_pre_run(SOURCE)
    assert sorted({r["started_at"] for r in rows}, reverse=True) == ["2026-01-05", "2026-01-04", "2026-01-03"]
    assert [(r["started_at"], r["step"]) for r in rows[:2]] == [("2026-01-05", 0), ("2026-01-05", 1)]
    assert [r["started_at"] for r in local_state.list_pre_run(SOURCE, status="skip")] == ["2026-01-03"]
    assert {r["started_at"] for r in local_state.list_pre_run(SOURCE, since="2026-01-04")} == {"2026-01-04", "2026-01-05"}
