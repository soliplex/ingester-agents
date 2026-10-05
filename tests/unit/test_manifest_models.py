"""Tests for manifest Pydantic models in config.py."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from soliplex.agents import EmptyComponentError
from soliplex.agents import ValidationError
from soliplex.agents import store as agent_store
from soliplex.agents.config import SCM
from soliplex.agents.config import ContentFilter
from soliplex.agents.config import FSComponent
from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import PreProcessStep
from soliplex.agents.config import PreRunStep
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
            mock_settings.log_config_file = None
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

    def test_exclude_paths(self):
        c = WebDAVComponent(name="test", url="http://dav", path="/docs", exclude_paths=["HR", "**/Private"])
        assert c.exclude_paths == ["HR", "**/Private"]

    def test_exclude_paths_defaults_to_none(self):
        assert WebDAVComponent(name="test", url="http://dav", path="/docs").exclude_paths is None

    def test_exclude_paths_requires_a_path_scan(self):
        with pytest.raises(ValueError, match="applies only to a 'path' scan"):
            WebDAVComponent(name="test", url="http://dav", urls=["/a.pdf"], exclude_paths=["HR"])

    @pytest.mark.parametrize("pattern", ["", "  ", "/", "//"])
    def test_exclude_paths_rejects_an_empty_pattern(self, pattern):
        with pytest.raises(ValueError, match="must not be empty"):
            WebDAVComponent(name="test", url="http://dav", path="/docs", exclude_paths=[pattern])

    @pytest.mark.parametrize("bad", ["https://dav/a.pdf", "a/b.pdf"])
    def test_urls_entry_not_absolute_path_raises(self, bad):
        with pytest.raises(ValueError, match="absolute WebDAV path"):
            WebDAVComponent(name="test", url="http://dav", urls=["/a.pdf", bad])


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

    @pytest.mark.parametrize("bad", ["/a", "ftp://x/a", "https://", "example.com/a"])
    def test_non_http_url_raises(self, bad):
        with pytest.raises(ValueError, match="http:// or https:// URL"):
            WebComponent(name="test", url=bad)

    def test_non_http_urls_entry_raises(self):
        with pytest.raises(ValueError, match=r"invalid: \['/b'\]"):
            WebComponent(name="test", urls=["http://a.com", "/b"])


# --- ManifestConfig ---


class TestManifestConfig:
    def test_defaults(self):
        c = ManifestConfig()
        assert c.delete_stale is True
        assert c.haiku_config is None
        assert c.post_process == []
        # Off: a load over an empty location would delete the whole source.
        assert c.allow_empty_load is False

    def test_allow_empty_load(self):
        assert ManifestConfig(allow_empty_load=True).allow_empty_load is True

    def test_haiku_config_override(self):
        c = ManifestConfig(haiku_config="custom.yaml")
        assert c.haiku_config == "custom.yaml"

    def test_post_process_steps(self):
        c = ManifestConfig(
            post_process=[
                {"method": "pkg.mod:fn", "kwargs": {"x": 1}},
                {"method": "pkg.mod:other"},  # kwargs defaults to {}
            ]
        )
        assert [s.method for s in c.post_process] == ["pkg.mod:fn", "pkg.mod:other"]
        assert c.post_process[0].kwargs == {"x": 1}
        assert c.post_process[1].kwargs == {}


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
            config={"metadata": {"project": "x"}, "haiku_config": "custom.yaml"},
            components=[{"type": "fs", "name": "a", "path": "/a"}],
        )
        assert m.config.metadata == {"project": "x"}
        assert m.config.haiku_config == "custom.yaml"

    def test_manifest_dir_defaults_to_none(self):
        m = Manifest(id="t", name="test", source="src", components=[{"type": "fs", "name": "a", "path": "/a"}])
        assert m.manifest_dir is None

    def test_manifest_dir_can_be_set(self):
        m = Manifest(id="t", name="test", source="src", components=[{"type": "fs", "name": "a", "path": "/a"}])
        m.manifest_dir = "/path/to/manifests"
        assert m.manifest_dir == "/path/to/manifests"

    def test_manifest_dir_excluded_from_yaml(self):
        m = Manifest(
            id="t",
            name="test",
            source="src",
            components=[{"type": "fs", "name": "a", "path": "/a"}],
            manifest_dir="/some/path",
        )
        dumped = m.model_dump()
        assert "manifest_dir" not in dumped

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


# --- get_download_target --------------------------------------------------


def _manifest(source="src"):
    return Manifest(
        id="m",
        name="M",
        source=source,
        components=[{"type": "fs", "name": "c", "path": "/data"}],
    )


def test_download_target_is_the_installation(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", None)
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    agent_store.reset_store_cache()
    target = _manifest().get_download_target()
    assert target.is_local is True
    assert target.root == tmp_path / "dl" / "src"


def test_download_target_is_an_s3_installation(monkeypatch):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", "inherited")
    monkeypatch.setattr(agent_store.settings, "download_dir", "dl")
    monkeypatch.setattr(agent_store, "_make_s3_store", lambda bucket, options: None)
    agent_store.reset_store_cache()
    assert _manifest().get_download_target().base_uri == "s3://inherited/dl/src"


def test_download_target_dir_argument_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", None)
    agent_store.reset_store_cache()
    target = _manifest().get_download_target(download_dir=str(tmp_path / "explicit"))
    assert target.root == tmp_path / "explicit" / "src"


# --- unknown keys are rejected -----------------------------------------------


def _raw(**overrides):
    raw = {
        "id": "m",
        "name": "M",
        "source": "src",
        "components": [{"type": "fs", "name": "c", "path": "/data"}],
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    "raw, location",
    [
        (_raw(bogus=1), ("bogus",)),
        (_raw(config={"extentions": ["md"]}), ("config", "extentions")),
        (_raw(schedule={"cron": "* * * * *", "timezone": "UTC"}), ("schedule", "timezone")),
        (_raw(config={"post_process": [{"method": "m:f", "args": []}]}), ("config", "post_process", 0, "args")),
        (
            _raw(components=[{"type": "fs", "name": "c", "path": "/d", "extentions": ["md"]}]),
            ("components", 0, "fs", "extentions"),
        ),
        (
            _raw(components=[{"type": "scm", "name": "c", "platform": "github", "owner": "o", "repo": "r", "token": "x"}]),
            ("components", 0, "scm", "token"),
        ),
        (
            _raw(components=[{"type": "webdav", "name": "c", "url": "http://dav", "path": "/", "pasword": "x"}]),
            ("components", 0, "webdav", "pasword"),
        ),
        (_raw(components=[{"type": "web", "name": "c", "url": "http://x", "depth": 2}]), ("components", 0, "web", "depth")),
    ],
)
def test_unknown_keys_are_rejected(raw, location):
    """A typo is an error, not a setting silently left at its default."""
    with pytest.raises(PydanticValidationError) as excinfo:
        Manifest(**raw)
    errors = excinfo.value.errors()
    assert [e["loc"] for e in errors if e["type"] == "extra_forbidden"] == [location]


def test_free_form_fields_still_accept_any_keys():
    manifest = Manifest(
        **_raw(
            config={"metadata": {"anything": "x"}, "post_process": [{"method": "m:f", "kwargs": {"any": 1}}]},
            components=[{"type": "fs", "name": "c", "path": "/d", "metadata": {"whatever": "y"}}],
        )
    )
    assert manifest.config.metadata == {"anything": "x"}
    assert manifest.config.post_process[0].kwargs == {"any": 1}
    assert manifest.components[0].metadata == {"whatever": "y"}


def test_download_store_is_rejected_with_an_explanation():
    """The removed override gets a message pointing at what replaced it."""
    expected = "download_store is no longer supported: storage is chosen per installation"
    with pytest.raises(PydanticValidationError, match=expected):
        Manifest(**_raw(config={"download_store": {"target": "s3"}}))


def test_haiku_config_override_is_still_supported():
    manifest = Manifest(**_raw(config={"haiku_config": "haiku.rag.s3.yaml"}))
    assert manifest.config.haiku_config == "haiku.rag.s3.yaml"


# --- manifest hooks ---


class TestHookSteps:
    def _manifest(self, config):
        return Manifest(id="m", name="M", source="s", config=config, components=[{"type": "fs", "name": "c", "path": "/x"}])

    def test_defaults(self):
        config = ManifestConfig()
        assert config.pre_run == []
        assert config.pre_process == []

    def test_pre_run_step_defaults_and_values(self):
        step = PreRunStep(method="pkg:fn")
        assert (step.kwargs, step.on_error, step.timeout) == ({}, "fail", 300)
        assert PreRunStep(method="pkg:fn", timeout=None).timeout is None

    @pytest.mark.parametrize("bad", [{"timeout": 0}, {"on_error": "ignore"}, {"metod": "typo"}])
    def test_pre_run_step_rejects(self, bad):
        with pytest.raises(PydanticValidationError):
            PreRunStep(method="pkg:fn", **bad)

    def test_pre_process_step_defaults(self):
        step = PreProcessStep(method="pkg:fn")
        assert (step.kwargs, step.mime_types, step.on_error) == ({}, None, "continue")

    @pytest.mark.parametrize("bad", [{"mime_type": ["application/pdf"]}, {"extensions": ["pdf"]}, {"on_error": "x"}])
    def test_pre_process_step_rejects(self, bad):
        with pytest.raises(PydanticValidationError):
            PreProcessStep(method="pkg:fn", **bad)

    def test_yaml_shaped_config(self):
        manifest = self._manifest(
            {
                "pre_run": [{"method": "a:b", "kwargs": {"x": 1}, "on_error": "continue", "timeout": 15}],
                "pre_process": [{"method": "c:d", "mime_types": ["application/pdf"]}],
            }
        )
        assert manifest.config.pre_run[0].timeout == 15
        assert manifest.config.pre_process[0].mime_types == ["application/pdf"]
        assert self._manifest({"pre_process": []}).config.pre_process == []


# --- error_on_empty ---

_ERROR_ON_EMPTY_COMPONENTS = [
    {"type": "fs", "name": "c", "path": "/d"},
    {"type": "scm", "name": "c", "platform": "github", "owner": "o", "repo": "r"},
    {"type": "webdav", "name": "c", "url": "http://dav", "path": "/"},
    {"type": "web", "name": "c", "url": "http://x"},
]


@pytest.mark.parametrize("component", _ERROR_ON_EMPTY_COMPONENTS, ids=lambda c: c["type"])
def test_error_on_empty_defaults_to_false(component):
    assert Manifest(**_raw(components=[component])).components[0].error_on_empty is False


@pytest.mark.parametrize("component", _ERROR_ON_EMPTY_COMPONENTS, ids=lambda c: c["type"])
def test_error_on_empty_parses(component):
    manifest = Manifest(**_raw(components=[{**component, "error_on_empty": True}]))
    assert manifest.components[0].error_on_empty is True


def test_error_on_empty_typo_is_rejected():
    with pytest.raises(PydanticValidationError) as excinfo:
        Manifest(**_raw(components=[{"type": "fs", "name": "c", "path": "/d", "error_on_emtpy": True}]))
    errors = excinfo.value.errors()
    assert [e["loc"] for e in errors if e["type"] == "extra_forbidden"] == [("components", 0, "fs", "error_on_emtpy")]


def test_empty_component_error_message():
    err = EmptyComponentError("pubs", "webdav", "pubs-webdav", 3, 3)
    assert str(err) == (
        "Component 'pubs' (webdav) returned no items (after extension filtering) and has error_on_empty set; "
        "skipping stale-document removal for source 'pubs-webdav' (inventory=3, not_found=3)"
    )
    assert (err.component, err.component_type, err.source, err.inventory, err.not_found) == (
        "pubs",
        "webdav",
        "pubs-webdav",
        3,
        3,
    )
