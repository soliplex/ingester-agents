"""Operator alerts: one record per manifest run on a dedicated logger.

Every manifest run ends in exactly one record on the
``soliplex.agents.alerts`` logger, so a deployment can route that logger to
a higher-priority sink (a pager, a chat webhook, an on-call mailbox) from its
``LOG_CONFIG_FILE`` without the detail logs coming along:

* :func:`manifest_failed` -- ERROR: the manifest failed and needs an
  operator. Carries the manifest's path and every reason it failed.
* :func:`manifest_completed` -- INFO: the manifest finished without a
  failure. Carries its counts and an :class:`Outcome`.

``manifest migrate`` and ``manifest vacuum`` also raise a
:func:`manifest_failed` for each manifest whose database they failed on
(:attr:`Stage.MIGRATE` / :attr:`Stage.VACUUM`), once the verb has finished.

A sink that should only page on failures sets ``level: ERROR`` on its
handler. Records propagate, so they also reach the root handlers (console,
Logfire) unless the config sets ``propagate: false`` on this logger.

Neither record carries a traceback: whatever raised was logged, with its
traceback, by the module that caught it. The fields are also set as record
attributes (``alert``, ``manifest_id``, ``manifest_path``, ...), which the
JSON log format writes out as fields.
"""

import enum
import logging
import os

LOGGER_NAME = "soliplex.agents.alerts"
logger = logging.getLogger(LOGGER_NAME)

ALERT_FAILED = "manifest_failed"
ALERT_COMPLETED = "manifest_completed"
UNKNOWN = "unknown"


class Stage(enum.StrEnum):
    """Where in a manifest's life the failure happened."""

    MANIFEST_FILE = "manifest_file"  # invalid file, duplicate id
    RUN = "run"  # the run raised (a pre-run step failing under on_error: fail, ...)
    COMPONENTS = "components"  # component or file errors
    HAIKU_LOAD = "haiku_load"  # the load raised, exited non-zero or timed out
    POST_PROCESS = "post_process"  # a post-process step raised
    MIGRATE = "migrate"  # `manifest migrate` failed on the manifest's database
    VACUUM = "vacuum"  # `manifest vacuum` failed on the manifest's database


class Outcome(enum.StrEnum):
    """How a manifest that did not fail finished."""

    OK = "ok"  # the run, and the load and post-process when requested
    NO_LOAD = "no_load"  # the run; no load was requested
    SKIPPED = "skipped"  # a pre-run step called the run off
    LOAD_SKIPPED_EMPTY = "load_skipped_empty"  # load skipped over an empty location


_OUTCOME_TEXT = {
    Outcome.OK: "load ok",
    Outcome.NO_LOAD: "no load",
    Outcome.SKIPPED: "skipped by pre-run",
    Outcome.LOAD_SKIPPED_EMPTY: "load skipped",
}

# Counts named in a completion message, in this order; zeros are left out.
_COUNT_LABELS = {
    "ingested": "ingested",
    "deleted": "deleted",
    "not_found": "not found",
    "pre_process_skipped": "pre-process skipped",
    "pre_process_modified": "pre-process modified",
    "post_process_steps": "post-process steps",
}


def _path(path: str | None) -> str:
    """*path* made absolute, so the record is usable from any directory."""
    return os.path.abspath(path) if path else UNKNOWN


def manifest_failed(
    *,
    manifest_id: str | None,
    path: str | None,
    stage: Stage,
    reasons: list[str],
    source: str | None = None,
) -> None:
    """Emit one ERROR record: this manifest failed and needs an operator.

    Args:
        manifest_id: The manifest's id; ``None`` when the file never loaded.
        path: The manifest file; ``None`` when not known.
        stage: Where it failed (the first failure, when there are several).
        reasons: Every failure, one short description each.
        source: The manifest's source, when known.
    """
    manifest_id = manifest_id or UNKNOWN
    manifest_path = _path(path)
    logger.error(
        "Manifest '%s' (%s) needs attention: %s: %s",
        manifest_id,
        manifest_path,
        stage,
        "; ".join(reasons),
        extra={
            "alert": ALERT_FAILED,
            "manifest_id": manifest_id,
            "manifest_path": manifest_path,
            "manifest_source": source,
            "stage": str(stage),
            "reasons": list(reasons),
        },
    )


def manifest_completed(
    *,
    manifest_id: str,
    path: str | None,
    source: str,
    outcome: Outcome,
    counts: dict[str, int],
    note: str | None = None,
) -> None:
    """Emit one INFO record: this manifest finished without a failure.

    Args:
        manifest_id: The manifest's id.
        path: The manifest file; ``None`` when not known.
        source: The manifest's source.
        outcome: How it finished.
        counts: What it did -- see :func:`completion_counts`.
        note: Why a run stopped short (the pre-run step that skipped it, why
            the load was skipped), for those outcomes.
    """
    manifest_path = _path(path)
    parts = [f"{counts[key]} {label}" for key, label in _COUNT_LABELS.items() if counts.get(key)]
    parts.append(_OUTCOME_TEXT[outcome])
    suffix = f" ({note})" if note else ""
    logger.info(
        "Manifest '%s' (%s) completed: %s%s",
        manifest_id,
        manifest_path,
        ", ".join(parts),
        suffix,
        extra={
            "alert": ALERT_COMPLETED,
            "manifest_id": manifest_id,
            "manifest_path": manifest_path,
            "manifest_source": source,
            "outcome": str(outcome),
            "counts": dict(counts),
            "note": note,
        },
    )


def completion_counts(run_result: dict | None, load_result: dict | None = None) -> dict[str, int]:
    """The counts a completion record carries, from a run and its load.

    Args:
        run_result: A manifest run's result (its ``summary`` is read).
        load_result: The run's haiku load result, when one ran.
    """
    summary = (run_result or {}).get("summary") or {}
    counts = {key: summary.get(key, 0) for key in _COUNT_LABELS if key != "post_process_steps"}
    counts["post_process_steps"] = len((load_result or {}).get("post_process") or [])
    return counts
