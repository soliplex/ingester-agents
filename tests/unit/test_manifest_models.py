"""Tests for manifest Pydantic models in config.py."""

import os
from unittest.mock import patch

import pytest

from soliplex.agents import ValidationError
from soliplex.agents.config import SCM
from soliplex.agents.config import ContentFilter
from soliplex.agents.config import FSComponent
from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import Schedule
from soliplex.agents.config import SCMComponent
from soliplex.agents.config import WebComponent
from soliplex.agents.config import WebDAVComponent
from soliplex.agents.config import configure_logging
from soliplex.agents.config import resolve_credential

# --- ValidationError ---


class TestValidationError:
    def test_message(self):
        err = ValidationError({"bad": "config"})
        assert "Invalid config" in str(err)


# --- configure_logging ---


class TestConfigureLogging:
    def test_configure_logging_success(self):
        configure_logging()

    def test_configure_logging_fallback(self):
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.log_level = "INVALID_LEVEL"
            mock_settings.log_format = None
            # Force basicConfig to raise on first call
            with patch("logging.basicConfig", side_effect=[ValueError("bad"), None]):
                configure_logging()


# --- resolve_credential ---


class TestResolveCredential:
    def test_resolve_from_env_var(self):
        with patch.dict(os.environ, {"MY_TOKEN": "secret123"}):
            assert resolve_credential("MY_TOKEN") == "secret123"

    def test_resolve_from_docker_secret(self, tmp_path):
        secret_file = tmp_path / "MY_SECRET"
        secret_file.write_text("docker_secret_value\n")
        with patch("soliplex.agents.config.Path") as mock_path:
            mock_path.return_value = secret_file
            assert resolve_credential("MY_SECRET") == "docker_secret_value"

    def test_resolve_not_found(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="not found"):
                resolve_credential("NONEXISTENT_CRED")

    def test_docker_secret_takes_precedence(self, tmp_path):
        secret_file = tmp_path / "DUAL_CRED"
        secret_file.write_text("from_secret\n")
        with patch.dict(os.environ, {"DUAL_CRED": "from_env"}):
            with patch("soliplex.agents.config.Path") as mock_path:
                mock_path.return_value = secret_file
                assert resolve_credential("DUAL_CRED") == "from_secret"


# --- FSComponent ---


class TestFSComponent:
    def test_minimal(self):
        c = FSComponent(name="test", path="/data")
        assert c.type == "fs"
        assert c.extensions is None
        assert c.metadata is None

    def test_with_overrides(self):
        c = FSComponent(name="test", path="/data", extensions=["txt"], metadata={"key": "val"})
        assert c.extensions == ["txt"]
        assert c.metadata == {"key": "val"}


# --- SCMComponent ---


class TestSCMComponent:
    def test_github_minimal(self):
        c = SCMComponent(name="test", platform="github", owner="org", repo="repo")
        assert c.platform == SCM.GITHUB
        assert c.incremental is False
        assert c.branch == "main"
        assert c.content_filter == ContentFilter.ALL

    def test_gitea_with_base_url(self):
        c = SCMComponent(name="test", platform="gitea", owner="org", repo="repo", base_url="https://gitea.example.com/api/v1")
        assert c.platform == SCM.GITEA
        assert c.base_url == "https://gitea.example.com/api/v1"

    def test_gitea_warns_without_base_url(self, caplog):
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.scm_base_url = None
            with caplog.at_level("WARNING"):
                SCMComponent(name="test", platform="gitea", owner="org", repo="repo")
            assert "base_url" in caplog.text

    def test_gitea_no_warning_with_settings_base_url(self, caplog):
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.scm_base_url = "https://gitea.example.com/api/v1"
            with caplog.at_level("WARNING"):
                SCMComponent(name="test", platform="gitea", owner="org", repo="repo")
            assert "base_url" not in caplog.text

    def test_all_options(self):
        c = SCMComponent(
            name="test",
            platform="github",
            owner="org",
            repo="repo",
            incremental=True,
            branch="develop",
            content_filter="issues",
            auth_token="MY_TOKEN",
            extensions=["md"],
            metadata={"team": "backend"},
        )
        assert c.incremental is True
        assert c.branch == "develop"
        assert c.content_filter == ContentFilter.ISSUES


# --- WebDAVComponent ---


class TestWebDAVComponent:
    def test_with_path(self):
        c = WebDAVComponent(name="test", url="http://dav", path="/docs")
        assert c.path == "/docs"
        assert c.urls is None
        assert c.urls_file is None

    def test_with_urls(self):
        c = WebDAVComponent(name="test", url="http://dav", urls=["/a.pdf", "/b.pdf"])
        assert c.urls == ["/a.pdf", "/b.pdf"]

    def test_with_urls_file(self):
        c = WebDAVComponent(name="test", url="http://dav", urls_file="list.txt")
        assert c.urls_file == "list.txt"

    def test_no_source_raises(self):
        with pytest.raises(ValueError, match="one of"):
            WebDAVComponent(name="test", url="http://dav")

    def test_multiple_sources_raises(self):
        with pytest.raises(ValueError, match="only one"):
            WebDAVComponent(name="test", url="http://dav", path="/docs", urls=["/a.pdf"])

    def test_with_credentials(self):
        c = WebDAVComponent(name="test", url="http://dav", path="/docs", username="USER_VAR", password="PASS_VAR")
        assert c.username == "USER_VAR"
        assert c.password == "PASS_VAR"


