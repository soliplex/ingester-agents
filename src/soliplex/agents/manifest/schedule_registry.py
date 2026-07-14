"""In-memory registry of manifest schedules for hot-reloading.

The registry is reconciled against the manifest directory on a fixed
interval so that added, removed, and re-scheduled manifests take effect
without restarting the server.

This module is deliberately pure: it performs no filesystem or scheduler
I/O. It receives already-parsed manifests plus the current time and returns
the actions the caller should take (which schedules were added, removed, or
changed, and which manifests are due to run now). The server glue owns the
directory scan and the actual execution.
"""

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime

from croniter import croniter

from soliplex.agents.config import Manifest

logger = logging.getLogger(__name__)


@dataclass
class ScheduleEntry:
    """A single manifest's schedule state within the registry."""

    manifest_id: str
    path: str
    cron_expr: str | None  # None for manifests without a schedule
    next_run: datetime | None  # None when unscheduled or cron is invalid


@dataclass
class ReconcileResult:
    """Actions the caller should take after a reconcile pass.

    ``added``/``rescheduled`` carry entries (so the caller can log the cron
    expression); ``removed`` carries ids. ``to_run`` lists the manifests
    that should be executed this cycle: scheduled manifests that are due,
    plus newly-seen unscheduled manifests (which run once).
    """

    added: list[ScheduleEntry] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rescheduled: list[ScheduleEntry] = field(default_factory=list)
    to_run: list[ScheduleEntry] = field(default_factory=list)


def _next_run(cron_expr: str, now: datetime) -> datetime | None:
    """Return the next fire time strictly after *now*, or None if invalid."""
    if not croniter.is_valid(cron_expr):
        logger.warning(
            "Invalid cron expression %r; manifest will not be scheduled",
            cron_expr,
        )
        return None
    return croniter(cron_expr, now).get_next(datetime)


class ScheduleRegistry:
    """Tracks manifest schedules and diffs them against the filesystem."""

    def __init__(self) -> None:
        self._entries: dict[str, ScheduleEntry] = {}

    def reconcile(
        self,
        pairs: list[tuple[Manifest, str]],
        now: datetime,
    ) -> ReconcileResult:
        """Diff the registry against *pairs* and return the actions to take.

        Args:
            pairs: ``(Manifest, path)`` tuples currently present on disk.
            now: The current time (timezone-aware, UTC).

        Returns:
            A :class:`ReconcileResult` describing added/removed/rescheduled
            manifests and those due to run now.
        """
        result = ReconcileResult()
        seen: set[str] = set()

        for manifest, path in pairs:
            mid = manifest.id
            seen.add(mid)
            cron_expr = manifest.schedule.cron if manifest.schedule else None
            existing = self._entries.get(mid)

            if existing is None:
                entry = ScheduleEntry(
                    manifest_id=mid,
                    path=path,
                    cron_expr=cron_expr,
                    next_run=(_next_run(cron_expr, now) if cron_expr is not None else None),
                )
                self._entries[mid] = entry
                result.added.append(entry)
                if cron_expr is None:
                    # Unscheduled manifests run once when first seen.
                    result.to_run.append(entry)
                continue

            # Existing manifest: keep the path current and detect a change
            # to the schedule (added, removed, or a different cron).
            existing.path = path
            if cron_expr != existing.cron_expr:
                existing.cron_expr = cron_expr
                existing.next_run = _next_run(cron_expr, now) if cron_expr is not None else None
                result.rescheduled.append(existing)

        for mid in list(self._entries):
            if mid not in seen:
                del self._entries[mid]
                result.removed.append(mid)

        for entry in self._entries.values():
            if entry.cron_expr is not None and entry.next_run is not None and now >= entry.next_run:
                result.to_run.append(entry)
                # Advance to the next future occurrence so a single fire
                # happens even if several were missed (no catch-up storm).
                entry.next_run = _next_run(entry.cron_expr, now)

        return result
