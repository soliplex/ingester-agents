"""WebDAV ETag-based state caching for change detection."""

import json
import logging
import re
from pathlib import Path

from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


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
        Path to the JSON state file.
    """
    return Path(settings.state_dir) / f"{sanitize_url(webdav_url)}.json"


def load_state(webdav_url: str) -> dict:
    """Load cached ETag/SHA256 state from disk.

    Args:
        webdav_url: WebDAV server URL.

    Returns:
        Dict mapping absolute WebDAV paths to {"etag": ..., "sha256": ...}.
        Returns empty dict if file is missing, corrupted, or unreadable.
    """
    state_path = get_state_path(webdav_url)
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Corrupted state file {state_path}, starting fresh")
        return {}
    except OSError:
        logger.warning(f"Cannot read state file {state_path}, starting fresh")
        return {}
    else:
        if not isinstance(data, dict):
            logger.warning(f"State file {state_path} does not contain a dict, ignoring")
            return {}
        return data


def save_state(webdav_url: str, state: dict) -> None:
    """Write ETag/SHA256 state to disk.

    Creates parent directories if they don't exist.

    Args:
        webdav_url: WebDAV server URL.
        state: Dict mapping absolute WebDAV paths to {"etag": ..., "sha256": ...}.
    """
    state_path = get_state_path(webdav_url)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def prune_state(state: dict, current_paths: set[str]) -> tuple[dict, list[str]]:
    """Remove entries from state that are no longer present on the server.

    Args:
        state: Current state dict.
        current_paths: Set of absolute WebDAV paths currently on the server.

    Returns:
        Tuple of (pruned state dict, list of removed paths).
    """
    removed = [path for path in state if path not in current_paths]
    pruned = {path: entry for path, entry in state.items() if path in current_paths}
    return pruned, removed
