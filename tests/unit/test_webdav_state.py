"""Tests for soliplex.agents.webdav.state module (SQLite backend)."""

import json
import sqlite3
from unittest.mock import patch

from soliplex.agents.webdav import state as webdav_state

# --- sanitize_url ---


def test_sanitize_url_https():
    assert webdav_state.sanitize_url("https://webdav.example.com") == "webdav_example_com"


def test_sanitize_url_http():
    assert webdav_state.sanitize_url("http://webdav.example.com") == "webdav_example_com"


def test_sanitize_url_with_port():
    assert webdav_state.sanitize_url("https://webdav.example.com:8080") == "webdav_example_com_8080"


def test_sanitize_url_with_path():
    assert webdav_state.sanitize_url("https://webdav.example.com/path/to/dav") == "webdav_example_com_path_to_dav"


def test_sanitize_url_no_scheme():
    assert webdav_state.sanitize_url("webdav.example.com") == "webdav_example_com"


def test_sanitize_url_empty():
    assert webdav_state.sanitize_url("") == ""


# --- get_state_path ---


def test_get_state_path():
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = "/tmp/state"
        path = webdav_state.get_state_path("https://webdav.example.com")
        assert str(path).replace("\\", "/") == "/tmp/state/webdav_example_com.db"


# --- load_state ---


def test_load_state_missing_file(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {}


def test_load_state_valid_db(tmp_path):
    db_path = tmp_path / "webdav_example_com.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(webdav_state._CREATE_TABLE)
    conn.execute(
        "INSERT INTO state VALUES (?, ?, ?, ?)",
        ("/docs/test.md", '"abc123"', "def456", 100),
    )
    conn.commit()
    conn.close()

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {
            "/docs/test.md": {
                "etag": '"abc123"',
                "sha256": "def456",
                "size": 100,
            }
        }


def test_load_state_os_error(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        # Create a directory where the .db file would be
        db_file = tmp_path / "webdav_example_com.db"
        db_file.mkdir()
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {}


# --- save_state ---


def test_save_state_creates_dirs(tmp_path):
    state_data = {"/docs/test.md": {"etag": '"abc"', "sha256": "def"}}
    state_dir = tmp_path / "nested" / "state"

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(state_dir)
        webdav_state.save_state("https://webdav.example.com", state_data)

    db_path = state_dir / "webdav_example_com.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT path, etag, sha256, size FROM state").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0] == ("/docs/test.md", '"abc"', "def", 0)


def test_save_state_upserts(tmp_path):
    """save_state does upserts, not full overwrites."""
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.save_state(
            "https://webdav.example.com",
            {"/docs/a.md": {"etag": '"e1"', "sha256": "h1", "size": 10}},
        )
        webdav_state.save_state(
            "https://webdav.example.com",
            {"/docs/b.pdf": {"etag": '"e2"', "sha256": "h2", "size": 20}},
        )
        result = webdav_state.load_state("https://webdav.example.com")
        assert "/docs/a.md" in result
        assert "/docs/b.pdf" in result


# --- upsert_entry ---


