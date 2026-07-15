"""Local synchronization state, per source, backed by SQLite.

Replaces the Ingester's ``/source-status`` change detection and
``/sync-state`` commit tracking with a local store. Each source gets one
SQLite file under ``settings.state_dir`` named ``<sanitized-source>.db``
with two tables:

* ``files`` — one row per document URI with its content hash, used to
  decide which files are new/changed and to prune stale entries.
* ``sync`` — a single row holding the SCM commit marker, branch and
  last-sync timestamp for incremental syncs.
"""

import datetime
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from soliplex.agents import local_store
from soliplex.agents.config import settings
from soliplex.agents.local_store import sanitize_source

logger = logging.getLogger(__name__)

_CREATE_FILES = (
    "CREATE TABLE IF NOT EXISTS files (uri TEXT PRIMARY KEY, sha256 TEXT, etag TEXT, size INTEGER, mime_type TEXT)"
)
_CREATE_SYNC = (
    "CREATE TABLE IF NOT EXISTS sync "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), last_commit_sha TEXT, "
    "branch TEXT, last_sync_date TEXT, metadata TEXT)"
)

STATUS_NEW = "new"
STATUS_MISMATCH = "mismatch"
STATUS_UNCHANGED = "unchanged"
PROCESSABLE_STATUSES = frozenset({STATUS_NEW, STATUS_MISMATCH})


def get_state_path(source: str) -> Path:
    """Return the SQLite state file path for *source*."""
    return Path(settings.state_dir) / f"{sanitize_source(source)}.db"


@contextmanager
def _get_connection(source: str):
    """Open a SQLite connection for *source*, creating tables if needed."""
    db_path = get_state_path(source)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_CREATE_FILES)
        conn.execute(_CREATE_SYNC)
        yield conn
    finally:
        conn.close()


def _uri_of(row: dict) -> str | None:
    """Return the identifier of an inventory row (``uri`` or ``path``)."""
    return row.get("uri") or row.get("path")


def load_file_state(source: str) -> dict[str, dict]:
    """Return the cached file state for *source* keyed by URI.

    Args:
        source: Source identifier.

    Returns:
        Dict mapping URI to ``{"sha256", "etag", "size", "mime_type"}``.
    """
    try:
        with _get_connection(source) as conn:
            rows = conn.execute("SELECT uri, sha256, etag, size, mime_type FROM files").fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Cannot read state for %s: %s", source, exc)
        return {}
    return {r[0]: {"sha256": r[1], "etag": r[2], "size": r[3], "mime_type": r[4]} for r in rows}


def upsert_file(
    source: str,
    uri: str,
    sha256: str | None,
    etag: str | None = None,
    size: int = 0,
    mime_type: str | None = None,
) -> None:
    """Insert or update the cached state for a single document URI."""
    with _get_connection(source) as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO files (uri, sha256, etag, size, mime_type) VALUES (?, ?, ?, ?, ?)",
                (uri, sha256, etag, size, mime_type),
            )


def delete_file(source: str, uri: str) -> None:
    """Remove the cached state for a single document URI."""
    with _get_connection(source) as conn:
        with conn:
            conn.execute("DELETE FROM files WHERE uri = ?", (uri,))


def prune_files(source: str, current_uris: set[str]) -> list[str]:
    """Drop file entries whose URI is no longer present in the source.

    Args:
        source: Source identifier.
        current_uris: Set of URIs currently present in the source.

    Returns:
        List of URIs that were removed from the state.
    """
    with _get_connection(source) as conn:
        rows = conn.execute("SELECT uri FROM files").fetchall()
        removed = [r[0] for r in rows if r[0] not in current_uris]
        if removed:
            with conn:
                conn.executemany("DELETE FROM files WHERE uri = ?", [(u,) for u in removed])
    return removed


def prune_documents(source: str, current_uris: set[str], download_dir: str | None = None) -> list[str]:
    """Remove stale documents from both the state and the filesystem.

    Drops state entries whose URI is absent from *current_uris* and deletes
    the corresponding document file and ``.meta.json`` sidecar.

    Args:
        source: Source identifier.
        current_uris: Set of URIs currently present in the source.
        download_dir: Override for ``settings.download_dir``.

    Returns:
        List of URIs that were pruned.
    """
    state = load_file_state(source)
    removed = prune_files(source, current_uris)
    for uri in removed:
        mime_type = state.get(uri, {}).get("mime_type")
        local_store.delete_document(source, uri, mime_type=mime_type, download_dir=download_dir)
    return removed


