"""Tests for the manifest CLI ``run`` command."""

import textwrap
from unittest.mock import AsyncMock
from unittest.mock import patch

from typer.testing import CliRunner

from soliplex.agents.manifest.cli import cli

runner = CliRunner()


def _write_manifest(tmp_path):
    f = tmp_path / "manifest.yml"
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
    return str(f)


class TestRun:
    def test_run_reports_results(self, tmp_path):
        path = _write_manifest(tmp_path)
        fake = [
            {
                "manifest_id": "test-m",
                "manifest_name": "Test Manifest",
                "results": [{"component": "comp1", "result": {"ingested": [1, 2], "errors": []}}],
                "delete_stale_result": None,
            }
        ]
        with patch("soliplex.agents.manifest.runner.run_manifests", new=AsyncMock(return_value=fake)):
            result = runner.invoke(cli, [path])
        assert result.exit_code == 0
        assert "Test Manifest" in result.output
        assert "comp1: 2 ingested, 0 errors" in result.output

    def test_run_reports_component_error(self, tmp_path):
        path = _write_manifest(tmp_path)
        fake = [
            {
                "manifest_id": "test-m",
                "manifest_name": "Test Manifest",
                "results": [{"component": "comp1", "error": "boom"}],
                "delete_stale_result": None,
            }
        ]
        with patch("soliplex.agents.manifest.runner.run_manifests", new=AsyncMock(return_value=fake)):
            result = runner.invoke(cli, [path])
        assert result.exit_code == 0
        assert "comp1: ERROR - boom" in result.output

    def test_run_json_output(self, tmp_path):
        path = _write_manifest(tmp_path)
        fake = [{"manifest_id": "test-m", "manifest_name": "Test Manifest", "results": []}]
        with patch("soliplex.agents.manifest.runner.run_manifests", new=AsyncMock(return_value=fake)):
            result = runner.invoke(cli, [path, "--json"])
        assert result.exit_code == 0
        assert '"manifest_id": "test-m"' in result.output

    def test_run_file_not_found(self):
        with patch(
            "soliplex.agents.manifest.runner.run_manifests",
            new=AsyncMock(side_effect=FileNotFoundError("Path not found: /nope")),
        ):
            result = runner.invoke(cli, ["/nope"])
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_run_validation_error(self, tmp_path):
        path = _write_manifest(tmp_path)
        with patch(
            "soliplex.agents.manifest.runner.run_manifests",
            new=AsyncMock(side_effect=ValueError("Duplicate manifest IDs")),
        ):
            result = runner.invoke(cli, [path])
        assert result.exit_code == 1
        assert "Validation error:" in result.output