def test_upsert_entry(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.upsert_entry(
            "https://webdav.example.com",
            "/docs/test.md",
            '"etag1"',
            "hash1",
            42,
        )
        entry = webdav_state.get_entry("https://webdav.example.com", "/docs/test.md")
        assert entry == {
            "etag": '"etag1"',
            "sha256": "hash1",
            "size": 42,
        }


def test_upsert_entry_overwrites(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.upsert_entry(
            "https://webdav.example.com",
            "/docs/test.md",
            '"old"',
            "old_hash",
            10,
        )
        webdav_state.upsert_entry(
            "https://webdav.example.com",
            "/docs/test.md",
            '"new"',
            "new_hash",
            20,
        )
        entry = webdav_state.get_entry("https://webdav.example.com", "/docs/test.md")
        assert entry["etag"] == '"new"'
        assert entry["sha256"] == "new_hash"
        assert entry["size"] == 20


# --- get_entry ---


def test_get_entry_missing(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        entry = webdav_state.get_entry("https://webdav.example.com", "/no/such/file.md")
        assert entry is None


# --- delete_entry ---


def test_delete_entry(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.upsert_entry(
            "https://webdav.example.com",
            "/docs/test.md",
            '"e"',
            "h",
            0,
        )
        webdav_state.delete_entry("https://webdav.example.com", "/docs/test.md")
        assert webdav_state.get_entry("https://webdav.example.com", "/docs/test.md") is None


# --- prune_state ---


def test_prune_state_removes_absent(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.save_state(
            "https://webdav.example.com",
            {
                "/docs/a.md": {"etag": "e1", "sha256": "h1"},
                "/docs/b.md": {"etag": "e2", "sha256": "h2"},
                "/docs/c.md": {"etag": "e3", "sha256": "h3"},
            },
        )
        removed = webdav_state.prune_state(
            "https://webdav.example.com",
            {"/docs/a.md", "/docs/c.md"},
        )
        assert removed == ["/docs/b.md"]
        state = webdav_state.load_state("https://webdav.example.com")
        assert "/docs/b.md" not in state
        assert "/docs/a.md" in state
        assert "/docs/c.md" in state


def test_prune_state_keeps_all_present(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.save_state(
            "https://webdav.example.com",
            {"/docs/a.md": {"etag": "e1", "sha256": "h1"}},
        )
        removed = webdav_state.prune_state(
            "https://webdav.example.com",
            {"/docs/a.md"},
        )
        assert removed == []


def test_prune_state_empty_db(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        removed = webdav_state.prune_state(
            "https://webdav.example.com",
            {"/docs/a.md"},
        )
        assert removed == []


def test_prune_state_empty_current(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.save_state(
            "https://webdav.example.com",
            {"/docs/a.md": {"etag": "e1", "sha256": "h1"}},
        )
        removed = webdav_state.prune_state(
            "https://webdav.example.com",
            set(),
        )
        assert removed == ["/docs/a.md"]


# --- JSON migration ---


def test_migrate_json_to_sqlite(tmp_path):
    """Loading state with a legacy .json triggers auto-migration."""
    json_data = {
        "/docs/a.md": {"etag": '"e1"', "sha256": "h1", "size": 10},
        "/docs/b.pdf": {"etag": '"e2"', "sha256": "h2"},
    }
    json_path = tmp_path / "webdav_example_com.json"
    json_path.write_text(json.dumps(json_data))

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")

    assert "/docs/a.md" in result
    assert result["/docs/a.md"]["sha256"] == "h1"
    assert "/docs/b.pdf" in result
    assert not json_path.exists()
    assert (tmp_path / "webdav_example_com.json.migrated").exists()
    assert (tmp_path / "webdav_example_com.db").exists()


def test_migrate_skipped_when_db_exists(tmp_path):
    """If both .json and .db exist, no migration occurs."""
    json_path = tmp_path / "webdav_example_com.json"
    json_path.write_text(json.dumps({"/old": {"etag": "x", "sha256": "y"}}))

    db_path = tmp_path / "webdav_example_com.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(webdav_state._CREATE_TABLE)
    conn.execute(
        "INSERT INTO state VALUES (?, ?, ?, ?)",
        ("/new", '"e"', "h", 0),
    )
    conn.commit()
    conn.close()

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")

    assert "/new" in result
    assert "/old" not in result
    assert json_path.exists()


def test_migrate_corrupted_json(tmp_path):
    """Corrupted JSON file is skipped during migration."""
    json_path = tmp_path / "webdav_example_com.json"
    json_path.write_text("not valid json {{{")

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")

    assert result == {}
    assert (tmp_path / "webdav_example_com.db").exists()


def test_migrate_non_dict_json(tmp_path):
    """JSON file containing non-dict is skipped during migration."""
    json_path = tmp_path / "webdav_example_com.json"
    json_path.write_text(json.dumps(["not", "a", "dict"]))

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")

    assert result == {}


def test_migrate_rename_failure(tmp_path):
    """Migration succeeds even if rename fails."""
    json_data = {"/docs/a.md": {"etag": '"e1"', "sha256": "h1"}}
    json_path = tmp_path / "webdav_example_com.json"
    json_path.write_text(json.dumps(json_data))

    with (
        patch.object(webdav_state, "settings") as mock_settings,
        patch("soliplex.agents.webdav.state.Path.rename", side_effect=OSError("perm")),
    ):
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")

    assert "/docs/a.md" in result
    # JSON file still exists because rename failed
    assert json_path.exists()
