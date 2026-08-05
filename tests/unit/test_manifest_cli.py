"""Tests for the manifest CLI ``run`` / ``migrate`` / ``vacuum`` commands."""

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
            result = runner.invoke(cli, ["run", path])
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
            result = runner.invoke(cli, ["run", path])
        assert result.exit_code == 0
        assert "comp1: ERROR - boom" in result.output

    def test_run_json_output(self, tmp_path):
        path = _write_manifest(tmp_path)
        fake = [{"manifest_id": "test-m", "manifest_name": "Test Manifest", "results": []}]
        with patch("soliplex.agents.manifest.runner.run_manifests", new=AsyncMock(return_value=fake)):
            result = runner.invoke(cli, ["run", path, "--json"])
        assert result.exit_code == 0
        assert '"manifest_id": "test-m"' in result.output

    def test_run_file_not_found(self):
        with patch(
            "soliplex.agents.manifest.runner.run_manifests",
            new=AsyncMock(side_effect=FileNotFoundError("Path not found: /nope")),
        ):
            result = runner.invoke(cli, ["run", "/nope"])
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_run_validation_error(self, tmp_path):
        path = _write_manifest(tmp_path)
        with patch(
            "soliplex.agents.manifest.runner.run_manifests",
            new=AsyncMock(side_effect=ValueError("Duplicate manifest IDs")),
        ):
            result = runner.invoke(cli, ["run", path])
        assert result.exit_code == 1
        assert "Validation error:" in result.output


_MAINT = "soliplex.agents.manifest.haiku_maint.run_maintenance"


def _ok(source="src", verb="vacuum", rc=0):
    return {
        "manifest_id": "m",
        "source": source,
        "verb": verb,
        "db": f"/data/lance/{source}.lancedb",
        "argv": ["haiku-rag", "vacuum", f"--db=/data/lance/{source}.lancedb"],
        "command": f"haiku-rag vacuum --db=/data/lance/{source}.lancedb",
        "timeout": 3600,
        "returncode": rc,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
    }


class TestMaintenance:
    def test_vacuum_reports_success(self):
        with patch(_MAINT, new=AsyncMock(return_value=[_ok()])) as mock:
            result = runner.invoke(cli, ["vacuum"])
        assert result.exit_code == 0
        assert "src: vacuum ok -> /data/lance/src.lancedb" in result.output
        # PATH defaults to the "all" sentinel.
        assert mock.call_args.args == ("vacuum", "all")
        assert mock.call_args.kwargs == {"timeout": None, "dry_run": False}

    def test_migrate_passes_verb_path_and_timeout(self, tmp_path):
        path = _write_manifest(tmp_path)
        with patch(_MAINT, new=AsyncMock(return_value=[_ok(verb="migrate")])) as mock:
            result = runner.invoke(cli, ["migrate", path, "--timeout", "60"])
        assert result.exit_code == 0
        assert mock.call_args.args == ("migrate", path)
        assert mock.call_args.kwargs["timeout"] == 60

    def test_nonzero_returncode_exits_1(self):
        with patch(_MAINT, new=AsyncMock(return_value=[_ok(rc=2)])):
            result = runner.invoke(cli, ["vacuum"])
        assert result.exit_code == 1
        assert "vacuum FAILED (rc=2)" in result.output

    def test_timeout_exits_1(self):
        timed_out = _ok() | {"returncode": None, "timed_out": True}
        with patch(_MAINT, new=AsyncMock(return_value=[timed_out])):
            result = runner.invoke(cli, ["vacuum"])
        assert result.exit_code == 1
        assert "vacuum TIMED OUT after 3600s" in result.output

    def test_resolution_error_exits_1(self):
        failed = {"manifest_id": "m", "source": "src", "verb": "vacuum", "error": "LANCEDB_DIR ... must be set"}
        with patch(_MAINT, new=AsyncMock(return_value=[failed])):
            result = runner.invoke(cli, ["vacuum"])
        assert result.exit_code == 1
        assert "src: vacuum ERROR - LANCEDB_DIR" in result.output

    def test_duplicate_skip_is_not_a_failure(self):
        skipped = {"manifest_id": "m2", "source": "src", "verb": "vacuum", "skipped": "duplicate-db"}
        with patch(_MAINT, new=AsyncMock(return_value=[_ok(), skipped])):
            result = runner.invoke(cli, ["vacuum"])
        assert result.exit_code == 0
        assert "src: skipped (duplicate db)" in result.output

    def test_dry_run_prints_command_lines_only(self):
        results = [
            _ok(source="a") | {"dry_run": True},
            {"manifest_id": "m2", "source": "b", "verb": "vacuum", "skipped": "duplicate-db"},
        ]
        with patch(_MAINT, new=AsyncMock(return_value=results)) as mock:
            result = runner.invoke(cli, ["vacuum", "--dry-run"])
        assert result.exit_code == 0
        assert mock.call_args.kwargs["dry_run"] is True
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert lines[0] == "haiku-rag vacuum --db=/data/lance/a.lancedb"
        # Non-commands are '#'-prefixed so the block stays paste-safe.
        assert lines[1].startswith("# b: skipped")

    def test_dry_run_exits_1_on_resolution_error(self):
        failed = {"manifest_id": "m", "source": "src", "verb": "vacuum", "error": "boom"}
        with patch(_MAINT, new=AsyncMock(return_value=[failed])):
            result = runner.invoke(cli, ["vacuum", "--dry-run"])
        assert result.exit_code == 1
        assert "# src: ERROR - boom" in result.output

    def test_json_output(self):
        with patch(_MAINT, new=AsyncMock(return_value=[_ok()])):
            result = runner.invoke(cli, ["vacuum", "--json"])
        assert result.exit_code == 0
        assert '"verb": "vacuum"' in result.output

    def test_path_not_found_exits_1(self):
        with patch(_MAINT, new=AsyncMock(side_effect=FileNotFoundError("MANIFEST_DIR must be set"))):
            result = runner.invoke(cli, ["vacuum"])
        assert result.exit_code == 1
        assert "Error: MANIFEST_DIR must be set" in result.output

    def test_duplicate_ids_exits_1(self):
        with patch(_MAINT, new=AsyncMock(side_effect=ValueError("Duplicate manifest IDs"))):
            result = runner.invoke(cli, ["migrate"])
        assert result.exit_code == 1
        assert "Validation error:" in result.output
