"""Tests for the ``si-agent scm`` maintenance commands."""

import pytest
from typer.testing import CliRunner

from soliplex.agents import local_state
from soliplex.agents import store as agent_store
from soliplex.agents.scm import cli as scm_cli

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the checkout base dir, state dir and download dir at tmp_path."""
    monkeypatch.setattr(scm_cli.settings, "scm_git_repo_base_dir", str(tmp_path / "repos"))
    monkeypatch.setattr(local_state.settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    return tmp_path


def test_reset_clone_deletes_the_branch_checkout_and_clears_the_cursor(env):
    checkout = env / "repos" / "admin" / "docs@develop"
    (checkout / ".git").mkdir(parents=True)
    other = env / "repos" / "admin" / "docs@main"
    other.mkdir(parents=True)
    local_state.set_sync_meta("my-source", "abc123", branch="develop")

    result = runner.invoke(scm_cli.cli, ["reset-clone", "admin/docs", "--branch", "develop", "--source", "my-source"])

    assert result.exit_code == 0, result.output
    assert not checkout.exists()
    assert other.exists()
    assert "Deleted checkout" in result.output
    meta = local_state.get_sync_meta("my-source")
    assert meta["last_commit_sha"] is None
    assert meta["branch"] == "develop"


def test_reset_clone_without_checkout_or_state(env):
    result = runner.invoke(scm_cli.cli, ["reset-clone", "admin/docs", "--source", "unknown"])

    assert result.exit_code == 0, result.output
    assert "No checkout at" in result.output
    assert "No sync state found for unknown" in result.output
    # Nothing was created for a source that never ran.
    assert not local_state.get_state_path("unknown").exists()


def test_reset_clone_without_source_leaves_state_alone(env):
    local_state.set_sync_meta("my-source", "abc123", branch="main")

    result = runner.invoke(scm_cli.cli, ["reset-clone", "admin/docs"])

    assert result.exit_code == 0, result.output
    assert local_state.get_sync_meta("my-source")["last_commit_sha"] == "abc123"


def test_reset_clone_rejects_bad_repo(env):
    result = runner.invoke(scm_cli.cli, ["reset-clone", "no-slash"])
    assert result.exit_code != 0
