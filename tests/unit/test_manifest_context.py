"""Unit tests for soliplex.agents.manifest.context."""

import pytest

from soliplex.agents import store as agent_store
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.sidecar import Sidecars


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> LoadContext:
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    return LoadContext.for_source("gitea:admin:r:all")


def test_for_source_sanitizes_and_resolves(ctx, tmp_path):
    assert ctx.source == "gitea:admin:r:all"
    assert ctx.sanitized == "gitea_admin_r_all"
    assert ctx.target.root == tmp_path / "dl" / "gitea_admin_r_all"
    assert isinstance(ctx.sidecars, Sidecars)


def test_for_source_download_dir_override(tmp_path):
    ctx = LoadContext.for_source("src", download_dir=str(tmp_path / "other"))
    assert ctx.target.root == tmp_path / "other" / "src"


def test_download_uri_is_the_source_base(ctx):
    assert ctx.download_uri == ctx.target.base_uri
    assert ctx.download_uri.startswith("file://")


def test_env_carries_the_context(ctx, tmp_path):
    env = ctx.env({})
    assert env["SOURCE"] == "gitea_admin_r_all"
    assert env["DOWNLOAD_DIR"] == str(tmp_path / "dl")
    assert env["DOWNLOAD_URI"] == ctx.download_uri


def test_env_defaults_to_the_process_environment(ctx, monkeypatch):
    monkeypatch.setenv("UNRELATED_VAR", "kept")
    assert ctx.env()["UNRELATED_VAR"] == "kept"


def test_env_preserves_the_base_it_is_given(ctx):
    assert ctx.env({"OTHER": "x"})["OTHER"] == "x"


def test_env_exports_storage_options_prefixed(tmp_path, monkeypatch):
    """Storage options reach a subprocess as DOWNLOAD_S3_* variables.

    Nothing sets them yet -- the local backend has none -- but the channel is
    what a second backend will use, so it is covered here rather than later.
    """
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    store = agent_store.get_document_store("src")
    ctx = LoadContext(
        source="src",
        sanitized="src",
        store=store,
        sidecars=Sidecars(store),
        storage_options={"endpoint": "http://minio:9000", "region": "xx"},
    )
    env = ctx.env({})
    assert env["DOWNLOAD_S3_ENDPOINT"] == "http://minio:9000"
    assert env["DOWNLOAD_S3_REGION"] == "xx"


def test_context_is_frozen(ctx):
    with pytest.raises(AttributeError):
        ctx.source = "other"
