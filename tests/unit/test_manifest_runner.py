"""Tests for manifest runner — 100% branch coverage required."""

import textwrap
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from soliplex.agents.config import FSComponent
from soliplex.agents.config import Manifest
from soliplex.agents.config import SCMComponent
from soliplex.agents.config import WebComponent
from soliplex.agents.config import WebDAVComponent
from soliplex.agents.config import settings
from soliplex.agents.manifest import runner

# --- load_manifest ---


class TestLoadManifest:
    def test_valid_yaml(self, tmp_path):
        f = tmp_path / "test.yml"
        f.write_text(
            textwrap.dedent("""\
            id: test-m
            name: Test Manifest
            source: test-source
            components:
              - type: fs
                name: comp1
                path: /data
        """)
        )
        m = runner.load_manifest(str(f))
        assert m.id == "test-m"
        assert m.source == "test-source"
        assert len(m.components) == 1
        assert isinstance(m.components[0], FSComponent)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            runner.load_manifest("/nonexistent/path.yml")

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yml"
        f.write_text(":\n  - :\n  bad:\n    [unterminated")
        with pytest.raises(ValueError, match="Invalid YAML"):
            runner.load_manifest(str(f))

    def test_non_mapping_yaml(self, tmp_path):
        f = tmp_path / "list.yml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(TypeError, match="Expected a YAML mapping"):
            runner.load_manifest(str(f))

    def test_pydantic_validation_error(self, tmp_path):
        f = tmp_path / "incomplete.yml"
        f.write_text("id: test\n")
        with pytest.raises(ValidationError, match="validation error"):
            runner.load_manifest(str(f))


# --- load_manifests_from_dir ---


class TestLoadManifestsFromDir:
    def test_loads_multiple(self, tmp_path):
        for name, mid in [("a.yml", "a"), ("b.yaml", "b")]:
            (tmp_path / name).write_text(
                textwrap.dedent(f"""\
                id: {mid}
                name: Manifest {mid}
                source: src-{mid}
                components:
                  - type: fs
                    name: comp
                    path: /data
            """)
            )
        result = runner.load_manifests_from_dir(str(tmp_path))
        assert len(result) == 2
        assert {m.id for m in result} == {"a", "b"}

    def test_skips_invalid_with_warning(self, tmp_path, caplog):
        (tmp_path / "good.yml").write_text(
            textwrap.dedent("""\
            id: good
            name: Good
            source: src
            components:
              - type: fs
                name: comp
                path: /data
        """)
        )
        (tmp_path / "bad.yml").write_text(":::invalid:::")
        import logging

        with caplog.at_level(logging.WARNING):
            result = runner.load_manifests_from_dir(str(tmp_path))
        assert len(result) == 1
        assert result[0].id == "good"
        assert "Skipping invalid manifest" in caplog.text

    def test_duplicate_ids_raises(self, tmp_path):
        for name in ["a.yml", "b.yml"]:
            (tmp_path / name).write_text(
                textwrap.dedent("""\
                id: same-id
                name: Dup
                source: src
                components:
                  - type: fs
                    name: comp
                    path: /data
            """)
            )
        with pytest.raises(ValueError, match="Duplicate manifest IDs"):
            runner.load_manifests_from_dir(str(tmp_path))

    def test_empty_dir(self, tmp_path):
        result = runner.load_manifests_from_dir(str(tmp_path))
        assert result == []


# --- override_settings ---


class TestOverrideSettings:
    def test_override_and_restore(self):
        original = settings.extensions[:]
        with runner.override_settings(extensions=["txt"]):
            assert settings.extensions == ["txt"]
        assert settings.extensions == original

    def test_restore_on_exception(self):
        original = settings.extensions[:]
        with pytest.raises(RuntimeError):
            _raise_inside_override()
        assert settings.extensions == original


def _raise_inside_override():
    with runner.override_settings(extensions=["txt"]):
        assert settings.extensions == ["txt"]
        raise RuntimeError("boom")


