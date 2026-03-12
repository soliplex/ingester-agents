"""Tests for soliplex.agents.webdav.state module."""

import json
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
        assert str(path).replace("\\", "/") == "/tmp/state/webdav_example_com.json"


# --- load_state ---


def test_load_state_missing_file(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {}


def test_load_state_valid_json(tmp_path):
    state_data = {"/docs/test.md": {"etag": '"abc123"', "sha256": "def456"}}
    state_file = tmp_path / "webdav_example_com.json"
    state_file.write_text(json.dumps(state_data))

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == state_data


def test_load_state_corrupted_json(tmp_path):
    state_file = tmp_path / "webdav_example_com.json"
    state_file.write_text("not valid json {{{")

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {}


def test_load_state_not_a_dict(tmp_path):
    state_file = tmp_path / "webdav_example_com.json"
    state_file.write_text(json.dumps(["not", "a", "dict"]))

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {}


def test_load_state_os_error(tmp_path):
    with patch.object(webdav_state, "settings") as mock_settings:
        # Point to a non-existent dir that would cause an OSError on read
        mock_settings.state_dir = str(tmp_path)
        # Create a file that will trigger OSError
        state_file = tmp_path / "webdav_example_com.json"
        state_file.mkdir()  # Creating a directory where file is expected
        result = webdav_state.load_state("https://webdav.example.com")
        assert result == {}


# --- save_state ---


def test_save_state_creates_dirs(tmp_path):
    state_data = {"/docs/test.md": {"etag": '"abc"', "sha256": "def"}}
    state_dir = tmp_path / "nested" / "state"

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(state_dir)
        webdav_state.save_state("https://webdav.example.com", state_data)

    state_file = state_dir / "webdav_example_com.json"
    assert state_file.exists()
    loaded = json.loads(state_file.read_text())
    assert loaded == state_data


def test_save_state_writes_correct_json(tmp_path):
    state_data = {
        "/docs/a.md": {"etag": '"etag1"', "sha256": "hash1"},
        "/docs/b.pdf": {"etag": '"etag2"', "sha256": "hash2"},
    }

    with patch.object(webdav_state, "settings") as mock_settings:
        mock_settings.state_dir = str(tmp_path)
        webdav_state.save_state("https://webdav.example.com", state_data)

    state_file = tmp_path / "webdav_example_com.json"
    loaded = json.loads(state_file.read_text())
    assert loaded == state_data


# --- prune_state ---


def test_prune_state_removes_absent():
    state = {
        "/docs/a.md": {"etag": "e1", "sha256": "h1"},
        "/docs/b.md": {"etag": "e2", "sha256": "h2"},
        "/docs/c.md": {"etag": "e3", "sha256": "h3"},
    }
    current = {"/docs/a.md", "/docs/c.md"}

    pruned, removed = webdav_state.prune_state(state, current)

    assert "/docs/b.md" not in pruned
    assert "/docs/a.md" in pruned
    assert "/docs/c.md" in pruned
    assert removed == ["/docs/b.md"]


def test_prune_state_keeps_all_present():
    state = {"/docs/a.md": {"etag": "e1", "sha256": "h1"}}
    current = {"/docs/a.md"}

    pruned, removed = webdav_state.prune_state(state, current)

    assert pruned == state
    assert removed == []


def test_prune_state_empty_state():
    pruned, removed = webdav_state.prune_state({}, {"/docs/a.md"})
    assert pruned == {}
    assert removed == []


def test_prune_state_empty_current():
    state = {"/docs/a.md": {"etag": "e1", "sha256": "h1"}}
    pruned, removed = webdav_state.prune_state(state, set())
    assert pruned == {}
    assert removed == ["/docs/a.md"]
