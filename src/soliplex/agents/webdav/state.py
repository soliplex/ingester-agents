"""WebDAV ETag-based state caching backed by SQLite."""

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

_CREATE_TABLE = "CREATE TABLE IF NOT EXISTS state (path TEXT PRIMARY KEY, etag TEXT, sha256 TEXT, size INTEGER)"


def sanitize_url(url: str) -> str:
    """Convert a WebDAV URL to a filesystem-safe filename.

    Strips the scheme, replaces special characters with underscores,
    and collapses consecutive underscores.

    Args:
        url: WebDAV server URL (e.g., "https://webdav.example.com:8080/path")

    Returns:
        Sanitized string suitable for use as a filename.
    """
    # Strip scheme (http:// or https://)
    cleaned = re.sub(r"^https?://", "", url)
    # Replace non-alphanumeric characters with underscores
    cleaned = re.sub(r"[^a-zA-Z0-9]", "_", cleaned)
    # Collapse consecutive underscores
    cleaned = re.sub(r"_+", "_", cleaned)
    # Strip leading/trailing underscores
    return cleaned.strip("_")


def get_state_path(webdav_url: str) -> Path:
    """Return the state file path for a given WebDAV server URL.

    Args:
        webdav_url: WebDAV server URL.

    Returns:
        Path to the SQLite state file.
    """
    return Path(settings.state_dir) / f"{sanitize_url(webdav_url)}.db"


def _get_json_path(webdav_url: str) -> Path:
    """Return the legacy JSON state file path."""
    return Path(settings.state_dir) / f"{sanitize_url(webdav_url)}.json"


@contextmanager
def _get_connection(webdav_url: str):
    """Open a SQLite connection, creating the table if needed.

    Automatically migrates from legacy JSON if a .json file exists
    but no .db file does.
    """
    db_path = get_state_path(webdav_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    json_path = _get_json_path(webdav_url)
    need_migrate = not db_path.exists() and json_path.exists()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_CREATE_TABLE)
        if need_migrate:
            _migrate_json(conn, json_path)
        yield conn
    finally:
        conn.close()


def _migrate_json(conn: sqlite3.Connection, json_path: Path) -> None:
    """Import entries from a legacy JSON state file into SQLite."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        logger.warning(f"Could not read legacy state {json_path}, skipping migration")
        return
    if not isinstance(data, dict):
        logger.warning(f"Legacy state {json_path} is not a dict, skipping migration")
        return
    with conn:
        for path, entry in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO state (path, etag, sha256, size) VALUES (?, ?, ?, ?)",
                (
                    path,
                    entry.get("etag"),
                    entry.get("sha256"),
                    entry.get("size", 0),
                ),
            )
    migrated_path = json_path.with_suffix(".json.migrated")
    try:
        json_path.rename(migrated_path)
    except OSError:
        logger.warning(f"Could not rename {json_path} to {migrated_path}")
    logger.info(f"Migrated {len(data)} entries from {json_path}")


def load_state(webdav_url: str) -> dict:
    """Load all cached ETag/SHA256 state from the database.

    Args:
        webdav_url: WebDAV server URL.

    Returns:
        Dict mapping absolute WebDAV paths to
        {"etag": ..., "sha256": ..., "size": ...}.
    """
    try:
        with _get_connection(webdav_url) as conn:
            rows = conn.execute("SELECT path, etag, sha256, size FROM state").fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning(f"Cannot read state for {webdav_url}: {exc}")
        return {}
    return {row[0]: {"etag": row[1], "sha256": row[2], "size": row[3]} for row in rows}


def save_state(webdav_url: str, state: dict) -> None:
    """Upsert all entries from *state* into the database.

    Unlike the former JSON implementation this does **not** remove
    entries that are absent from *state*; use :func:`prune_state`
    for that.

    Args:
        webdav_url: WebDAV server URL.
        state: Dict mapping paths to
               {"etag": ..., "sha256": ..., "size": ...}.
    """
    with _get_connection(webdav_url) as conn:
        with conn:
            for path, entry in state.items():
                conn.execute(
                    "INSERT OR REPLACE INTO state (path, etag, sha256, size) VALUES (?, ?, ?, ?)",
                    (
                        path,
                        entry.get("etag"),
                        entry.get("sha256"),
                        entry.get("size", 0),
                    ),
                )


def upsert_entry(
    webdav_url: str,
    path: str,
    etag: str,
    sha256: str,
    size: int = 0,
) -> None:
    """Insert or update a single file's state.

    Args:
        webdav_url: WebDAV server URL.
        path: Absolute WebDAV path of the file.
        etag: ETag value from the server.
        sha256: SHA-256 hash of the file content.
        size: File size in bytes.
    """
    with _get_connection(webdav_url) as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO state (path, etag, sha256, size) VALUES (?, ?, ?, ?)",
                (path, etag, sha256, size),
            )


def get_entry(webdav_url: str, path: str) -> dict | None:
    """Retrieve a single file's cached state.

    Args:
        webdav_url: WebDAV server URL.
        path: Absolute WebDAV path of the file.

    Returns:
        Dict with etag/sha256/size or None if not found.
    """
    with _get_connection(webdav_url) as conn:
        row = conn.execute(
            "SELECT etag, sha256, size FROM state WHERE path = ?",
            (path,),
        ).fetchone()
    if row is None:
        return None
    return {"etag": row[0], "sha256": row[1], "size": row[2]}


def delete_entry(webdav_url: str, path: str) -> None:
    """Remove a single file's state.

    Args:
        webdav_url: WebDAV server URL.
        path: Absolute WebDAV path of the file.
    """
    with _get_connection(webdav_url) as conn:
        with conn:
            conn.execute("DELETE FROM state WHERE path = ?", (path,))


def prune_state(webdav_url: str, current_paths: set[str]) -> list[str]:
    """Remove entries whose paths are no longer present on the server.

    Args:
        webdav_url: WebDAV server URL.
        current_paths: Set of absolute WebDAV paths currently on
                       the server.

    Returns:
        List of paths that were removed.
    """
    with _get_connection(webdav_url) as conn:
        rows = conn.execute("SELECT path FROM state").fetchall()
        removed = [row[0] for row in rows if row[0] not in current_paths]
        if removed:
            with conn:
                conn.executemany(
                    "DELETE FROM state WHERE path = ?",
                    [(p,) for p in removed],
                )
    return removed