# --- _resolve_workflow_params ---


class TestResolveWorkflowParams:
    def test_no_config(self):
        m = Manifest(id="t", name="t", source="s", components=[{"type": "fs", "name": "c", "path": "/p"}])
        params = runner._resolve_workflow_params(m)
        assert params["start_workflows"] is False
        assert params["workflow_definition_id"] is None
        assert params["param_set_id"] is None
        assert params["priority"] == 0

    def test_with_config(self):
        m = Manifest(
            id="t",
            name="t",
            source="s",
            config={
                "start_workflows": True,
                "workflow_definition_id": "wf1",
                "param_set_id": "ps1",
                "priority": 5,
            },
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        params = runner._resolve_workflow_params(m)
        assert params["start_workflows"] is True
        assert params["workflow_definition_id"] == "wf1"
        assert params["param_set_id"] == "ps1"
        assert params["priority"] == 5


# --- run_manifest ---


class TestRunManifest:
    @pytest.fixture
    def fs_manifest(self):
        return Manifest(
            id="fs-test",
            name="FS Test",
            source="fs-src",
            config={"metadata": {"project": "test"}},
            components=[{"type": "fs", "name": "docs", "path": "/data"}],
        )

    @pytest.fixture
    def scm_manifest(self):
        return Manifest(
            id="scm-test",
            name="SCM Test",
            source="scm-src",
            components=[
                {"type": "scm", "name": "repo", "platform": "github", "owner": "org", "repo": "repo"},
            ],
        )

    @pytest.fixture
    def scm_incremental_manifest(self):
        return Manifest(
            id="scm-inc",
            name="SCM Inc",
            source="scm-src",
            components=[
                {
                    "type": "scm",
                    "name": "repo",
                    "platform": "github",
                    "owner": "org",
                    "repo": "repo",
                    "incremental": True,
                    "auth_token": "MY_TOKEN",
                    "base_url": "https://custom.api/v1",
                },
            ],
        )

    @pytest.fixture
    def webdav_path_manifest(self):
        return Manifest(
            id="wdav",
            name="WebDAV",
            source="wdav-src",
            components=[
                {"type": "webdav", "name": "docs", "url": "http://dav", "path": "/docs"},
            ],
        )

    @pytest.fixture
    def webdav_urls_manifest(self):
        return Manifest(
            id="wdav-urls",
            name="WebDAV URLs",
            source="wdav-src",
            components=[
                {"type": "webdav", "name": "docs", "url": "http://dav", "urls": ["/a.pdf", "/b.pdf"]},
            ],
        )

    @pytest.fixture
    def webdav_urls_file_manifest(self):
        return Manifest(
            id="wdav-file",
            name="WebDAV File",
            source="wdav-src",
            components=[
                {"type": "webdav", "name": "docs", "url": "http://dav", "urls_file": "list.txt"},
            ],
        )

    @pytest.fixture
    def web_manifest(self):
        return Manifest(
            id="web-test",
            name="Web Test",
            source="web-src",
            components=[
                {"type": "web", "name": "page", "url": "http://example.com"},
            ],
        )

    @pytest.mark.asyncio
    async def test_fs_component(self, fs_manifest):
        mock_handler = AsyncMock(return_value={"ingested": [1], "errors": []})
        with patch.dict(runner._DISPATCH, {FSComponent: mock_handler}):
            result = await runner.run_manifest(fs_manifest)
        assert result["manifest_id"] == "fs-test"
        assert len(result["results"]) == 1
        assert result["results"][0]["component"] == "docs"
        assert result["results"][0]["result"]["ingested"] == [1]

    @pytest.mark.asyncio
    async def test_scm_component_full(self, scm_manifest):
        mock_handler = AsyncMock(return_value={"ingested": [], "errors": []})
        with patch.dict(runner._DISPATCH, {SCMComponent: mock_handler}):
            result = await runner.run_manifest(scm_manifest)
        assert result["results"][0]["component"] == "repo"
        mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_webdav_path_component(self, webdav_path_manifest):
        mock_handler = AsyncMock(return_value={"ingested": [], "errors": []})
        with patch.dict(runner._DISPATCH, {WebDAVComponent: mock_handler}):
            result = await runner.run_manifest(webdav_path_manifest)
        assert result["results"][0]["component"] == "docs"

    @pytest.mark.asyncio
    async def test_web_component(self, web_manifest):
        mock_handler = AsyncMock(return_value={"ingested": [], "errors": []})
        with patch.dict(runner._DISPATCH, {WebComponent: mock_handler}):
            result = await runner.run_manifest(web_manifest)
        assert result["results"][0]["component"] == "page"

    @pytest.mark.asyncio
    async def test_component_error(self, fs_manifest):
        mock_handler = AsyncMock(side_effect=RuntimeError("connection failed"))
        with patch.dict(runner._DISPATCH, {FSComponent: mock_handler}):
            result = await runner.run_manifest(fs_manifest)
        assert "error" in result["results"][0]
        assert "connection failed" in result["results"][0]["error"]

    @pytest.mark.asyncio
    async def test_unknown_component_type(self):
        m = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        # Replace the component with an object of an unregistered type
        m.components[0] = MagicMock(name="fake_comp")
        m.components[0].name = "unknown"
        result = await runner.run_manifest(m)
        assert "error" in result["results"][0]
        assert "Unknown component type" in result["results"][0]["error"]


# --- dispatch helpers ---


class TestRunFSComponent:
    @pytest.mark.asyncio
    async def test_dispatches_with_extensions(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["txt"]},
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.fs.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_fs_component(component, manifest, runner._resolve_workflow_params(manifest), {})
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[0][0] == "/p"
            assert call_kwargs[0][1] == "s"

    @pytest.mark.asyncio
    async def test_dispatches_without_extensions(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        component = manifest.components[0]
        original_ext = settings.extensions[:]
        with patch("soliplex.agents.fs.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_fs_component(component, manifest, runner._resolve_workflow_params(manifest), {"k": "v"})
            mock.assert_called_once()
        assert settings.extensions == original_ext

    @pytest.mark.asyncio
    async def test_empty_metadata_passes_none(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.fs.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_fs_component(component, manifest, runner._resolve_workflow_params(manifest), {})
            assert mock.call_args.kwargs["extra_metadata"] is None


class TestRunSCMComponent:
    @pytest.mark.asyncio
    async def test_full_sync(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[
                {"type": "scm", "name": "r", "platform": "github", "owner": "o", "repo": "r"},
            ],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.scm.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_scm_component(component, manifest, runner._resolve_workflow_params(manifest), {})
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_incremental_sync_with_credentials(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[
                {
                    "type": "scm",
                    "name": "r",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "incremental": True,
                    "auth_token": "MY_TOKEN",
                    "base_url": "https://custom.api/v1",
                },
            ],
        )
        component = manifest.components[0]
        with (
            patch("soliplex.agents.scm.app.incremental_sync", new_callable=AsyncMock) as mock,
            patch("soliplex.agents.manifest.runner.resolve_credential", return_value="secret123"),
        ):
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_scm_component(component, manifest, runner._resolve_workflow_params(manifest), {"k": "v"})
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_extensions_override(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["rst"]},
            components=[
                {"type": "scm", "name": "r", "platform": "github", "owner": "o", "repo": "r"},
            ],
        )
        component = manifest.components[0]
        original_ext = settings.extensions[:]
        with patch("soliplex.agents.scm.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_scm_component(component, manifest, runner._resolve_workflow_params(manifest), {})
        assert settings.extensions == original_ext


class TestRunWebDAVComponent:
    @pytest.mark.asyncio
    async def test_path_mode(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "webdav", "name": "d", "url": "http://dav", "path": "/docs"}],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.webdav.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, runner._resolve_workflow_params(manifest), {})
            mock.assert_called_once()
            assert mock.call_args.kwargs["webdav_url"] == "http://dav"

    @pytest.mark.asyncio
    async def test_urls_file_mode(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "webdav", "name": "d", "url": "http://dav", "urls_file": "list.txt"}],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.webdav.app.load_inventory_from_urls", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, runner._resolve_workflow_params(manifest), {})
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_urls_list_mode(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[
                {"type": "webdav", "name": "d", "url": "http://dav", "urls": ["/a.pdf", "/b.pdf"]},
            ],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.webdav.app.load_inventory_from_urls", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, runner._resolve_workflow_params(manifest), {"k": "v"})
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_credentials(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[
                {
                    "type": "webdav",
                    "name": "d",
                    "url": "http://dav",
                    "path": "/docs",
                    "username": "USER_VAR",
                    "password": "PASS_VAR",
                },
            ],
        )
        component = manifest.components[0]
        with (
            patch("soliplex.agents.webdav.app.load_inventory", new_callable=AsyncMock) as mock,
            patch("soliplex.agents.manifest.runner.resolve_credential", side_effect=["user1", "pass1"]),
        ):
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, runner._resolve_workflow_params(manifest), {})
            assert mock.call_args.kwargs["webdav_username"] == "user1"
            assert mock.call_args.kwargs["webdav_password"] == "pass1"

    @pytest.mark.asyncio
    async def test_extensions_override(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["pdf"]},
            components=[{"type": "webdav", "name": "d", "url": "http://dav", "path": "/docs"}],
        )
        component = manifest.components[0]
        original_ext = settings.extensions[:]
        with patch("soliplex.agents.webdav.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, runner._resolve_workflow_params(manifest), {})
        assert settings.extensions == original_ext


class TestRunWebComponent:
    @pytest.mark.asyncio
    async def test_dispatches(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "web", "name": "p", "url": "http://example.com"}],
        )
        component = manifest.components[0]
        with (
            patch("soliplex.agents.web.app.resolve_urls", new_callable=AsyncMock) as mock_resolve,
            patch("soliplex.agents.web.app.load_inventory", new_callable=AsyncMock) as mock_load,
        ):
            mock_resolve.return_value = ["http://example.com"]
            mock_load.return_value = {"ingested": [], "errors": []}
            await runner._run_web_component(component, manifest, runner._resolve_workflow_params(manifest), {"k": "v"})
            mock_resolve.assert_called_once()
            mock_load.assert_called_once()


