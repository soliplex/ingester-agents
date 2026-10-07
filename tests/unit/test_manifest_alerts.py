"""Where operator alerts are raised: the CLI runner, the server queues, the
scheduler and the maintenance verbs (the alert records themselves are
covered in ``test_alerts.py``)."""

import logging
import signal
import textwrap
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

import soliplex.agents.server as server
from soliplex.agents import alerts
from soliplex.agents.config import Manifest
from soliplex.agents.manifest import haiku_maint
from soliplex.agents.manifest import runner
from soliplex.agents.manifest.schedule_registry import ScheduleRegistry
from soliplex.agents.server import haiku_queue
from soliplex.agents.server import manifest_queue


@pytest.fixture
def alert_records(caplog):
    """The records on the alert logger, as ``(alert, stage or outcome, record)``."""
    caplog.set_level(logging.INFO, logger=alerts.LOGGER_NAME)

    def _get():
        return [
            (r.alert, getattr(r, "stage", None) or getattr(r, "outcome", None), r)
            for r in caplog.records
            if r.name == alerts.LOGGER_NAME
        ]

    return _get


def _write(tmp_path, name="m.yml", mid="m", source="src"):
    path = tmp_path / name
    path.write_text(
        textwrap.dedent(f"""\
        id: {mid}
        name: Manifest {mid}
        source: {source}
        components:
          - type: fs
            name: c
            path: /data
        """),
        encoding="utf-8",
    )
    return path


def _manifest(path="/m/m.yml", mid="m", source="src"):
    manifest = Manifest(id=mid, name="M", source=source, components=[{"type": "fs", "name": "c", "path": "/data"}])
    manifest.manifest_path = path
    return manifest


def _clean_run(**extra):
    return {"manifest_id": "m", "results": [], "summary": {"ingested": 2}, **extra}


def _load(**extra):
    return {"returncode": 0, "timed_out": False, "post_process": [], **extra}


# --- load_manifest ---


def test_load_manifest_records_its_path(tmp_path):
    path = _write(tmp_path)
    manifest = runner.load_manifest(str(path))
    assert manifest.manifest_path == str(path.resolve())
    assert "manifest_path" not in manifest.model_dump()


# --- run_failures / load_failures ---


class TestRunFailures:
    def test_clean(self):
        assert runner.run_failures(_clean_run()) == (None, [])

    def test_the_run_raised(self):
        assert runner.run_failures({"error": "boom"}) == (alerts.Stage.RUN, ["manifest: boom"])

    def test_names_components_and_samples_failing_uris(self):
        errors = [{"uri": f"u{i}"} for i in range(7)] + [{"path": "/listing"}, {}]
        result = {
            "results": [
                {"component": "a", "error": "RuntimeError: x"},
                {"component": "b", "result": {"errors": errors}},
                {"component": "c", "result": {"errors": [{"uri": "only"}]}},
                {"component": "d", "result": {}},
            ],
            "summary": {"component_errors": 1, "file_errors": 10},
        }
        assert runner.run_failures(result) == (
            alerts.Stage.COMPONENTS,
            [
                "component a: RuntimeError: x",
                "component b: 9 file errors (u0, u1, u2, u3, u4 and 4 more)",
                "component c: 1 file errors (only)",
            ],
        )

    def test_falls_back_to_the_summary_counts(self):
        result = {"results": [], "summary": {"file_errors": 2}}
        assert runner.run_failures(result) == (alerts.Stage.COMPONENTS, ["file_errors=2"])


