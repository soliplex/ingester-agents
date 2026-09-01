"""Local synchronization state, per source, backed by SQLite.

Replaces the Ingester's ``/source-status`` change detection and
``/sync-state`` commit tracking with a local store. Each source gets one
SQLite file under ``settings.state_dir`` named ``<sanitized-source>.db``
with two tables:

* ``files`` — one row per document URI with its content hash, used to
  decide which files are new/changed and to prune stale entries.
* ``sync`` — a single row holding the SCM commit marker, branch and
  last-sync timestamp for incremental syncs.

Connections are cached per state file and run in WAL mode. Both parts are
needed together: reopening the database for every document costs far more
than the commit does, and WAL only pays off once the connection outlives a
single write (measured on an ingest-shaped workload, 7.4 ms/doc for
open-per-write, 8.7 ms/doc for open-per-write under WAL, 0.11 ms/doc for a
reused WAL connection). Each write is still its own transaction, so a crash
mid-run loses nothing already recorded.

The cache means the file stays open, so anything that removes or copies a
state file must call :func:`close_state_connections` first -- see
:func:`reset_state`.
"""

import datetime
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from soliplex.agents import local_store
from soliplex.agents.common import mime
from soliplex.agents.config import settings
from soliplex.agents.local_store import sanitize_source
from soliplex.agents.sidecar import Sidecars
from soliplex.agents.store import get_document_store

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


def get_state_path(source: str, target=None) -> Path:
    """Return the SQLite state file path for *source* under *target*.

    Change detection compares each URI's upstream hash against this state, and
    a store swap changes neither the URIs nor the hashes -- so a source pointed
    at a new target would report every document ``unchanged`` and write nothing
    to the new location. Qualifying the filename by the target makes the swap
    open a fresh, empty state instead, so everything re-fetches.

    A **local default** target keeps the historical unqualified name. That is
    deliberate: suffixing unconditionally would orphan every existing
    ``<source>.db`` on upgrade and re-fetch every corpus, which is a
    self-inflicted outage from a filename change.

    Rolling a source back is then free -- the original state file is still
    there, still describing the documents at the old location.

    Args:
        source: Source identifier.
        target: Resolved :class:`~soliplex.agents.store.DownloadTarget`.
            Defaults to whatever the installation settings resolve to.

    Returns:
        Path to this source-and-target's SQLite file.
    """
    if target is None:
        from soliplex.agents.store import get_document_store

        target = get_document_store(source).target
    suffix = "" if target.is_local and target.dir == settings.download_dir else f".{target.digest()}"
    return Path(settings.state_dir) / f"{sanitize_source(source)}{suffix}.db"


# One live connection per state file, keyed by its resolved path so a
# settings change (or a target swap) opens a different database rather than
# reusing the wrong one.
_connections: dict[str, sqlite3.Connection] = {}


