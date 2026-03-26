"""Tests for manifest CLI — preflight command."""

import textwrap
from unittest.mock import AsyncMock
from unittest.mock import patch

from typer.testing import CliRunner

from soliplex.agents.manifest.cli import cli

runner = CliRunner()


def _write_manifest(tmp_path, extra_yaml=""):
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
        + extra_yaml
    )
    return str(f)


def _write_manifest_with_workflows(tmp_path, workflow_id="wf-1", param_set_id="ps-1"):
    f = tmp_path / "manifest.yml"
    f.write_text(
        textwrap.dedent(f"""\
        id: test-m
        name: Test Manifest
        source: test-source
        components:
          - type: fs
            name: comp1
            path: /data
        config:
          start_workflows: true
          workflow_definition_id: {workflow_id}
          param_set_id: {param_set_id}
        """)
    )
    return str(f)


class TestPreflightNoWorkflows:
    def test_no_config_block(self, tmp_path):
        path = _write_manifest(tmp_path)
        result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 0
        assert "start_workflows is not enabled" in result.output

    def test_start_workflows_false(self, tmp_path):
        path = _write_manifest(
            tmp_path,
            textwrap.dedent("""\
            config:
              start_workflows: false
            """),
        )
        result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 0
        assert "start_workflows is not enabled" in result.output


class TestPreflightFileErrors:
    def test_file_not_found(self):
        result = runner.invoke(cli, ["preflight", "/nonexistent/path.yml"])
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yml"
        f.write_text(":\n  - :\n  bad:\n    [unterminated")
        result = runner.invoke(cli, ["preflight", str(f)])
        assert result.exit_code == 1
        assert "Validation error:" in result.output

    def test_non_mapping_yaml(self, tmp_path):
        f = tmp_path / "list.yml"
        f.write_text("- item1\n- item2\n")
        result = runner.invoke(cli, ["preflight", str(f)])
        assert result.exit_code == 1
        assert "Validation error:" in result.output


class TestPreflightBothFound:
    def test_both_exist(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-abc", "ps-xyz")
        with patch("soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(return_value={"id": "wf-abc"})):
            with patch("soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(return_value={"id": "ps-xyz"})):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 0
        assert "OK" in result.output
        assert "wf-abc" in result.output
        assert "ps-xyz" in result.output


class TestPreflightWorkflowMissing:
    def test_workflow_missing(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-missing", "ps-xyz")
        with patch("soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(return_value=None)):
            with patch("soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(return_value={"id": "ps-xyz"})):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 1
        assert "MISSING" in result.output
        assert "wf-missing" in result.output
        assert "OK" in result.output
        assert "ps-xyz" in result.output


class TestPreflightParamSetMissing:
    def test_param_set_missing(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-abc", "ps-missing")
        with patch("soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(return_value={"id": "wf-abc"})):
            with patch("soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(return_value=None)):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 1
        assert "OK" in result.output
        assert "wf-abc" in result.output
        assert "MISSING" in result.output
        assert "ps-missing" in result.output


class TestPreflightBothMissing:
    def test_both_missing(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-gone", "ps-gone")
        with patch("soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(return_value=None)):
            with patch("soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(return_value=None)):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 1
        assert result.output.count("MISSING") == 2


class TestPreflightErrors:
    def test_workflow_raises(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-err", "ps-xyz")
        with patch(
            "soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(side_effect=RuntimeError("conn refused"))
        ):
            with patch("soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(return_value={"id": "ps-xyz"})):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 1
        assert "ERROR" in result.output
        assert "conn refused" in result.output
        assert "OK" in result.output

    def test_param_set_raises(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-abc", "ps-err")
        with patch("soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(return_value={"id": "wf-abc"})):
            with patch(
                "soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(side_effect=RuntimeError("timeout"))
            ):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 1
        assert "OK" in result.output
        assert "ERROR" in result.output
        assert "timeout" in result.output

    def test_both_raise(self, tmp_path):
        path = _write_manifest_with_workflows(tmp_path, "wf-err", "ps-err")
        with patch("soliplex.agents.manifest.cli.client.find_workflow", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with patch("soliplex.agents.manifest.cli.client.find_param_set", new=AsyncMock(side_effect=RuntimeError("bang"))):
                result = runner.invoke(cli, ["preflight", path])
        assert result.exit_code == 1
        assert result.output.count("ERROR") == 2
