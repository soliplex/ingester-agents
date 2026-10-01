"""Tests for manifest runner — 100% branch coverage required."""

import asyncio
import logging
import textwrap
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opentelemetry.trace import StatusCode
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

    def test_manifest_dir_is_set(self, tmp_path):
        f = tmp_path / "test.yml"
        f.write_text(
            textwrap.dedent("""\
            id: test-m
            name: Test
            source: src
            components:
              - type: fs
                name: c
                path: /data
        """)
        )
        m = runner.load_manifest(str(f))
        assert m.manifest_dir == str(tmp_path.resolve())


# --- load_manifests_with_paths ---


class TestLoadManifestsWithPaths:
    def test_returns_manifest_path_pairs(self, tmp_path):
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
        pairs = runner.load_manifests_with_paths(str(tmp_path))
        assert len(pairs) == 2
        ids = {m.id for m, _ in pairs}
        assert ids == {"a", "b"}
        for _m, p in pairs:
            assert p.endswith((".yml", ".yaml"))

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

        with caplog.at_level(logging.WARNING):
            pairs = runner.load_manifests_with_paths(str(tmp_path))
        assert len(pairs) == 1
        assert pairs[0][0].id == "good"
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
            runner.load_manifests_with_paths(str(tmp_path))

    def test_empty_dir(self, tmp_path):
        assert runner.load_manifests_with_paths(str(tmp_path)) == []


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


# --- run_manifest ---


