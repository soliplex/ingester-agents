"""Built-in pre-run steps.

A pre-run step is referenced from a manifest's ``config.pre_run`` list by
dotted path and called as ``method(context, **kwargs)`` once, before any
component runs (see :mod:`soliplex.agents.manifest.pre_run`).
"""

import logging
import shutil
import tempfile
from pathlib import Path

from soliplex.agents.config import settings
from soliplex.agents.manifest import webhook
from soliplex.agents.manifest.pre_run import PreRunContext
from soliplex.agents.manifest.pre_run import PreRunStatus

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


async def notify_webhook(
    context: PreRunContext,
    *,
    url: str | None = None,
    url_secret: str | None = None,
    headers: dict[str, str] | None = None,
    secret_headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> PreRunStatus:
    """POST a "manifest starting" notification.

    The body is ``{"event": "manifest.started", "manifest_id",
    "manifest_name", "source", "started_at", "download_uri"}``. A notification
    never decides whether the run happens, so this always returns CONTINUE; a
    failed delivery raises, and the step's ``on_error`` decides what that
    means -- configure ``on_error: continue`` so a webhook outage cannot block
    ingestion.

    Args:
        context: The run's pre-run context.
        url: Webhook URL, or ...
        url_secret: ... the name of a docker secret / env var holding it.
        headers: Literal headers to send.
        secret_headers: Headers whose values are docker secret / env var
            names, resolved before sending (e.g. ``{Authorization: TOKEN}``).
        timeout: Seconds before the request is abandoned.
    """
    target, resolved_headers = webhook.resolve_target(url, url_secret, headers, secret_headers)
    manifest = context.manifest
    await webhook.post_json(
        target,
        {
            "event": "manifest.started",
            "manifest_id": manifest.id,
            "manifest_name": manifest.name,
            "source": manifest.source,
            "started_at": context.started_at,
            "download_uri": context.load.download_uri,
        },
        headers=resolved_headers,
        timeout=timeout,
    )
    return PreRunStatus.CONTINUE


def _existing(path: Path) -> Path:
    """*path*, or its nearest ancestor that exists (a fresh download dir may not yet)."""
    path = path.resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _gb(size: int) -> str:
    return f"{size / (1024 * _MB):.1f} GB"


def check_free_space(context: PreRunContext, *, min_free_mb: float, include_spool: bool = True):
    """Skip the run when the disk it writes to is short of space.

    Checks the download directory for a local store, and -- with
    *include_spool* -- the pre-process spool directory
    (``PRE_PROCESS_SPOOL_DIR``, default the system temp dir), which stages
    every document on either backend. Object storage has no free space to
    check, so an S3 store is reported rather than checked.

    Args:
        context: The run's pre-run context.
        min_free_mb: Minimum free space required on each checked disk, in MB.
        include_spool: Also check the spool directory.
    """
    need = int(min_free_mb * _MB)
    checks: list[tuple[str, Path]] = []
    target = context.load.target
    if target.is_local:
        checks.append(("download dir", Path(target.dir)))
    if include_spool:
        checks.append(("spool dir", Path(settings.pre_process_spool_dir or tempfile.gettempdir())))
    for label, path in checks:
        free = shutil.disk_usage(_existing(path)).free
        if free < need:
            return PreRunStatus.SKIP, f"{_gb(free)} free under {path} ({label}), need {_gb(need)}"
    if not target.is_local:
        return PreRunStatus.CONTINUE, "free-space check not applicable to object storage"
    return PreRunStatus.CONTINUE