class TestLoadFailures:
    @pytest.mark.parametrize("load", [None, {}, {"skipped": {"reason": "empty"}}, _load()])
    def test_no_failure(self, load):
        assert runner.load_failures(load) == (None, [])

    def test_raised(self):
        assert runner.load_failures(None, "OSError: x") == (alerts.Stage.HAIKU_LOAD, ["haiku load: OSError: x"])

    def test_timed_out(self):
        assert runner.load_failures(_load(returncode=None, timed_out=True)) == (
            alerts.Stage.HAIKU_LOAD,
            ["haiku load: timed out"],
        )

    def test_rc_and_post_process(self):
        load = _load(returncode=2, post_process_error="post-process a failed: X")
        assert runner.load_failures(load) == (
            alerts.Stage.HAIKU_LOAD,
            ["haiku load: rc=2", "post-process a failed: X"],
        )

    def test_post_process_alone(self):
        assert runner.load_failures(_load(post_process_error="pp")) == (alerts.Stage.POST_PROCESS, ["pp"])

    def test_killed_by_a_signal(self):
        rc = -int(signal.SIGTERM)
        assert runner.load_failures(_load(returncode=rc)) == (
            alerts.Stage.HAIKU_LOAD,
            [f"haiku load: killed by SIGTERM (rc={rc})"],
        )


# --- report_outcome ---


class TestReportOutcome:
    def test_a_failure_lists_run_and_load_reasons(self, alert_records):
        result = _clean_run(
            results=[{"component": "a", "error": "x"}],
            summary={"component_errors": 1},
            haiku_load=_load(returncode=1),
        )
        runner.report_outcome(_manifest(), result)
        ((alert, stage, record),) = alert_records()
        assert (alert, stage) == ("manifest_failed", "components")
        assert record.reasons == ["component a: x", "haiku load: rc=1"]
        assert record.manifest_source == "src"

    def test_a_load_failure_alone(self, alert_records):
        runner.report_outcome(_manifest(), _clean_run(haiku_load_error="boom"))
        ((alert, stage, _),) = alert_records()
        assert (alert, stage) == ("manifest_failed", "haiku_load")

    @pytest.mark.parametrize(
        "extra, outcome, note",
        [
            pytest.param({"haiku_load": _load(post_process=[{}])}, "ok", None, id="ok"),
            pytest.param({}, "no_load", None, id="no-load"),
            pytest.param({"skipped": {"method": "gate", "message": "busy"}}, "skipped", "gate: busy", id="skipped"),
            pytest.param({"skipped": {"method": "gate", "message": None}}, "skipped", "gate", id="skipped-bare"),
            pytest.param(
                {"haiku_load": {"skipped": {"reason": "no documents"}}},
                "load_skipped_empty",
                "no documents",
                id="empty",
            ),
        ],
    )
    def test_completions(self, alert_records, extra, outcome, note):
        runner.report_outcome(_manifest(), _clean_run(**extra))
        ((alert, got, record),) = alert_records()
        assert (alert, got, record.note) == ("manifest_completed", outcome, note)
        assert record.counts["ingested"] == 2


# --- the CLI runner ---


class TestRunManifests:
    @pytest.mark.asyncio
    async def test_one_alert_per_manifest(self, tmp_path, alert_records):
        a = _write(tmp_path, "a.yml", "a")
        _write(tmp_path, "b.yml", "b")
        _write(tmp_path, "c.yml", "c")
        runs = [
            RuntimeError("boom"),
            _clean_run(manifest_id="b"),
            _clean_run(manifest_id="c", results=[{"component": "c", "error": "x"}], summary={"component_errors": 1}),
        ]
        with patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock, side_effect=runs):
            await runner.run_manifests(str(tmp_path))
        records = alert_records()
        assert [(alert, stage) for alert, stage, _ in records] == [
            ("manifest_failed", "run"),
            ("manifest_completed", "no_load"),
            ("manifest_failed", "components"),
        ]
        assert records[0][2].reasons == ["RuntimeError: boom"]
        assert records[0][2].manifest_path == str(a.resolve())

    @pytest.mark.asyncio
    async def test_a_skipped_run_is_not_loaded_and_completes(self, tmp_path, alert_records):
        path = _write(tmp_path)
        skipped = _clean_run(skipped={"method": "gate", "message": None})
        with (
            patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock, return_value=skipped),
            patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock) as mock_load,
        ):
            await runner.run_manifests(str(path), load=True)
        mock_load.assert_not_awaited()
        assert [(a, s) for a, s, _ in alert_records()] == [("manifest_completed", "skipped")]

    @pytest.mark.asyncio
    async def test_a_loaded_run_completes_after_its_load(self, tmp_path, alert_records):
        path = _write(tmp_path)
        with (
            patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock, return_value=_clean_run()),
            patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock, return_value=_load()),
        ):
            await runner.run_manifests(str(path), load=True)
        assert [(a, s) for a, s, _ in alert_records()] == [("manifest_completed", "ok")]


