"""Tests for the manifest ScheduleRegistry."""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from soliplex.agents.config import Manifest
from soliplex.agents.manifest.schedule_registry import ScheduleRegistry

_LOGGER = "soliplex.agents.manifest.schedule_registry"


def _manifest(mid: str, cron: str | None = None) -> Manifest:
    data: dict = {
        "id": mid,
        "name": f"Manifest {mid}",
        "source": f"src-{mid}",
        "components": [{"type": "fs", "name": "comp", "path": "/data"}],
    }
    if cron is not None:
        data["schedule"] = {"cron": cron}
    return Manifest(**data)


def _pairs(*specs: tuple[str, str | None]) -> list[tuple[Manifest, str]]:
    return [(_manifest(mid, cron), f"/manifests/{mid}.yml") for mid, cron in specs]


def test_new_scheduled_registers_but_not_due():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)

    res = reg.reconcile(_pairs(("a", "*/5 * * * *")), now)

    assert [e.manifest_id for e in res.added] == ["a"]
    assert res.to_run == []
    entry = reg._entries["a"]
    assert entry.cron_expr == "*/5 * * * *"
    assert entry.next_run == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def test_new_unscheduled_runs_once():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    res = reg.reconcile(_pairs(("u", None)), now)
    assert [e.manifest_id for e in res.added] == ["u"]
    assert [e.manifest_id for e in res.to_run] == ["u"]
    assert reg._entries["u"].next_run is None

    # A second reconcile must not run it again.
    res2 = reg.reconcile(_pairs(("u", None)), now + timedelta(minutes=1))
    assert res2.added == []
    assert res2.to_run == []
    assert res2.rescheduled == []


def test_invalid_cron_is_not_scheduled(caplog):
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        res = reg.reconcile(_pairs(("bad", "not a cron")), now)

    assert [e.manifest_id for e in res.added] == ["bad"]
    assert res.to_run == []
    assert reg._entries["bad"].next_run is None
    assert "Invalid cron expression" in caplog.text


def test_removed_manifest_dropped():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg.reconcile(_pairs(("a", "0 0 * * *"), ("b", None)), now)

    res = reg.reconcile(_pairs(("a", "0 0 * * *")), now + timedelta(minutes=1))

    assert res.removed == ["b"]
    assert "b" not in reg._entries


def test_cron_change_reschedules():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    reg.reconcile(_pairs(("a", "0 0 * * *")), now)

    res = reg.reconcile(_pairs(("a", "*/5 * * * *")), now)

    assert [e.manifest_id for e in res.rescheduled] == ["a"]
    assert reg._entries["a"].cron_expr == "*/5 * * * *"
    assert reg._entries["a"].next_run == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def test_schedule_added_to_unscheduled():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    reg.reconcile(_pairs(("a", None)), now)

    res = reg.reconcile(_pairs(("a", "*/5 * * * *")), now)

    assert [e.manifest_id for e in res.rescheduled] == ["a"]
    assert reg._entries["a"].next_run == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    assert res.to_run == []  # no longer a one-time unscheduled run


def test_schedule_removed_from_scheduled():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg.reconcile(_pairs(("a", "0 0 * * *")), now)

    res = reg.reconcile(_pairs(("a", None)), now)

    assert [e.manifest_id for e in res.rescheduled] == ["a"]
    assert reg._entries["a"].cron_expr is None
    assert reg._entries["a"].next_run is None
    assert res.to_run == []  # removing a schedule does not trigger a run


def test_due_manifest_fires_and_advances():
    reg = ScheduleRegistry()
    now1 = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    reg.reconcile(_pairs(("a", "*/5 * * * *")), now1)  # next_run 00:05

    now2 = datetime(2026, 1, 1, 0, 5, 10, tzinfo=UTC)
    res = reg.reconcile(_pairs(("a", "*/5 * * * *")), now2)

    assert [e.manifest_id for e in res.to_run] == ["a"]
    # Advanced to the next future occurrence (no catch-up storm).
    assert reg._entries["a"].next_run == datetime(2026, 1, 1, 0, 10, tzinfo=UTC)


def test_scheduled_not_due_does_not_run():
    reg = ScheduleRegistry()
    now1 = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    reg.reconcile(_pairs(("a", "*/5 * * * *")), now1)  # next_run 00:05

    now2 = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)  # before 00:05
    res = reg.reconcile(_pairs(("a", "*/5 * * * *")), now2)

    assert res.to_run == []


def test_existing_path_is_updated():
    reg = ScheduleRegistry()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg.reconcile([(_manifest("a", None), "/old/a.yml")], now)

    reg.reconcile([(_manifest("a", None), "/new/a.yml")], now)

    assert reg._entries["a"].path == "/new/a.yml"
