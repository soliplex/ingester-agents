"""Tests for the operator alert logger."""

import json
import logging
import os
import textwrap

import pytest

from soliplex.agents import alerts
from soliplex.agents import log_config
from soliplex.agents.config import JsonFormatter


@pytest.fixture
def records(caplog):
    """The records the alert logger emits, at every level."""
    caplog.set_level(logging.DEBUG, logger=alerts.LOGGER_NAME)
    return lambda: [r for r in caplog.records if r.name == alerts.LOGGER_NAME]


class TestManifestFailed:
    def test_message_and_fields(self, records, tmp_path):
        path = tmp_path / "docs.yml"
        alerts.manifest_failed(
            manifest_id="docs",
            path=str(path),
            stage=alerts.Stage.POST_PROCESS,
            reasons=["post-process a failed: X", "haiku load: rc=1"],
            source="src",
        )
        (record,) = records()
        assert record.levelno == logging.ERROR
        assert record.exc_info is None
        assert record.getMessage() == (
            f"Manifest 'docs' ({path}) needs attention: post_process: post-process a failed: X; haiku load: rc=1"
        )
        assert record.alert == "manifest_failed"
        assert record.manifest_id == "docs"
        assert record.manifest_path == str(path)
        assert record.manifest_source == "src"
        assert record.stage == "post_process"
        assert record.reasons == ["post-process a failed: X", "haiku load: rc=1"]

    def test_relative_path_is_made_absolute(self, records):
        alerts.manifest_failed(manifest_id="m", path="m.yml", stage=alerts.Stage.RUN, reasons=["boom"])
        (record,) = records()
        assert record.manifest_path == os.path.abspath("m.yml")

    def test_unknown_id_and_path(self, records):
        alerts.manifest_failed(manifest_id=None, path=None, stage=alerts.Stage.MANIFEST_FILE, reasons=["bad"])
        (record,) = records()
        assert record.getMessage() == "Manifest 'unknown' (unknown) needs attention: manifest_file: bad"
        assert record.manifest_source is None


class TestManifestCompleted:
    def test_message_and_fields(self, records, tmp_path):
        path = tmp_path / "docs.yml"
        counts = {"ingested": 239, "deleted": 4, "not_found": 0, "post_process_steps": 2}
        alerts.manifest_completed(manifest_id="docs", path=str(path), source="src", outcome=alerts.Outcome.OK, counts=counts)
        (record,) = records()
        assert record.levelno == logging.INFO
        # Zero counts are left out of the message, but kept in the field.
        assert record.getMessage() == (
            f"Manifest 'docs' ({path}) completed: 239 ingested, 4 deleted, 2 post-process steps, load ok"
        )
        assert record.alert == "manifest_completed"
        assert record.manifest_id == "docs"
        assert record.manifest_path == str(path)
        assert record.manifest_source == "src"
        assert record.outcome == "ok"
        assert record.counts == counts
        assert record.note is None

    @pytest.mark.parametrize(
        "outcome, text",
        [
            (alerts.Outcome.NO_LOAD, "no load"),
            (alerts.Outcome.SKIPPED, "skipped by pre-run"),
            (alerts.Outcome.LOAD_SKIPPED_EMPTY, "load skipped"),
        ],
    )
    def test_outcomes_with_a_note(self, records, outcome, text):
        alerts.manifest_completed(manifest_id="m", path=None, source="s", outcome=outcome, counts={"ingested": 0}, note="why")
        (record,) = records()
        assert record.getMessage() == f"Manifest 'm' (unknown) completed: {text} (why)"
        assert record.outcome == str(outcome)
        assert record.note == "why"


def test_completion_counts():
    run = {
        "summary": {
            "ingested": 3,
            "deleted": 1,
            "not_found": 2,
            "pre_process_skipped": 4,
            "pre_process_modified": 5,
            "file_errors": 9,
        }
    }
    load = {"post_process": [{}, {}]}
    assert alerts.completion_counts(run, load) == {
        "ingested": 3,
        "deleted": 1,
        "not_found": 2,
        "pre_process_skipped": 4,
        "pre_process_modified": 5,
        "post_process_steps": 2,
    }


def test_completion_counts_without_a_run_or_load():
    assert set(alerts.completion_counts(None).values()) == {0}
    assert alerts.completion_counts({"summary": None}, {"post_process": None})["post_process_steps"] == 0


def test_json_formatter_writes_the_fields():
    record = logging.LogRecord(alerts.LOGGER_NAME, logging.ERROR, __file__, 1, "msg", (), None)
    record.__dict__.update({"alert": "manifest_failed", "manifest_path": "/m.yml", "reasons": ["a"]})
    obj = json.loads(JsonFormatter().format(record))
    assert obj["alert"] == "manifest_failed"
    assert obj["manifest_path"] == "/m.yml"
    assert obj["reasons"] == ["a"]


class _Capture(logging.Handler):
    """A handler a logging config file can name, which keeps what it receives."""

    received: list[logging.LogRecord] = []

    def emit(self, record):
        _Capture.received.append(record)


@pytest.fixture
def routed(tmp_path):
    """Apply a logging config that routes the alert logger to :class:`_Capture`."""

    def _apply(propagate: bool):
        config = tmp_path / "logging.yaml"
        config.write_text(
            textwrap.dedent(f"""\
            version: 1
            handlers:
              alerts:
                (): {__name__}._Capture
                level: ERROR
            loggers:
              {alerts.LOGGER_NAME}:
                level: INFO
                handlers: [alerts]
                propagate: {str(propagate).lower()}
            """),
            encoding="utf-8",
        )
        log_config.apply_file(str(config))

    _Capture.received = []
    yield _apply
    named = logging.getLogger(alerts.LOGGER_NAME)
    named.handlers.clear()
    named.setLevel(logging.NOTSET)
    named.propagate = True


@pytest.mark.parametrize("propagate", [True, False])
def test_routed_by_a_logging_config_file(routed, caplog, propagate):
    routed(propagate)
    with caplog.at_level(logging.INFO):
        alerts.manifest_failed(manifest_id="m", path="/m.yml", stage=alerts.Stage.RUN, reasons=["boom"])
        alerts.manifest_completed(
            manifest_id="m", path="/m.yml", source="s", outcome=alerts.Outcome.OK, counts={"ingested": 1}
        )
    # The handler's level keeps only failures; the root sees both unless cut off.
    assert [r.alert for r in _Capture.received] == ["manifest_failed"]
    seen_by_root = [r.alert for r in caplog.records if r.name == alerts.LOGGER_NAME]
    assert seen_by_root == (["manifest_failed", "manifest_completed"] if propagate else [])
