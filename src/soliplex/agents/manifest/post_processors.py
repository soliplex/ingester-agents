"""Built-in manifest post-process callbacks.

A post-process callback is referenced from a manifest's ``config.post_process``
list by dotted path and invoked as ``method(source, **kwargs)`` after the
``haiku-ingester`` load for that source completes (see
:mod:`soliplex.agents.manifest.post_process` for how a step is resolved and
called, and the README's "Post-process callbacks" for how one is configured).
This module holds the callbacks that ship with ingester-agents.
"""

import logging

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


# Lines of the load's stderr included in a failure notification.
DEFAULT_STDERR_LINES = 20

# Counts copied from the manifest run's summary into the notification.
_SUMMARY_KEYS = (
    "components",
    "component_errors",
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