class TestResolveManifestsAlerts:
    def test_invalid_file_in_a_directory(self, tmp_path, alert_records, caplog):
        _write(tmp_path, "good.yml", "good")
        bad = tmp_path / "bad.yml"
        bad.write_text(":::invalid:::", encoding="utf-8")
        manifests = runner.resolve_manifests(str(tmp_path), alert_invalid=True)
        assert [m.id for m in manifests] == ["good"]
        ((alert, stage, record),) = alert_records()
        assert (alert, stage) == ("manifest_failed", "manifest_file")
        assert record.manifest_path == str(bad)
        assert record.reasons[0].startswith("invalid manifest: ")
        assert "Skipping invalid manifest" not in caplog.text

    def test_without_alerting_only_warns(self, tmp_path, alert_records, caplog):
        (tmp_path / "bad.yml").write_text(":::invalid:::", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert runner.resolve_manifests(str(tmp_path)) == []
        assert alert_records() == []
        assert "Skipping invalid manifest" in caplog.text

    def test_duplicate_ids_alert_on_each_file(self, tmp_path, alert_records):
        a = _write(tmp_path, "a.yml", "dup")
        b = _write(tmp_path, "b.yml", "dup")
        with pytest.raises(ValueError, match="Duplicate manifest IDs"):
            runner.resolve_manifests(str(tmp_path), alert_invalid=True)
        records = [r for _, _, r in alert_records()]
        assert [r.manifest_path for r in records] == [str(a), str(b)]
        assert records[0].reasons == [f"duplicate manifest id, also declared in {b}"]

    def test_duplicate_ids_without_alerting(self, tmp_path, alert_records):
        _write(tmp_path, "a.yml", "dup")
        _write(tmp_path, "b.yml", "dup")
        with pytest.raises(ValueError, match="Duplicate manifest IDs"):
            runner.resolve_manifests(str(tmp_path))
        assert alert_records() == []

    @pytest.mark.parametrize("alert_invalid", [True, False])
    def test_an_invalid_single_file(self, tmp_path, alert_records, alert_invalid):
        bad = tmp_path / "bad.yml"
        bad.write_text("id: x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="validation error"):
            runner.resolve_manifests(str(bad), alert_invalid=alert_invalid)
        assert len(alert_records()) == (1 if alert_invalid else 0)


# --- the server's manifest queue ---


class TestManifestQueue:
    @pytest.mark.asyncio
    async def test_a_file_that_no_longer_loads(self, tmp_path, alert_records):
        bad = tmp_path / "m.yml"
        bad.write_text(":::invalid:::", encoding="utf-8")
        with pytest.raises(ValueError, match="validation error"):
            await manifest_queue.run_manifest_now("m", str(bad))
        ((alert, stage, record),) = alert_records()
        assert (alert, stage, record.manifest_id) == ("manifest_failed", "manifest_file", "m")
        assert record.manifest_path == str(bad)

    @pytest.mark.asyncio
    async def test_a_run_that_raises(self, tmp_path, alert_records):
        path = _write(tmp_path)
        with (
            patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock, side_effect=OSError("x")),
            pytest.raises(OSError, match="x"),
        ):
            await manifest_queue.run_manifest_now("m", str(path))
        ((alert, stage, record),) = alert_records()
        assert (alert, stage, record.reasons) == ("manifest_failed", "run", ["OSError: x"])

    @pytest.mark.parametrize(
        "run, load_enabled, load_on_error, expected, queued",
        [
            pytest.param(
                _clean_run(skipped={"method": "gate"}),
                True,
                False,
                [("manifest_completed", "skipped")],
                False,
                id="skipped",
            ),
            pytest.param(_clean_run(), False, False, [("manifest_completed", "no_load")], False, id="no-load"),
            pytest.param(_clean_run(), True, False, [], True, id="load-queued"),
            pytest.param(
                _clean_run(summary={"file_errors": 1}),
                True,
                False,
                [("manifest_failed", "components")],
                False,
                id="load-blocked",
            ),
            pytest.param(
                _clean_run(summary={"file_errors": 1}),
                True,
                True,
                [("manifest_failed", "components")],
                True,
                id="load-on-error",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_run_outcomes(self, tmp_path, alert_records, run, load_enabled, load_on_error, expected, queued):
        path = _write(tmp_path)
        with (
            patch("soliplex.agents.server.manifest_queue.settings") as ms,
            patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock, return_value=run),
            patch("soliplex.agents.server.manifest_queue.enqueue_load", new_callable=AsyncMock) as mock_enqueue,
        ):
            ms.haiku_load_enabled = load_enabled
            ms.haiku_load_on_error = load_on_error
            await manifest_queue.run_manifest_now("m", str(path))
        assert [(a, s) for a, s, _ in alert_records()] == expected
        assert mock_enqueue.await_count == (1 if queued else 0)


# --- the server's haiku load queue ---


class TestHaikuQueue:
    def test_a_clean_load_completes_the_manifest(self, alert_records):
        haiku_queue._report_outcome(_manifest(), _clean_run(), _load(post_process=[{}]))
        ((alert, outcome, record),) = alert_records()
        assert (alert, outcome) == ("manifest_completed", "ok")
        assert record.counts["post_process_steps"] == 1

    def test_a_load_without_a_run_result(self, alert_records):
        haiku_queue._report_outcome(_manifest(), None, {"skipped": {"reason": "no documents"}})
        assert [(a, s) for a, s, _ in alert_records()] == [("manifest_completed", "load_skipped_empty")]

    def test_a_failed_load(self, alert_records):
        haiku_queue._report_outcome(_manifest(), _clean_run(), _load(post_process_error="pp"))
        ((alert, stage, record),) = alert_records()
        assert (alert, stage, record.reasons) == ("manifest_failed", "post_process", ["pp"])

    def test_a_clean_load_after_a_failed_run_says_nothing(self, alert_records):
        failed = _clean_run(summary={"file_errors": 1})
        haiku_queue._report_outcome(_manifest(), failed, _load())
        assert alert_records() == []

    @pytest.mark.asyncio
    async def test_a_load_that_raises(self, alert_records):
        haiku_queue.start_worker()
        try:
            with patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock, side_effect=OSError("gone")):
                await haiku_queue.enqueue_load(_manifest())
                await haiku_queue._queue.join()
        finally:
            await haiku_queue.stop_worker()
        ((alert, stage, record),) = alert_records()
        assert (alert, stage, record.reasons) == ("manifest_failed", "haiku_load", ["haiku load: OSError: gone"])


# --- the scheduler ---


class TestScheduler:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        server._schedule_registry = ScheduleRegistry()
        server._reconcile_log = server._ReconcileLog()
        yield
        server._schedule_registry = ScheduleRegistry()
        server._reconcile_log = server._ReconcileLog()

    async def _reconcile(self, tmp_path, times=1):
        with (
            patch("soliplex.agents.server.settings") as ms,
            patch("soliplex.agents.server.manifest_queue.enqueue_manifest", new_callable=AsyncMock),
        ):
            ms.manifest_dir = str(tmp_path)
            for _ in range(times):
                await server.reconcile_manifest_schedules()

    @pytest.mark.asyncio
    async def test_an_invalid_file_alerts_once(self, tmp_path, alert_records):
        bad = tmp_path / "bad.yml"
        bad.write_text(":::invalid:::", encoding="utf-8")
        await self._reconcile(tmp_path, times=3)
        ((alert, stage, record),) = alert_records()
        assert (alert, stage, record.manifest_path) == ("manifest_failed", "manifest_file", str(bad))
        assert record.manifest_id == "unknown"

    @pytest.mark.asyncio
    async def test_a_registered_manifest_that_breaks_alerts_with_its_id(self, tmp_path, alert_records):
        path = _write(tmp_path, "a.yml", "a")
        await self._reconcile(tmp_path)
        path.write_text("id: a\n", encoding="utf-8")
        await self._reconcile(tmp_path)
        ((_, _, record),) = alert_records()
        assert record.manifest_id == "a"

    @pytest.mark.asyncio
    async def test_duplicate_ids_alert_once_per_file(self, tmp_path, alert_records):
        _write(tmp_path, "a.yml", "dup")
        _write(tmp_path, "b.yml", "dup")
        await self._reconcile(tmp_path, times=2)
        assert [r.manifest_id for _, _, r in alert_records()] == ["dup", "dup"]


# --- maintenance verbs ---


class TestMaintenance:
    @pytest.mark.parametrize(
        "result, reason",
        [
            ({"error": "no config"}, "haiku vacuum: no config"),
            ({"skipped": "duplicate-db"}, None),
            ({"timed_out": True, "timeout": 30, "returncode": None}, "haiku vacuum: timed out after 30s"),
            ({"timed_out": False, "returncode": 3}, "haiku vacuum: rc=3"),
            ({"timed_out": False, "returncode": 0}, None),
        ],
    )
    def test_maintenance_failure(self, result, reason):
        assert haiku_maint.maintenance_failure("vacuum", result) == reason

    @pytest.mark.parametrize("verb, stage", [("migrate", "migrate"), ("vacuum", "vacuum")])
    @pytest.mark.asyncio
    async def test_failed_targets_alert_once_the_verb_finishes(self, alert_records, verb, stage):
        manifests = [_manifest(mid="a", source="a"), _manifest(mid="b", source="b"), _manifest(mid="c", source="c")]
        outcomes = {
            "a": {"timed_out": False, "returncode": 0},
            "b": {"timed_out": False, "returncode": 1},
            "c": RuntimeError("missing haiku-rag"),
        }

        async def fake_run_verb(source, verb, **kwargs):
            outcome = outcomes[source]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch.object(runner, "resolve_manifests", return_value=manifests),
            patch.object(haiku_maint, "resolve_haiku_cfg", return_value="/h.yaml"),
            patch.object(haiku_maint, "resolve_db_path", side_effect=lambda source: f"/db/{source}"),
            patch.object(haiku_maint, "run_verb", side_effect=fake_run_verb),
        ):
            await haiku_maint.run_maintenance(verb, "all")
        records = alert_records()
        assert [(a, s, r.manifest_id) for a, s, r in records] == [
            ("manifest_failed", stage, "b"),
            ("manifest_failed", stage, "c"),
        ]
        assert records[0][2].reasons == [f"haiku {verb}: rc=1"]
        assert records[1][2].reasons == [f"haiku {verb}: missing haiku-rag"]

    @pytest.mark.asyncio
    async def test_dry_run_and_backfill_do_not_alert(self, alert_records):
        failing = AsyncMock(return_value={"timed_out": False, "returncode": 1})
        with (
            patch.object(runner, "resolve_manifests", return_value=[_manifest()]),
            patch.object(haiku_maint, "resolve_haiku_cfg", return_value="/h.yaml"),
            patch.object(haiku_maint, "resolve_db_path", return_value="/db/src"),
            patch.object(haiku_maint, "run_verb", failing),
        ):
            await haiku_maint.run_maintenance("vacuum", "all", dry_run=True)
            await haiku_maint.run_maintenance(haiku_maint.BACKFILL_VERB, "all")
        assert alert_records() == []
