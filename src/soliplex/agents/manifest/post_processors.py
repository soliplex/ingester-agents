"""Built-in manifest post-process callbacks.

A post-process callback is referenced from a manifest's ``config.post_process``
list by dotted path and invoked as ``method(source, **kwargs)`` after the
``haiku-ingester`` load for that source completes (see
:mod:`soliplex.agents.manifest.post_process` for how a step is resolved and
called, and the README's "Post-process callbacks" for how one is configured).
This module holds the callbacks that ship with ingester-agents.
"""

import logging

from soliplex.agents import haiku_backfill
from soliplex.agents.manifest import haiku_maint
from soliplex.agents.manifest import webhook
from soliplex.agents.manifest.haiku_process import HaikuRun

logger = logging.getLogger(__name__)

# Default upper bound (seconds) on a vacuum subprocess before it is killed.
DEFAULT_VACUUM_TIMEOUT = 1800


async def vacuum(
    source: str,
    *,
    config: str | None = None,
    timeout: float = DEFAULT_VACUUM_TIMEOUT,
) -> None:
    """Vacuum the per-source LanceDB by running ``haiku-rag vacuum`` as a
    subprocess.

    Delegates to :func:`soliplex.agents.manifest.haiku_maint.run_verb`, which
    is the same code path the ``si-agent manifest vacuum`` CLI verb uses.
    Running out-of-process -- exactly like the ``haiku-ingester`` load -- keeps
    LanceDB's async runtime out of the agent's event loop (avoiding an
    in-process deadlock) and makes the pass killable, so a stuck compaction
    cannot hang the run or leave the process unable to exit. The subprocess's
    stdout/stderr are streamed to the logger line by line. Retention is taken
    from the haiku config's ``storage.vacuum_retention_seconds``.

    Unlike the CLI verb, a failure is **raised**: the post-process chain stops
    on the first error (see :func:`post_process.run_post_process`).

    Args:
        source: The manifest source; slugified to locate the database.
        config: Optional haiku.rag config path (auto-injected by the
            post-process runner). Passed as ``haiku-rag --config``; when omitted
            the subprocess falls back to haiku's own config discovery. It must
            match the DB embedder.
        timeout: Seconds before the vacuum subprocess is killed.

    Raises:
        RuntimeError: if the vacuum times out or exits non-zero.
    """
    result = await haiku_maint.run_verb(
        source,
        "vacuum",
        haiku_cfg=str(config) if config else None,
        timeout=timeout,
    )
    if result["timed_out"]:
        raise RuntimeError(f"Vacuum for source '{source}' timed out after {timeout}s")
    if result["returncode"] != 0:
        raise RuntimeError(f"Vacuum for source '{source}' failed (rc={result['returncode']})")
    logger.info("Vacuum completed for source '%s'", source)


async def backfill_metadata(
    source: str,
    *,
    config: str | None = None,
    missing: list[str] | None = None,
    content_types: list[str] | None = None,
    doc_filter: str | None = None,
    full: bool = False,
    database: str | None = None,
    batch_size: int | None = None,
    attachments: bool = True,
    timeout: float = DEFAULT_VACUUM_TIMEOUT,
) -> None:
    """Re-run the source's haiku-rag ``metadata_provider`` over documents already indexed.

    Delegates to :func:`soliplex.agents.manifest.haiku_maint.run_verb` with
    the ``backfill-metadata`` verb -- the same subprocess the ``si-agent
    manifest backfill-metadata`` CLI verb runs (see
    :mod:`soliplex.agents.haiku_backfill`). As a post-process step it runs
    right after the load, so nothing else is writing the database.

    Scoped, never a full pass by default: this runs after *every* load,
    including scheduled ones where nothing changed, and an unscoped pass would
    fetch every document of the source each time to find there is no work.
    Give ``missing`` (e.g. ``[page_count]``) or ``doc_filter``; after the
    first run only documents that still lack a key are fetched, which is the
    few the provider could not fill. ``full: true`` asks for the full pass
    anyway -- for a one-off after changing what the provider returns.

    Documents that could not be filled are logged, not raised, so the steps
    after this one still run; the run itself failing or timing out is raised.

    Args:
        source: The manifest source; slugified to locate the database.
        config: Optional haiku.rag config path (auto-injected by the
            post-process runner).
        missing: Only documents lacking one of these metadata keys.
        content_types: Only documents of one of these content types.
        doc_filter: A LanceDB ``WHERE`` clause scoping which documents run.
        full: Run without ``missing`` / ``doc_filter`` scoping.
        database: Name of the database in the config's ``lancedb.databases``;
            only needed when it places more than one.
        batch_size: Pagination size for the document listing.
        attachments: Fill PDF attachments too, from their parent document.
        timeout: Seconds before the subprocess is killed.

    Raises:
        ValueError: when neither ``missing``, ``doc_filter`` nor ``full`` is
            given.
        RuntimeError: if the back-fill times out or crashes.
    """
    if not (missing or doc_filter or full):
        raise ValueError("backfill_metadata needs `missing` or `doc_filter` to scope it, or `full: true`")
    result = await haiku_maint.run_verb(
        source,
        haiku_maint.BACKFILL_VERB,
        haiku_cfg=str(config) if config else None,
        timeout=timeout,
        options={
            "missing": missing or [],
            "content_types": content_types or [],
            "doc_filter": doc_filter,
            "db_name": database,
            "batch_size": batch_size,
            "attachments": attachments,
        },
    )
    if result["timed_out"]:
        raise RuntimeError(f"Metadata back-fill for source '{source}' timed out after {timeout}s")
    summary = result["summary"]
    if result["returncode"] == haiku_backfill.EXIT_PARTIAL:
        logger.warning(
            "Metadata back-fill for source '%s' could not fill %d document(s): %s",
            source,
            len(summary["errors"]),
            ", ".join(error["uri"] for error in summary["errors"]),
        )
    elif result["returncode"] != 0:
        raise RuntimeError(f"Metadata back-fill for source '{source}' failed (rc={result['returncode']})")
    logger.info("Metadata back-fill completed for source '%s': %s", source, summary)