class TestRunManifest:
    @pytest.fixture
    def fs_manifest(self):
        return Manifest(
            id="fs-test",
            name="FS Test",
            source="fs-src",
            config={"metadata": {"project": "test"}, "delete_stale": False},
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
        assert result["summary"]["component_errors"] == 1


# --- run summary: what the run reports about itself ---


def _two_component_manifest(delete_stale=False):
    return Manifest(
        id="docs-site",
        name="Docs",
        source="docs-src",
        config={"delete_stale": delete_stale},
        components=[
            {"type": "fs", "name": "wiki", "path": "/wiki"},
            {"type": "fs", "name": "notes", "path": "/notes"},
        ],
    )


def _finished_record(caplog):
    (record,) = [r for r in caplog.records if r.getMessage().startswith("Manifest 'docs-site' finished")]
    return record


class TestRunSummary:
    @pytest.mark.asyncio
    async def test_clean_run_logs_info_and_counts(self, caplog):
        handler = AsyncMock(return_value={"ingested": ["a", "b"], "errors": [], "rejected": [{"uri": "c"}]})
        with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.runner"):
            with patch.dict(runner._DISPATCH, {FSComponent: handler}):
                result = await runner.run_manifest(_two_component_manifest())

        assert result["summary"] == {
            "components": 2,
            "component_errors": 0,
            "components_with_file_errors": 0,
            "file_errors": 0,
            "ingested": 4,
            "not_found": 0,
            "rejected": 2,
            "deleted": 0,
            "delete_stale_skipped": False,
        }
        assert caplog.text.count("completed successfully") == 2
        assert _finished_record(caplog).levelno == logging.INFO

    @pytest.mark.asyncio
    async def test_file_errors_are_not_reported_as_success(self, caplog):
        # The 'wiki' component returns 3 failed files; 'notes' is clean.
        failed = {"ingested": ["a"], "errors": [{"uri": u, "error": "502"} for u in ("x", "y", "z")]}
        handler = AsyncMock(side_effect=[failed, {"ingested": ["b"], "errors": []}])
        with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.runner"):
            with patch.dict(runner._DISPATCH, {FSComponent: handler}):
                result = await runner.run_manifest(_two_component_manifest(delete_stale=True))

        summary = result["summary"]
        assert summary["file_errors"] == 3
        assert summary["components_with_file_errors"] == 1
        assert summary["component_errors"] == 0
        assert summary["delete_stale_skipped"] is True
        warning = next(r for r in caplog.records if "file errors" in r.getMessage() and r.name.endswith("runner"))
        assert warning.levelno == logging.WARNING
        assert warning.getMessage() == "Component 'wiki' finished with 3 file errors"
        assert "Component 'wiki' completed successfully" not in caplog.text
        assert "Component 'notes' completed successfully" in caplog.text
        finished = _finished_record(caplog)
        assert finished.levelno == logging.ERROR
        assert "0 component errors, 3 file errors" in finished.getMessage()

    @pytest.mark.asyncio
    async def test_raised_component_is_an_error_with_a_stable_template(self, caplog):
        handler = AsyncMock(side_effect=[RuntimeError("boom"), {"ingested": [], "errors": []}])
        with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.runner"):
            with patch.dict(runner._DISPATCH, {FSComponent: handler}):
                result = await runner.run_manifest(_two_component_manifest())

        assert result["summary"]["component_errors"] == 1
        raised = next(r for r in caplog.records if r.getMessage() == "Error running component wiki")
        # %-style arguments, so Logfire groups every component's failure together.
        assert raised.msg == "Error running component %s"
        assert raised.exc_info is not None
        assert _finished_record(caplog).levelno == logging.ERROR

    @pytest.mark.asyncio
    async def test_counts_deleted_and_not_found(self):
        handler = AsyncMock(
            return_value={"inventory": [{"path": "keep.md", "sha256": "1"}], "not_found": ["gone.md"], "errors": []}
        )
        with (
            patch.dict(runner._DISPATCH, {FSComponent: handler}),
            patch(
                "soliplex.agents.local_state.reconcile_documents",
                AsyncMock(return_value=["old.md", "gone.md"]),
            ) as mock_reconcile,
        ):
            result = await runner.run_manifest(_two_component_manifest(delete_stale=True))

        mock_reconcile.assert_awaited_once_with("docs-src", {"keep.md"})
        assert result["summary"]["deleted"] == 2
        assert result["summary"]["not_found"] == 1
        assert result["summary"]["delete_stale_skipped"] is False


# --- dispatch helpers ---


class TestRunFSComponent:
    @pytest.mark.asyncio
    async def test_dispatches_with_extensions(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["txt"], "delete_stale": False},
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.fs.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_fs_component(component, manifest, {})
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
            await runner._run_fs_component(component, manifest, {"k": "v"})
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
            await runner._run_fs_component(component, manifest, {})
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
            await runner._run_scm_component(component, manifest, {})
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
            await runner._run_scm_component(component, manifest, {"k": "v"})
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_extensions_override(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["rst"], "delete_stale": False},
            components=[
                {"type": "scm", "name": "r", "platform": "github", "owner": "o", "repo": "r"},
            ],
        )
        component = manifest.components[0]
        original_ext = settings.extensions[:]
        with patch("soliplex.agents.scm.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_scm_component(component, manifest, {})
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
            await runner._run_webdav_component(component, manifest, {})
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
            await runner._run_webdav_component(component, manifest, {})
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_urls_file_passes_base_dir(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "webdav", "name": "d", "url": "http://dav", "urls_file": "list.txt"}],
            manifest_dir="/manifests",
        )
        component = manifest.components[0]
        with patch("soliplex.agents.webdav.app.load_inventory_from_urls", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, {})
            assert mock.call_args.kwargs["base_dir"] == "/manifests"

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
            await runner._run_webdav_component(component, manifest, {"k": "v"})
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
            await runner._run_webdav_component(component, manifest, {})
            assert mock.call_args.kwargs["webdav_username"] == "user1"
            assert mock.call_args.kwargs["webdav_password"] == "pass1"

    @pytest.mark.asyncio
    async def test_extensions_override(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["pdf"], "delete_stale": False},
            components=[{"type": "webdav", "name": "d", "url": "http://dav", "path": "/docs"}],
        )
        component = manifest.components[0]
        original_ext = settings.extensions[:]
        with patch("soliplex.agents.webdav.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_webdav_component(component, manifest, {})
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
            await runner._run_web_component(component, manifest, {"k": "v"})
            mock_resolve.assert_called_once()
            mock_load.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_base_dir(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "web", "name": "p", "url": "http://example.com"}],
            manifest_dir="/manifests",
        )
        component = manifest.components[0]
        with (
            patch("soliplex.agents.web.app.resolve_urls", new_callable=AsyncMock) as mock_resolve,
            patch("soliplex.agents.web.app.load_inventory", new_callable=AsyncMock) as mock_load,
        ):
            mock_resolve.return_value = ["http://example.com"]
            mock_load.return_value = {"ingested": [], "errors": []}
            await runner._run_web_component(component, manifest, {})
            assert mock_resolve.call_args.kwargs["base_dir"] == "/manifests"


# --- resolve_manifests ---

_MANIFEST_YAML = textwrap.dedent("""\
    id: {mid}
    name: Test
    source: {mid}
    components:
      - type: fs
        name: c
        path: /data
""")