# --- run_manifests ---


class TestRunManifests:
    @pytest.mark.asyncio
    async def test_single_file(self, tmp_path):
        f = tmp_path / "test.yml"
        f.write_text(
            textwrap.dedent("""\
            id: test
            name: Test
            source: src
            components:
              - type: fs
                name: c
                path: /data
        """)
        )
        with patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock) as mock:
            mock.return_value = {"manifest_id": "test", "results": []}
            results = await runner.run_manifests(str(f))
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_directory(self, tmp_path):
        for name, mid in [("a.yml", "a"), ("b.yml", "b")]:
            (tmp_path / name).write_text(
                textwrap.dedent(f"""\
                id: {mid}
                name: M{mid}
                source: src
                components:
                  - type: fs
                    name: c
                    path: /data
            """)
            )
        with patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock) as mock:
            mock.return_value = {"manifest_id": "x", "results": []}
            results = await runner.run_manifests(str(tmp_path))
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_path(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            await runner.run_manifests("/nonexistent/path")

    @pytest.mark.asyncio
    async def test_duplicate_ids_in_dir(self, tmp_path):
        for name in ["a.yml", "b.yml"]:
            (tmp_path / name).write_text(
                textwrap.dedent("""\
                id: same
                name: Dup
                source: src
                components:
                  - type: fs
                    name: c
                    path: /data
            """)
            )
        with pytest.raises(ValueError, match="Duplicate manifest IDs"):
            await runner.run_manifests(str(tmp_path))