# Lines of the load's stderr included in a failure notification.
DEFAULT_STDERR_LINES = 20

# Counts copied from the manifest run's summary into the notification.
_SUMMARY_KEYS = (
    "components",
    "component_errors",
    "empty_components",
    "file_errors",
    "ingested",
    "deleted",
    "pre_process_skipped",
    "pre_process_modified",
)


async def notify_webhook(
    source: str,
    *,
    url: str | None = None,
    url_secret: str | None = None,
    headers: dict[str, str] | None = None,
    secret_headers: dict[str, str] | None = None,
    timeout: float = 10,
    stderr_lines: int = DEFAULT_STDERR_LINES,
    ingester: HaikuRun | None = None,
    run_result: dict | None = None,
) -> None:
    """POST a "load finished" notification.

    The body is ``{"event": "load.finished", "source", "status", "returncode",
    "timed_out", "summary"}``, plus ``"manifest_id"`` when the run is known and
    ``"stderr_tail"`` (the last *stderr_lines* lines) when the load failed or
    timed out. ``status`` is ``ok``, ``failed``, ``timed_out`` or ``no_load``.
    *ingester* and *run_result* are injected by the post-process runner.

    Unlike a pre-run notification, a failed delivery here stops the
    post-process chain (it raises), so list this step last.

    Args:
        source: The manifest source.
        url: Webhook URL, or ...
        url_secret: ... the name of a docker secret / env var holding it.
        headers: Literal headers to send.
        secret_headers: Headers whose values are docker secret / env var names.
        timeout: Seconds before the request is abandoned.
        stderr_lines: stderr lines to include on failure.
        ingester: The load's outcome (auto-injected).
        run_result: The manifest run that queued the load (auto-injected).
    """
    target, resolved_headers = webhook.resolve_target(url, url_secret, headers, secret_headers)
    if ingester is None:
        status = "no_load"
    elif ingester.timed_out:
        status = "timed_out"
    elif ingester.returncode == 0:
        status = "ok"
    else:
        status = "failed"
    summary = (run_result or {}).get("summary") or {}
    payload = {
        "event": "load.finished",
        "source": source,
        "status": status,
        "returncode": ingester.returncode if ingester is not None else None,
        "timed_out": ingester.timed_out if ingester is not None else False,
        "summary": {key: summary[key] for key in _SUMMARY_KEYS if key in summary},
    }
    if run_result and run_result.get("manifest_id"):
        payload["manifest_id"] = run_result["manifest_id"]
    if status in ("failed", "timed_out"):
        payload["stderr_tail"] = "\n".join(ingester.stderr.splitlines()[-stderr_lines:])
    await webhook.post_json(target, payload, headers=resolved_headers, timeout=timeout)
    logger.info("Load notification sent for source '%s' (%s)", source, status)