class TestResolveManifests:
    def test_single_file(self, tmp_path):
        f = tmp_path / "one.yml"
        f.write_text(_MANIFEST_YAML.format(mid="one"))
        assert [m.id for m in runner.resolve_manifests(str(f))] == ["one"]

    def test_directory(self, tmp_path):
        (tmp_path / "a.yml").write_text(_MANIFEST_YAML.format(mid="a"))
        (tmp_path / "b.yaml").write_text(_MANIFEST_YAML.format(mid="b"))
        assert [m.id for m in runner.resolve_manifests(str(tmp_path))] == ["a", "b"]

    def test_all_uses_manifest_dir(self, tmp_path, monkeypatch):
        (tmp_path / "a.yml").write_text(_MANIFEST_YAML.format(mid="a"))
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        assert [m.id for m in runner.resolve_manifests("all")] == ["a"]

    def test_all_without_manifest_dir_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "manifest_dir", None, raising=False)
        with pytest.raises(FileNotFoundError, match="MANIFEST_DIR"):
            runner.resolve_manifests("all")

    def test_all_with_non_directory_manifest_dir_raises(self, tmp_path, monkeypatch):
        f = tmp_path / "not-a-dir.yml"
        f.write_text(_MANIFEST_YAML.format(mid="a"))
        monkeypatch.setattr(settings, "manifest_dir", str(f), raising=False)
        with pytest.raises(FileNotFoundError, match="not a directory"):
            runner.resolve_manifests("all")

    def test_missing_path_raises(self):
        with pytest.raises(FileNotFoundError, match="Path not found"):
            runner.resolve_manifests("/nonexistent/path")


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
            mock.return_value = {"manifest_id": "test", "results": [], "summary": {}}
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
            mock.return_value = {"manifest_id": "x", "results": [], "summary": {}}
            results = await runner.run_manifests(str(tmp_path))
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_load_triggers_haiku_load(self, tmp_path):
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
        with (
            patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock) as mock_run,
            patch(
                "soliplex.agents.manifest.haiku_loader.run_load",
                new_callable=AsyncMock,
            ) as mock_load,
        ):
            mock_run.return_value = {"manifest_id": "test", "results": [], "summary": {}}
            mock_load.return_value = {"source": "src", "returncode": 0}
            results = await runner.run_manifests(str(f), load=True)
        mock_load.assert_awaited_once()
        assert results[0]["haiku_load"] == {"source": "src", "returncode": 0}

    @pytest.mark.asyncio
    async def test_manifest_failure_isolated_to_that_manifest(self, tmp_path):
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
            mock.side_effect = [RuntimeError("boom"), {"manifest_id": "b", "results": [], "summary": {}}]
            results = await runner.run_manifests(str(tmp_path))
        # First manifest failed, but the second still ran.
        assert len(results) == 2
        assert results[0] == {"manifest_id": "a", "manifest_name": "Ma", "error": "boom"}
        assert results[1]["manifest_id"] == "b"

    @pytest.mark.asyncio
    async def test_haiku_load_failure_isolated(self, tmp_path):
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
        with (
            patch("soliplex.agents.manifest.runner.run_manifest", new_callable=AsyncMock) as mock_run,
            patch("soliplex.agents.manifest.haiku_loader.run_load", new_callable=AsyncMock) as mock_load,
        ):
            mock_run.return_value = {"manifest_id": "test", "results": [], "summary": {}}
            mock_load.side_effect = RuntimeError("load boom")
            results = await runner.run_manifests(str(f), load=True)
        # Component result preserved; the load failure is recorded, not raised.
        assert results[0]["manifest_id"] == "test"
        assert results[0]["haiku_load_error"] == "load boom"
        assert "haiku_load" not in results[0]

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


# --- collect_inventory_uris ---


class TestCollectInventoryUris:
    def test_with_path_key(self):
        result = {"inventory": [{"path": "file1.md", "sha256": "aaa"}, {"path": "file2.md", "sha256": "bbb"}]}
        uris = runner.collect_inventory_uris(result)
        assert uris == [{"uri": "file1.md", "sha256": "aaa"}, {"uri": "file2.md", "sha256": "bbb"}]

    def test_with_uri_key(self):
        result = {"inventory": [{"uri": "/owner/repo/file.md", "sha256": "ccc"}]}
        uris = runner.collect_inventory_uris(result)
        assert uris == [{"uri": "/owner/repo/file.md", "sha256": "ccc"}]

    def test_uri_takes_precedence_over_path(self):
        result = {"inventory": [{"uri": "preferred", "path": "fallback", "sha256": "ddd"}]}
        uris = runner.collect_inventory_uris(result)
        assert uris[0]["uri"] == "preferred"

    def test_empty_inventory(self):
        assert runner.collect_inventory_uris({"inventory": []}) == []

    def test_missing_inventory_key(self):
        assert runner.collect_inventory_uris({"ingested": []}) == []

    def test_skips_entries_without_uri_or_path(self):
        result = {"inventory": [{"sha256": "eee"}, {"uri": "ok", "sha256": "fff"}]}
        uris = runner.collect_inventory_uris(result)
        assert len(uris) == 1
        assert uris[0]["uri"] == "ok"

    def test_missing_sha256_defaults_to_empty_string(self):
        result = {"inventory": [{"path": "file.md"}]}
        uris = runner.collect_inventory_uris(result)
        assert uris == [{"uri": "file.md", "sha256": ""}]


# --- run_manifest with delete_stale ---