# --- WebComponent ---


class TestWebComponent:
    def test_single_url(self):
        c = WebComponent(name="test", url="http://example.com")
        assert c.url == "http://example.com"

    def test_url_list(self):
        c = WebComponent(name="test", urls=["http://a.com", "http://b.com"])
        assert len(c.urls) == 2

    def test_urls_file(self):
        c = WebComponent(name="test", urls_file="pages.txt")
        assert c.urls_file == "pages.txt"

    def test_no_source_raises(self):
        with pytest.raises(ValueError, match="one of"):
            WebComponent(name="test")

    def test_multiple_sources_raises(self):
        with pytest.raises(ValueError, match="only one"):
            WebComponent(name="test", url="http://a.com", urls=["http://b.com"])


# --- ManifestConfig ---


class TestManifestConfig:
    def test_defaults(self):
        c = ManifestConfig()
        assert c.start_workflows is False
        assert c.priority == 0

    def test_workflow_validation_passes(self):
        c = ManifestConfig(start_workflows=True, workflow_definition_id="wf1", param_set_id="ps1")
        assert c.start_workflows is True

    def test_workflow_missing_params_raises(self):
        with pytest.raises(ValueError, match="start_workflows requires"):
            ManifestConfig(start_workflows=True)

    def test_workflow_missing_param_set_raises(self):
        with pytest.raises(ValueError, match="start_workflows requires"):
            ManifestConfig(start_workflows=True, workflow_definition_id="wf1")


# --- Schedule ---


class TestSchedule:
    def test_cron(self):
        s = Schedule(cron="0 0 * * *")
        assert s.cron == "0 0 * * *"


# --- Manifest ---


class TestManifest:
    def test_minimal(self):
        m = Manifest(id="t", name="test", source="src", components=[{"type": "fs", "name": "a", "path": "/a"}])
        assert m.id == "t"
        assert len(m.components) == 1
        assert isinstance(m.components[0], FSComponent)

    def test_discriminated_union(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            components=[
                {"type": "fs", "name": "a", "path": "/a"},
                {"type": "scm", "name": "b", "platform": "github", "owner": "o", "repo": "r"},
                {"type": "webdav", "name": "c", "url": "http://dav", "path": "/docs"},
                {"type": "web", "name": "d", "url": "http://example.com"},
            ],
        )
        assert isinstance(m.components[0], FSComponent)
        assert isinstance(m.components[1], SCMComponent)
        assert isinstance(m.components[2], WebDAVComponent)
        assert isinstance(m.components[3], WebComponent)

    def test_duplicate_names_raises(self):
        with pytest.raises(ValueError, match="Duplicate component names"):
            Manifest(
                id="t",
                name="test",
                source="src",
                components=[
                    {"type": "fs", "name": "dup", "path": "/a"},
                    {"type": "fs", "name": "dup", "path": "/b"},
                ],
            )

    def test_with_schedule(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            schedule={"cron": "0 0 * * *"},
            components=[{"type": "fs", "name": "a", "path": "/a"}],
        )
        assert m.schedule.cron == "0 0 * * *"

    def test_with_config(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            config={"metadata": {"project": "x"}, "priority": 5},
            components=[{"type": "fs", "name": "a", "path": "/a"}],
        )
        assert m.config.metadata == {"project": "x"}
        assert m.config.priority == 5

    def test_get_extensions_component_wins(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            config={"extensions": ["md", "pdf"]},
            components=[{"type": "fs", "name": "a", "path": "/a", "extensions": ["txt"]}],
        )
        assert m.get_extensions(m.components[0]) == ["txt"]

    def test_get_extensions_falls_back_to_config(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            config={"extensions": ["md", "pdf"]},
            components=[{"type": "fs", "name": "a", "path": "/a"}],
        )
        assert m.get_extensions(m.components[0]) == ["md", "pdf"]

    def test_get_extensions_returns_none_without_config(self):
        m = Manifest(id="t", name="test", source="src", components=[{"type": "fs", "name": "a", "path": "/a"}])
        assert m.get_extensions(m.components[0]) is None

    def test_get_metadata_merges(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            config={"metadata": {"project": "enfold", "env": "prod"}},
            components=[{"type": "fs", "name": "a", "path": "/a", "metadata": {"env": "dev", "extra": "val"}}],
        )
        merged = m.get_metadata(m.components[0])
        assert merged == {"project": "enfold", "env": "dev", "extra": "val"}

    def test_get_metadata_config_only(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            config={"metadata": {"project": "enfold"}},
            components=[{"type": "fs", "name": "a", "path": "/a"}],
        )
        assert m.get_metadata(m.components[0]) == {"project": "enfold"}

    def test_get_metadata_component_only(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            components=[{"type": "fs", "name": "a", "path": "/a", "metadata": {"key": "val"}}],
        )
        assert m.get_metadata(m.components[0]) == {"key": "val"}

    def test_get_metadata_empty(self):
        m = Manifest(id="t", name="test", source="src", components=[{"type": "fs", "name": "a", "path": "/a"}])
        assert m.get_metadata(m.components[0]) == {}