def reconcile_documents(source: str, current_uris: set[str], download_dir: str | None = None) -> list[str]:
    """Reconcile the on-disk download folder against *current_uris*.

    Stricter than :func:`prune_documents`: in addition to dropping tracked
    URIs that are no longer present, it sweeps the actual source download
    folder and deletes any file that no longer backs a current URI --
    catching orphans that have no state row (e.g. files left behind when a
    document disappears from a WebDAV listing).

    Args:
        source: Source identifier.
        current_uris: Set of URIs currently present in the source.
        download_dir: Override for ``settings.download_dir``.

    Returns:
        List of removed identifiers (stale URIs and orphan relative paths).
    """
    state = load_file_state(source)

    # (A) Tracked URIs no longer present: delete file, sidecar, and state row.
    removed = [uri for uri in state if uri not in current_uris]
    for uri in removed:
        mime_type = state.get(uri, {}).get("mime_type")
        local_store.delete_document(source, uri, mime_type=mime_type, download_dir=download_dir)
    prune_files(source, current_uris)

    # (B) Disk sweep: delete any file not backing a surviving URI. On-disk
    # names use the resolved MIME type recorded in state, so recomputing the
    # relative path from (uri, stored mime) reproduces the exact file written
    # and cannot false-positive against a file we just stored.
    expected: set[str] = set()
    for uri, entry in state.items():
        if uri in current_uris:
            rel = local_store.uri_to_relpath(uri, mime_type=entry.get("mime_type")).as_posix()
            expected.add(rel)
            expected.add(rel + local_store.META_SUFFIX)

    base = local_store.source_dir(source, download_dir)
    if base.is_dir():
        for path in base.rglob("*"):
            if path.is_file() and path.relative_to(base).as_posix() not in expected:
                path.unlink()
                removed.append(path.relative_to(base).as_posix())

    return removed


def compute_to_process(inventory: list[dict], source: str) -> list[dict]:
    """Return inventory rows that are new or whose content changed.

    Mirrors the Ingester's ``new``/``mismatch`` semantics: a row is
    processed when its URI is unknown, when its ``sha256`` differs from
    the cached value, or when it carries no ``sha256`` (deferred hashing,
    e.g. WebDAV cache miss).

    Args:
        inventory: Inventory rows carrying ``uri``/``path`` and ``sha256``.
        source: Source identifier.

    Returns:
        The subset of *inventory* that needs to be (re)written.
    """
    state = load_file_state(source)
    to_process = []
    for row in inventory:
        uri = _uri_of(row)
        if uri is None:
            continue
        prev = state.get(uri)
        sha = row.get("sha256")
        if prev is None or not sha or prev.get("sha256") != sha:
            to_process.append(row)
    return to_process


def get_sync_meta(source: str) -> dict:
    """Return the SCM sync marker for *source*.

    Args:
        source: Source identifier.

    Returns:
        Dict with ``source_id``, ``last_commit_sha``, ``last_sync_date``
        (a :class:`datetime.datetime` or ``None``), ``branch`` and
        ``metadata``.
    """
    try:
        with _get_connection(source) as conn:
            row = conn.execute("SELECT last_commit_sha, branch, last_sync_date, metadata FROM sync WHERE id = 1").fetchone()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Cannot read sync meta for %s: %s", source, exc)
        row = None

    if row is None:
        return {
            "source_id": source,
            "last_commit_sha": None,
            "last_sync_date": None,
            "branch": "main",
            "metadata": {},
        }

    last_sync_date = None
    if row[2]:
        try:
            last_sync_date = datetime.datetime.fromisoformat(row[2])
        except ValueError:
            logger.warning("Invalid last_sync_date %r for %s", row[2], source)

    metadata = {}
    if row[3]:
        try:
            metadata = json.loads(row[3])
        except (json.JSONDecodeError, ValueError):
            logger.warning("Invalid sync metadata for %s", source)

    return {
        "source_id": source,
        "last_commit_sha": row[0],
        "last_sync_date": last_sync_date,
        "branch": row[1] or "main",
        "metadata": metadata,
    }


def set_sync_meta(
    source: str,
    commit_sha: str | None,
    branch: str = "main",
    last_sync_date: datetime.datetime | None = None,
    metadata: dict | None = None,
) -> None:
    """Persist the SCM sync marker for *source*."""
    date_str = last_sync_date.isoformat() if last_sync_date is not None else None
    meta_str = json.dumps(metadata) if metadata else None
    with _get_connection(source) as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync (id, last_commit_sha, branch, last_sync_date, metadata) VALUES (1, ?, ?, ?, ?)",
                (commit_sha, branch, date_str, meta_str),
            )


def reset_state(source: str) -> bool:
    """Clear all local state for *source* (forces a full resync).

    Args:
        source: Source identifier.

    Returns:
        True if a state file existed and was removed.
    """
    db_path = get_state_path(source)
    try:
        db_path.unlink()
    except FileNotFoundError:
        return False
    return True