class TestRunManifestDeleteStale:
    @pytest.fixture
    def delete_stale_manifest(self):
        return Manifest(
            id="ds-test",
            name="Delete Stale Test",
            source="ds-src",
            config={"delete_stale": True},
            components=[
                {"type": "fs", "name": "docs", "path": "/data"},
                {"type": "web", "name": "page", "url": "http://example.com"},
            ],
        )

    @pytest.mark.asyncio
    async def test_delete_stale_called_after_all_components(self, delete_stale_manifest):
        fs_result = {"inventory": [{"path": "a.md", "sha256": "h1"}], "ingested": [], "errors": []}
        web_result = {"inventory": [{"path": "http://example.com", "sha256": "h2"}], "ingested": [], "errors": []}

        mock_fs = AsyncMock(return_value=fs_result)
        mock_web = AsyncMock(return_value=web_result)

        with (
            patch.dict(runner._DISPATCH, {FSComponent: mock_fs, WebComponent: mock_web}),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
        ):
            mock_check.return_value = []
            result = await runner.run_manifest(delete_stale_manifest)

        # reconcile_documents called once with the source and consolidated URI set
        mock_check.assert_called_once()
        call_args = mock_check.call_args
        assert call_args[0][0] == "ds-src"
        assert call_args[0][1] == {"a.md", "http://example.com"}
        assert result["delete_stale_result"] == []

    @pytest.mark.asyncio
    async def test_not_found_excluded_from_reconcile_set(self, delete_stale_manifest, caplog):
        # A 404'd URI is reported in not_found and must be subtracted from the
        # reconcile "should exist" set so its local copy is removed.
        fs_result = {
            "inventory": [{"path": "a.md", "sha256": "h1"}, {"path": "b.md", "sha256": "h2"}],
            "ingested": [],
            "errors": [],
            "not_found": ["b.md"],
        }
        web_result = {"inventory": [{"path": "http://example.com", "sha256": "h3"}], "ingested": [], "errors": []}

        with (
            patch.dict(
                runner._DISPATCH,
                {FSComponent: AsyncMock(return_value=fs_result), WebComponent: AsyncMock(return_value=web_result)},
            ),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
            caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.runner"),
        ):
            mock_check.return_value = []
            await runner.run_manifest(delete_stale_manifest)

        mock_check.assert_called_once()
        assert mock_check.call_args[0][1] == {"a.md", "http://example.com"}
        # The completion summary reports the 404 count.
        assert "1 not found (404)" in caplog.text

    @pytest.mark.asyncio
    async def test_not_found_does_not_block_reconcile(self, delete_stale_manifest):
        # A not_found entry (with no transient errors) must not block the reconcile.
        fs_result = {"inventory": [{"path": "a.md", "sha256": "h1"}], "ingested": [], "errors": [], "not_found": ["b.md"]}
        web_result = {"inventory": [], "ingested": [], "errors": []}

        with (
            patch.dict(
                runner._DISPATCH,
                {FSComponent: AsyncMock(return_value=fs_result), WebComponent: AsyncMock(return_value=web_result)},
            ),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
        ):
            mock_check.return_value = []
            await runner.run_manifest(delete_stale_manifest)

        mock_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_file_errors_block_reconcile(self, delete_stale_manifest, caplog):
        # A transient per-file error (returned in a component's errors list, not
        # raised) must block the reconcile to stay safe.
        fs_result = {
            "inventory": [{"path": "a.md", "sha256": "h1"}],
            "ingested": [],
            "errors": [{"uri": "a.md", "error": "HTTP 500"}],
        }
        web_result = {"inventory": [], "ingested": [], "errors": []}

        with (
            patch.dict(
                runner._DISPATCH,
                {FSComponent: AsyncMock(return_value=fs_result), WebComponent: AsyncMock(return_value=web_result)},
            ),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
            caplog.at_level(logging.WARNING),
        ):
            result = await runner.run_manifest(delete_stale_manifest)

        mock_check.assert_not_called()
        assert result["delete_stale_result"] is None
        assert "Skipping delete_stale" in caplog.text

    @pytest.mark.asyncio
    async def test_delete_stale_skipped_on_component_error(self, delete_stale_manifest, caplog):
        mock_fs = AsyncMock(side_effect=RuntimeError("fail"))
        mock_web = AsyncMock(return_value={"inventory": [], "ingested": [], "errors": []})

        with (
            patch.dict(runner._DISPATCH, {FSComponent: mock_fs, WebComponent: mock_web}),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
            caplog.at_level(logging.WARNING),
        ):
            result = await runner.run_manifest(delete_stale_manifest)

        mock_check.assert_not_called()
        assert result["delete_stale_result"] is None
        assert "Skipping delete_stale" in caplog.text

    @pytest.mark.asyncio
    async def test_delete_stale_skipped_on_unknown_component(self):
        m = Manifest(
            id="ds-unk",
            name="DS Unknown",
            source="src",
            config={"delete_stale": True},
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        m.components[0] = MagicMock(name="fake")
        m.components[0].name = "unknown"

        with patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check:
            result = await runner.run_manifest(m)

        mock_check.assert_not_called()
        assert result["delete_stale_result"] is None

    @pytest.mark.asyncio
    async def test_delete_stale_not_called_when_disabled(self):
        m = Manifest(
            id="no-ds",
            name="No Delete Stale",
            source="src",
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        mock_handler = AsyncMock(return_value={"inventory": [{"path": "f.md", "sha256": "h"}], "ingested": [], "errors": []})

        with (
            patch.dict(runner._DISPATCH, {FSComponent: mock_handler}),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
        ):
            result = await runner.run_manifest(m)

        mock_check.assert_not_called()
        assert result["delete_stale_result"] is None

    @pytest.mark.asyncio
    async def test_delete_stale_false_in_config(self):
        m = Manifest(
            id="ds-off",
            name="DS Off",
            source="src",
            config={"delete_stale": False},
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        mock_handler = AsyncMock(return_value={"inventory": [], "ingested": [], "errors": []})

        with (
            patch.dict(runner._DISPATCH, {FSComponent: mock_handler}),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents") as mock_check,
        ):
            result = await runner.run_manifest(m)

        mock_check.assert_not_called()
        assert result["delete_stale_result"] is None

    @pytest.mark.asyncio
    async def test_result_includes_delete_stale_result_key(self):
        """Even when delete_stale is off, the key is present as None."""
        m = Manifest(
            id="t",
            name="t",
            source="s",
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )
        mock_handler = AsyncMock(return_value={"inventory": [], "ingested": [], "errors": []})
        with patch.dict(runner._DISPATCH, {FSComponent: mock_handler}):
            result = await runner.run_manifest(m)
        assert "delete_stale_result" in result


# --- SCM source passthrough ---


class TestSCMSourcePassthrough:
    @pytest.mark.asyncio
    async def test_full_sync_passes_manifest_source(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="my-manifest-source",
            components=[
                {"type": "scm", "name": "r", "platform": "github", "owner": "o", "repo": "r"},
            ],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.scm.app.load_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_scm_component(
                component,
                manifest,
                {},
            )
            assert mock.call_args.kwargs["source"] == "my-manifest-source"

    @pytest.mark.asyncio
    async def test_incremental_sync_passes_manifest_source(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="my-manifest-source",
            components=[
                {
                    "type": "scm",
                    "name": "r",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "incremental": True,
                    "auth_token": "MY_TOKEN",
                },
            ],
        )
        component = manifest.components[0]
        with (
            patch("soliplex.agents.scm.app.incremental_sync", new_callable=AsyncMock) as mock,
            patch("soliplex.agents.manifest.runner.resolve_credential", return_value="secret"),
        ):
            mock.return_value = {"ingested": [], "errors": []}
            await runner._run_scm_component(
                component,
                manifest,
                {},
            )
            assert mock.call_args.kwargs["source"] == "my-manifest-source"


# --- incremental SCM + delete_stale ---


class TestIncrementalSCMDeleteStale:
    @pytest.mark.asyncio
    async def test_incremental_scm_calls_list_all_uris(self):
        """When delete_stale + incremental SCM, list_all_uris fetches full URI set."""
        m = Manifest(
            id="t",
            name="t",
            source="s",
            config={"delete_stale": True},
            components=[
                {
                    "type": "scm",
                    "name": "r",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "incremental": True,
                },
            ],
        )
        # incremental_sync returns only changed files
        inc_result = {
            "inventory": [{"uri": "changed.md", "sha256": "h1"}],
            "ingested": ["changed.md"],
            "errors": [],
        }
        full_uris = [
            {"uri": "file1.md", "sha256": "a1"},
            {"uri": "file2.md", "sha256": "a2"},
        ]

        mock_scm = AsyncMock(return_value=inc_result)
        with (
            patch.dict(runner._DISPATCH, {SCMComponent: mock_scm}),
            patch(
                "soliplex.agents.manifest.runner._list_scm_all_uris",
                new_callable=AsyncMock,
                return_value=full_uris,
            ) as mock_list,
            patch(
                "soliplex.agents.manifest.runner.local_state.reconcile_documents",
                return_value=[],
            ) as mock_check,
        ):
            await runner.run_manifest(m)

        mock_list.assert_called_once_with(m.components[0], m)
        # prune uses full URIs from list_all_uris, not the partial inventory
        assert mock_check.call_args[0][0] == "s"
        assert mock_check.call_args[0][1] == {"file1.md", "file2.md"}

    @pytest.mark.asyncio
    async def test_incremental_scm_no_delete_stale_skips_list(self):
        """Without delete_stale, list_all_uris is NOT called."""
        m = Manifest(
            id="t",
            name="t",
            source="s",
            config={"delete_stale": False},
            components=[
                {
                    "type": "scm",
                    "name": "r",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "incremental": True,
                },
            ],
        )
        mock_scm = AsyncMock(return_value={"inventory": [], "ingested": [], "errors": []})
        with (
            patch.dict(runner._DISPATCH, {SCMComponent: mock_scm}),
            patch(
                "soliplex.agents.manifest.runner._list_scm_all_uris",
                new_callable=AsyncMock,
            ) as mock_list,
        ):
            await runner.run_manifest(m)

        mock_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_incremental_scm_error_skips_list(self):
        """On component error, list_all_uris is NOT called."""
        m = Manifest(
            id="t",
            name="t",
            source="s",
            config={"delete_stale": True},
            components=[
                {
                    "type": "scm",
                    "name": "r",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "incremental": True,
                },
            ],
        )
        mock_scm = AsyncMock(side_effect=RuntimeError("fail"))
        with (
            patch.dict(runner._DISPATCH, {SCMComponent: mock_scm}),
            patch(
                "soliplex.agents.manifest.runner._list_scm_all_uris",
                new_callable=AsyncMock,
            ) as mock_list,
            patch(
                "soliplex.agents.manifest.runner.local_state.reconcile_documents",
            ) as mock_check,
        ):
            await runner.run_manifest(m)

        mock_list.assert_not_called()
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_fs_and_incremental_scm(self):
        """FS URIs from inventory, SCM URIs from list_all_uris."""
        m = Manifest(
            id="t",
            name="t",
            source="s",
            config={"delete_stale": True},
            components=[
                {"type": "fs", "name": "docs", "path": "/data"},
                {
                    "type": "scm",
                    "name": "repo",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "incremental": True,
                },
            ],
        )
        fs_result = {"inventory": [{"path": "local.md", "sha256": "lh"}], "ingested": [], "errors": []}
        scm_result = {"inventory": [{"uri": "changed.md", "sha256": "ch"}], "ingested": [], "errors": []}
        full_scm_uris = [{"uri": "all1.md", "sha256": "s1"}, {"uri": "all2.md", "sha256": "s2"}]

        mock_fs = AsyncMock(return_value=fs_result)
        mock_scm = AsyncMock(return_value=scm_result)

        with (
            patch.dict(runner._DISPATCH, {FSComponent: mock_fs, SCMComponent: mock_scm}),
            patch(
                "soliplex.agents.manifest.runner._list_scm_all_uris",
                new_callable=AsyncMock,
                return_value=full_scm_uris,
            ),
            patch(
                "soliplex.agents.manifest.runner.local_state.reconcile_documents",
                return_value=[],
            ) as mock_check,
        ):
            await runner.run_manifest(m)

        # FS inventory + full SCM listing (not partial incremental)
        assert mock_check.call_args[0][1] == {"local.md", "all1.md", "all2.md"}


# --- _list_scm_all_uris ---


class TestListSCMAllUris:
    @pytest.mark.asyncio
    async def test_calls_list_all_uris(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            components=[
                {"type": "scm", "name": "r", "platform": "github", "owner": "o", "repo": "r"},
            ],
        )
        component = manifest.components[0]
        with patch("soliplex.agents.scm.app.list_all_uris", new_callable=AsyncMock) as mock:
            mock.return_value = [{"uri": "f.md", "sha256": "h"}]
            result = await runner._list_scm_all_uris(component, manifest)
            mock.assert_called_once_with(
                component.platform,
                component.repo,
                owner=component.owner,
                branch=component.branch,
                content_filter=component.content_filter,
            )
        assert result == [{"uri": "f.md", "sha256": "h"}]

    @pytest.mark.asyncio
    async def test_with_credentials_and_extensions(self):
        manifest = Manifest(
            id="t",
            name="t",
            source="s",
            config={"extensions": ["rst"]},
            components=[
                {
                    "type": "scm",
                    "name": "r",
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "auth_token": "MY_TOKEN",
                    "base_url": "https://custom.api/v1",
                },
            ],
        )
        component = manifest.components[0]
        original_ext = settings.extensions[:]
        with (
            patch("soliplex.agents.scm.app.list_all_uris", new_callable=AsyncMock) as mock,
            patch("soliplex.agents.manifest.runner.resolve_credential", return_value="secret"),
        ):
            mock.return_value = []
            await runner._list_scm_all_uris(component, manifest)
            mock.assert_called_once()
        # Settings restored
        assert settings.extensions == original_ext


# --- one download target per installation ------------------------------------


def _src_manifest(source, **config):
    return Manifest(
        id=f"m-{source}",
        name=source,
        source=source,
        config=config or None,
        components=[{"type": "fs", "name": "c", "path": "/data"}],
    )


@pytest.fixture(params=["fs", "s3"])
def installation(request, tmp_path, monkeypatch):
    """An installation storing documents locally, or in (in-memory) S3."""
    from obstore.store import MemoryStore

    from soliplex.agents import store as agent_store

    shared = MemoryStore()
    monkeypatch.setattr(agent_store, "_make_s3_store", lambda bucket, options: shared)
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    monkeypatch.setattr(agent_store.settings, "download_s3_bucket", "s3://docs" if request.param == "s3" else None)
    agent_store.reset_store_cache()
    yield agent_store
    agent_store.reset_store_cache()


@pytest.mark.asyncio
async def test_run_manifest_ingests_into_the_installation_without_rewriting_settings(installation, monkeypatch):
    before = (installation.settings.download_dir, installation.settings.download_s3_bucket)
    seen = {}

    async def fake_component(component, manifest, metadata):
        # Resolve the store the way an agent does: from the live settings.
        store = installation.get_document_store(manifest.source)
        await store.write("doc.md", b"x")
        seen["uri"] = store.target.base_uri
        seen["settings"] = (installation.settings.download_dir, installation.settings.download_s3_bucket)
        return {"ingested": ["doc.md"], "inventory": []}

    monkeypatch.setitem(runner._DISPATCH, FSComponent, fake_component)
    manifest = _src_manifest("src")
    await runner.run_manifest(manifest)

    assert seen["uri"] == manifest.get_download_target().base_uri
    # Nothing is rewritten mid-run any more, so nothing running alongside can see a change.
    assert seen["settings"] == before


@pytest.mark.asyncio
async def test_the_load_and_its_callbacks_read_where_the_run_wrote(installation, monkeypatch):
    """The regression for the old override bug: one place, end to end."""
    from soliplex.agents.config import PostProcessStep
    from soliplex.agents.manifest import haiku_loader
    from soliplex.agents.manifest import post_process

    written = {}
    callback = {}

    async def fake_component(component, manifest, metadata):
        store = installation.get_document_store(manifest.source)
        await store.write("doc.md", b"x")
        written["uri"] = store.target.base_uri
        return {"ingested": ["doc.md"], "inventory": []}

    def step(source, *, context):
        callback["uri"] = context.download_uri

    monkeypatch.setitem(runner._DISPATCH, FSComponent, fake_component)
    monkeypatch.setattr(post_process, "_resolve_method", lambda spec: step)
    monkeypatch.setattr(haiku_loader, "resolve_haiku_cfg", lambda manifest: "/cfg.yaml")
    monkeypatch.setattr(post_process, "resolve_haiku_cfg", lambda manifest: "/cfg.yaml")
    monkeypatch.setattr(haiku_loader.settings, "lancedb_dir", "/lance", raising=False)
    manifest = _src_manifest("src", post_process=[PostProcessStep(method="pkg:step")])

    await runner.run_manifest(manifest)
    proc = MagicMock(returncode=0, wait=AsyncMock(), stdout=MagicMock(), stderr=MagicMock())
    proc.stdout.read = proc.stderr.read = AsyncMock(return_value=b"")
    with patch("soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as ex:
        await haiku_loader.run_load(manifest)

    assert ex.call_args.kwargs["env"]["DOWNLOAD_URI"] == written["uri"]
    assert callback["uri"] == written["uri"]


@pytest.mark.asyncio
async def test_a_load_during_another_manifests_ingest_keeps_its_own_location(installation, monkeypatch):
    """What used to race: B's ingest rewrote the settings A's load then read."""
    from soliplex.agents.manifest.context import LoadContext

    ingesting = asyncio.Event()
    release = asyncio.Event()

    async def slow_component(component, manifest, metadata):
        ingesting.set()
        await release.wait()
        return {"ingested": [], "inventory": []}

    monkeypatch.setitem(runner._DISPATCH, FSComponent, slow_component)
    ingest_b = asyncio.create_task(runner.run_manifest(_src_manifest("b")))
    await asyncio.wait_for(ingesting.wait(), timeout=5)
    during = LoadContext.for_source("a").download_uri
    release.set()
    await ingest_b

    assert during == _src_manifest("a").get_download_target().base_uri


# --- spans ---


class TestComponentSpans:
    @pytest.mark.asyncio
    async def test_clean_component_span_carries_counts(self, spans):
        handler = AsyncMock(return_value={"ingested": ["a", "b"], "errors": [], "not_found": ["c"], "rejected": []})
        with patch.dict(runner._DISPATCH, {FSComponent: handler}):
            await runner.run_manifest(_two_component_manifest())

        wiki, notes = spans.named("component")
        assert wiki.attributes["logfire.msg"] == "component wiki (fs)"
        assert wiki.attributes["component.name"] == "wiki"
        assert wiki.attributes["component.type"] == "fs"
        assert wiki.attributes["manifest.id"] == "docs-site"
        assert wiki.attributes["component.ingested"] == 2
        assert wiki.attributes["component.errors"] == 0
        assert wiki.attributes["component.not_found"] == 1
        assert wiki.attributes["component.rejected"] == 0
        assert wiki.status.status_code is StatusCode.UNSET
        assert notes.attributes["component.name"] == "notes"

    @pytest.mark.asyncio
    async def test_file_errors_fail_the_component_span(self, spans):
        failed = {"ingested": [], "errors": [{"uri": u, "error": "502"} for u in ("x", "y", "z")]}
        handler = AsyncMock(side_effect=[failed, {"ingested": [], "errors": []}])
        with patch.dict(runner._DISPATCH, {FSComponent: handler}):
            await runner.run_manifest(_two_component_manifest())

        wiki, notes = spans.named("component")
        assert wiki.status.status_code is StatusCode.ERROR
        assert wiki.status.description == "3 file errors"
        assert wiki.attributes["component.errors"] == 3
        assert notes.status.status_code is StatusCode.UNSET

    @pytest.mark.asyncio
    async def test_a_raised_component_fails_its_span_and_the_next_still_runs(self, spans):
        handler = AsyncMock(side_effect=[RuntimeError("boom"), {"ingested": [], "errors": []}])
        with patch.dict(runner._DISPATCH, {FSComponent: handler}):
            await runner.run_manifest(_two_component_manifest())

        wiki, notes = spans.named("component")
        assert wiki.status.description == "component failed: RuntimeError"
        assert wiki.events[0].name == "exception"
        assert notes.status.status_code is StatusCode.UNSET

    @pytest.mark.asyncio
    async def test_unknown_component_type_fails_its_span(self, spans):
        m = _two_component_manifest()
        m.components[0] = MagicMock()
        m.components[0].name = "mystery"
        with patch.dict(runner._DISPATCH, {FSComponent: AsyncMock(return_value={"errors": []})}):
            await runner.run_manifest(m)

        mystery = spans.named("component")[0]
        assert mystery.attributes["component.type"] == "MagicMock"
        assert mystery.status.description == "unknown component type"

    @pytest.mark.asyncio
    async def test_delete_stale_span(self, spans):
        handler = AsyncMock(return_value={"inventory": [{"path": "keep.md", "sha256": "1"}], "errors": []})
        with (
            patch.dict(runner._DISPATCH, {FSComponent: handler}),
            patch("soliplex.agents.local_state.reconcile_documents", AsyncMock(return_value=["old.md"])),
        ):
            await runner.run_manifest(_two_component_manifest(delete_stale=True))

        (stale,) = spans.named("delete stale")
        assert stale.attributes["logfire.msg"] == "delete stale docs-src"
        assert stale.attributes["manifest.deleted"] == 1

    @pytest.mark.asyncio
    async def test_list_scm_uris_span(self, spans):
        m = Manifest(
            id="t",
            name="t",
            source="s",
            config={"delete_stale": True},
            components=[
                {"type": "scm", "name": "r", "platform": "github", "owner": "o", "repo": "r", "incremental": True},
            ],
        )
        with (
            patch.dict(runner._DISPATCH, {SCMComponent: AsyncMock(return_value={"ingested": [], "errors": []})}),
            patch("soliplex.agents.manifest.runner._list_scm_all_uris", AsyncMock(return_value=[])),
            patch("soliplex.agents.manifest.runner.local_state.reconcile_documents", AsyncMock(return_value=[])),
        ):
            await runner.run_manifest(m)

        (listing,) = spans.named("list scm uris")
        assert listing.attributes["component.name"] == "r"
        assert listing.attributes["manifest.id"] == "t"


class TestRunManifestsSpans:
    """The CLI path opens the same manifest span the server's queue does."""

    @staticmethod
    def _write(tmp_path, *ids):
        for mid in ids:
            (tmp_path / f"{mid}.yml").write_text(
                f"id: {mid}\nname: M{mid}\nsource: src-{mid}\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n"
            )

    @pytest.mark.asyncio
    async def test_one_span_per_manifest_with_its_outcome(self, tmp_path, spans):
        self._write(tmp_path, "a", "b")
        failed = {"manifest_id": "b", "results": [], "summary": {"component_errors": 0, "file_errors": 2}}
        clean = {"manifest_id": "a", "results": [], "summary": {"component_errors": 0, "file_errors": 0}}
        with patch("soliplex.agents.manifest.runner.run_manifest", AsyncMock(side_effect=[clean, failed])):
            await runner.run_manifests(str(tmp_path))

        a, b = spans.named("manifest run")
        assert a.attributes["manifest.id"] == "a"
        assert a.attributes["manifest.source"] == "src-a"
        assert a.status.status_code is StatusCode.UNSET
        assert b.attributes["manifest.file_errors"] == 2
        assert b.status.status_code is StatusCode.ERROR

    @pytest.mark.asyncio
    async def test_a_raised_run_fails_its_span(self, tmp_path, spans):
        self._write(tmp_path, "a")
        with patch("soliplex.agents.manifest.runner.run_manifest", AsyncMock(side_effect=RuntimeError("boom"))):
            await runner.run_manifests(str(tmp_path))

        (run,) = spans.named("manifest run")
        assert run.status.description == "manifest run failed: RuntimeError"

    @pytest.mark.asyncio
    async def test_a_failed_haiku_load_fails_the_span(self, tmp_path, spans):
        self._write(tmp_path, "a")
        with (
            patch(
                "soliplex.agents.manifest.runner.run_manifest",
                AsyncMock(return_value={"manifest_id": "a", "results": [], "summary": {}}),
            ),
            patch("soliplex.agents.manifest.haiku_loader.run_load", AsyncMock(side_effect=OSError("no binary"))),
        ):
            await runner.run_manifests(str(tmp_path), load=True)

        (run,) = spans.named("manifest run")
        assert run.status.description == "haiku load failed: OSError"