def _prepare(conn: sqlite3.Connection) -> None:
    """Put a fresh connection into WAL mode and ensure the schema exists."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_CREATE_FILES)
    conn.execute(_CREATE_SYNC)


def close_state_connections(path: Path | None = None) -> None:
    """Close cached connections -- *path*'s, or all of them.

    Call this before removing or copying a state file. The cached connection
    holds the file open (Windows refuses to unlink an open file), and under
    WAL the most recent commits live in the ``-wal`` sidecar until a clean
    close checkpoints them back into the database -- so a copy taken while a
    connection is open can be missing rows.

    Args:
        path: State file to close, or ``None`` to close every cached
            connection.
    """
    keys = [str(path)] if path is not None else list(_connections)
    for key in keys:
        conn = _connections.pop(key, None)
        if conn is not None:
            conn.close()


@contextmanager
def _get_connection(source: str):
    """Yield the cached SQLite connection for *source*, opening it if needed.

    The connection is deliberately not closed on exit; see the module
    docstring for why it is held open, and :func:`close_state_connections`
    for the cases that must drop it.
    """
    db_path = get_state_path(source)
    key = str(db_path)
    conn = _connections.get(key)
    if conn is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key)
        try:
            _prepare(conn)
        except Exception:
            # Never cache a connection we could not configure.
            conn.close()
            raise
        _connections[key] = conn
    yield conn


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


async def repair_relocated_documents(source: str, download_dir: str | None = None) -> list[str]:
    """Drop documents whose file sits where a stale MIME guess put it.

    On-disk names come from the MIME type recorded in state, not from the
    URI, so a document is stored wherever detection said it belonged at the
    time. Content sniffing cannot tell OOXML formats apart -- every one of
    them is a ZIP and reads back as docx -- so documents written before that
    was corrected landed under the wrong extension: ``/team/report.pptx``
    stored as ``team/report.docx``. Nothing downstream notices, because the
    state row agrees with the misnamed file: it is not stale, its hash still
    matches, and the disk sweep in :func:`reconcile_documents` builds its
    expectations from the same wrong MIME type.

    This clears both sides of that agreement -- the file (with its sidecar)
    and the state row -- so the next run re-fetches the document and writes
    it under its real extension. Two runs to fully heal, by design: the
    repair happens after the current run has already decided what to fetch.

    Dropping the state row is enough to force that re-fetch only for sources
    that decide what to fetch by comparing against state. An incremental SCM
    sync does not: it fetches the files touched by commits it has not seen
    yet, so a document no commit has touched since would never come back --
    the repair would delete it permanently instead of relocating it. So a
    repair also clears the incremental cursor (see :func:`clear_sync_cursor`),
    which costs one full listing on the next run and is self-limiting,
    because a repaired row no longer matches.

    A row qualifies only when its stored type *and* the type implied by its
    own URI extension are both container types that disagree. Renames that
    were deliberate stay untouched (a ``.bin`` sniffed as PDF is stored as
    ``.pdf`` on purpose), and rows stop matching once rewritten, so this
    becomes a no-op after everything has been repaired once.

    Args:
        source: Source identifier.
        download_dir: Override for ``settings.download_dir``.

    Returns:
        List of URIs whose stored document was discarded for re-fetching.
    """
    repaired = []
    state = load_file_state(source)
    untyped = 0
    for uri, entry in state.items():
        stored_type = entry.get("mime_type")
        if not stored_type:
            # No recorded type means no way to tell where the file was put,
            # so the row is skipped. Counted, because a corpus full of these
            # is a state file older than the mime_type column and explains a
            # repair pass that finds nothing.
            untyped += 1
            continue
        if not mime.is_container_type(stored_type):
            continue
        uri_type = mime.detect_mime_type(uri)
        if not mime.is_container_type(uri_type) or uri_type == stored_type:
            continue
        key = local_store.uri_to_relpath(uri, mime_type=stored_type).as_posix()
        # Checked explicitly, and only here: `delete_document` reports nothing
        # about what was present (see `DocumentStore.delete`), and this is the
        # one path that wants to know. A repair is rare and one-time, so the
        # extra lookup costs nothing the sweep would have to pay per object.
        existed = await get_document_store(source, download_dir).exists(key)
        await local_store.delete_document(source, uri, mime_type=stored_type, download_dir=download_dir)
        delete_file(source, uri)
        repaired.append(uri)
        logger.info(
            "repairing %s: stored as %s under %s, re-fetching as %s",
            uri,
            stored_type,
            key,
            uri_type,
        )
        if not existed:
            # The row said the document was there and it was not. Harmless
            # here -- the row is dropped either way -- but it means state and
            # storage had drifted, which is worth knowing about.
            logger.warning("repairing %s: no document found at %s to remove", uri, key)
    if untyped:
        logger.info("relocation repair for %s skipped %d row(s) with no recorded mime_type", source, untyped)
    if repaired:
        logger.info(
            "relocation repair for %s: %d of %d row(s) discarded for re-fetch; clearing incremental cursor",
            source,
            len(repaired),
            len(state),
        )
        clear_sync_cursor(source)
    else:
        logger.debug("relocation repair for %s: nothing to repair in %d row(s)", source, len(state))
    return repaired


async def prune_documents(source: str, current_uris: set[str], download_dir: str | None = None) -> list[str]:
    """Remove stale documents from both the state and the filesystem.

    Drops state entries whose URI is absent from *current_uris* and deletes
    the corresponding document file and ``.meta.json`` sidecar. Also runs
    :func:`repair_relocated_documents` first, so documents stored under a
    stale MIME guess are discarded for re-fetching alongside the genuinely
    stale ones.

    Args:
        source: Source identifier.
        current_uris: Set of URIs currently present in the source.
        download_dir: Override for ``settings.download_dir``.

    Returns:
        List of URIs that were pruned, including any repaired for re-fetch.
    """
    repaired = await repair_relocated_documents(source, download_dir=download_dir)
    state = load_file_state(source)
    removed = prune_files(source, current_uris)
    for uri in removed:
        mime_type = state.get(uri, {}).get("mime_type")
        await local_store.delete_document(source, uri, mime_type=mime_type, download_dir=download_dir)
    return repaired + removed


async def reconcile_documents(source: str, current_uris: set[str], download_dir: str | None = None) -> list[str]:
    """Reconcile the on-disk download folder against *current_uris*.

    Stricter than :func:`prune_documents`: in addition to dropping tracked
    URIs that are no longer present, it sweeps the actual source download
    folder and deletes any file that no longer backs a current URI --
    catching orphans that have no state row (e.g. files left behind when a
    document disappears from a WebDAV listing).

    Runs :func:`repair_relocated_documents` first. That has to happen before
    the state is read: the disk sweep derives every expected filename from
    the MIME type in state, so a document sitting under a stale guess matches
    its own wrong expectation and would otherwise survive the sweep.

    Args:
        source: Source identifier.
        current_uris: Set of URIs currently present in the source.
        download_dir: Override for ``settings.download_dir``.

    Returns:
        List of removed identifiers (URIs repaired for re-fetch, stale URIs,
        and orphan relative paths).
    """
    repaired = await repair_relocated_documents(source, download_dir=download_dir)
    state = load_file_state(source)

    # (A) Tracked URIs no longer present: delete file, sidecar, and state row.
    removed = [uri for uri in state if uri not in current_uris]
    for uri in removed:
        mime_type = state.get(uri, {}).get("mime_type")
        await local_store.delete_document(source, uri, mime_type=mime_type, download_dir=download_dir)
    prune_files(source, current_uris)

    # (B) Disk sweep: delete any file not backing a surviving URI. On-disk
    # names use the resolved MIME type recorded in state, so recomputing the
    # relative path from (uri, stored mime) reproduces the exact file written
    # and cannot false-positive against a file we just stored.
    sidecars = Sidecars(get_document_store(source, download_dir))
    expected: set[str] = set()
    for uri, entry in state.items():
        if uri in current_uris:
            rel = local_store.uri_to_relpath(uri, mime_type=entry.get("mime_type")).as_posix()
            expected.add(rel)
            expected |= sidecars.expected_keys(rel)

    store = get_document_store(source, download_dir)
    for key in await store.list():
        if key not in expected:
            logger.info("deleting orphan %s: no current URI accounts for it", store.uri(key))
            await store.delete(key)
            removed.append(key)

    return repaired + removed


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


def clear_sync_cursor(source: str) -> None:
    """Forget how far an incremental SCM sync has read, keeping everything else.

    Only ``last_commit_sha`` is cleared, so the next run falls back to a full
    listing while ``last_sync_date`` still scopes the issue fetch and the
    per-file hashes in ``files`` still suppress rewrites. The result is one
    extra listing, not a re-ingest.

    A no-op for sources that never wrote a marker.

    Args:
        source: Source identifier.
    """
    with _get_connection(source) as conn:
        with conn:
            conn.execute("UPDATE sync SET last_commit_sha = NULL WHERE id = 1")


def reset_state(source: str) -> bool:
    """Clear all local state for *source* (forces a full resync).

    Args:
        source: Source identifier.

    Returns:
        True if a state file existed and was removed.
    """
    db_path = get_state_path(source)
    # Drop the cached handle first: it keeps the file open, and a clean close
    # also checkpoints and removes the WAL sidecars.
    close_state_connections(db_path)
    try:
        db_path.unlink()
    except FileNotFoundError:
        return False
    return True
