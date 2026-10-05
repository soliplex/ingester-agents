"""Tests for haiku-rag maintenance verbs — 100% branch coverage required."""

import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from soliplex.agents.config import Manifest
from soliplex.agents.config import ManifestConfig
from soliplex.agents.config import settings
from soliplex.agents.manifest import haiku_maint

_EXEC = "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec"
_TIMEOUT = "soliplex.agents.manifest.haiku_process.asyncio.timeout"


def _manifest(source="src", manifest_id="m", haiku_config=None):
    config = ManifestConfig(haiku_config=haiku_config) if haiku_config is not None else None
    return Manifest(
        id=manifest_id,
        name="M",
        source=source,
        config=config,
        components=[{"type": "fs", "name": "c", "path": "/data"}],
    )


class _FakeStream:
    """Minimal stand-in for asyncio.StreamReader: one chunk per read, then EOF."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


def _fake_proc(returncode=0, stdout_lines=(b"ok\n",), stderr_lines=()):
    proc = MagicMock()
    proc.stdout = _FakeStream(stdout_lines)
    proc.stderr = _FakeStream(stderr_lines)
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class _RaisingTimeout:
    """asyncio.timeout stand-in that trips immediately on entry."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        raise TimeoutError

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def maint_env(monkeypatch):
    """Set the haiku settings the maintenance verbs read."""
    monkeypatch.setattr(settings, "haiku_path", "/opt/haiku", raising=False)
    monkeypatch.setattr(settings, "lancedb_dir", "/data/lance", raising=False)
    monkeypatch.setattr(settings, "haiku_default_config", "haiku.rag.default.yaml", raising=False)
    monkeypatch.setattr(
        settings,
        "haiku_maintenance_command",
        "haiku-rag --config={haiku_cfg} {verb} --db={db}",
        raising=False,
    )
    monkeypatch.setattr(settings, "haiku_maintenance_timeout", 3600, raising=False)
    monkeypatch.setattr(settings, "haiku_load_cwd", None, raising=False)
    monkeypatch.setattr(settings, "download_dir", "downloads", raising=False)
    monkeypatch.setattr(settings, "logfire_token", None, raising=False)


# --- build_maintenance_argv ---


class TestBuildMaintenanceArgv:
    def test_substitutes_verb_config_and_db(self, maint_env):
        argv = haiku_maint.build_maintenance_argv("vacuum", "/etc/haiku/h.yaml", "/data/lance/src.lancedb", "src")
        assert argv == [
            "haiku-rag",
            "--config=/etc/haiku/h.yaml",
            "vacuum",
            "--db=/data/lance/src.lancedb",
        ]

    def test_migrate_verb(self, maint_env):
        argv = haiku_maint.build_maintenance_argv("migrate", "/etc/h.yaml", "/db", "src")
        assert "migrate" in argv

    def test_omits_config_token_when_cfg_is_none(self, maint_env):
        argv = haiku_maint.build_maintenance_argv("vacuum", None, "/db", "src")
        assert argv == ["haiku-rag", "vacuum", "--db=/db"]

    def test_value_with_spaces_stays_one_token(self, maint_env):
        argv = haiku_maint.build_maintenance_argv("vacuum", "/etc/my haiku/h.yaml", "/db", "src")
        assert "--config=/etc/my haiku/h.yaml" in argv

    def test_extra_placeholders_available(self, monkeypatch, maint_env):
        monkeypatch.setattr(
            settings,
            "haiku_maintenance_command",
            "hr {verb} {source} {lancedb_dir} {haiku_path}",
            raising=False,
        )
        argv = haiku_maint.build_maintenance_argv("vacuum", None, "/db", "composite source")
        assert argv == ["hr", "vacuum", "composite-source", "/data/lance", "/opt/haiku"]

    def test_unset_dirs_render_empty(self, monkeypatch, maint_env):
        monkeypatch.setattr(settings, "lancedb_dir", None, raising=False)
        monkeypatch.setattr(settings, "haiku_path", None, raising=False)
        monkeypatch.setattr(settings, "haiku_maintenance_command", "hr {lancedb_dir} {haiku_path}", raising=False)
        assert haiku_maint.build_maintenance_argv("vacuum", None, "/db", "src") == ["hr", "", ""]


# --- run_verb ---


@pytest.mark.asyncio
class TestRunVerb:
    async def test_dry_run_does_not_spawn(self, maint_env):
        with patch(_EXEC, new_callable=AsyncMock) as mock_exec:
            result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg="/etc/h.yaml", dry_run=True)
        mock_exec.assert_not_called()
        assert result["dry_run"] is True
        assert result["argv"] == ["haiku-rag", "--config=/etc/h.yaml", "vacuum", f"--db={result['db']}"]
        # shlex.join quotes only what needs it (a Windows db path gets quoted
        # because of its backslashes; a posix one does not).
        assert result["command"].startswith("haiku-rag --config=/etc/h.yaml vacuum ")
        assert result["timeout"] == 3600
        assert "returncode" not in result

    async def test_dry_run_quotes_paths_with_spaces(self, maint_env):
        result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg="/etc/my haiku/h.yaml", dry_run=True)
        assert "'--config=/etc/my haiku/h.yaml'" in result["command"]

    async def test_success_logs_output_in_parts_and_reports(self, maint_env, caplog):
        proc = _fake_proc(returncode=0, stdout_lines=[b"line1\n"], stderr_lines=[b"warn\n"])
        with caplog.at_level(logging.INFO), patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
            result = await haiku_maint.run_verb("src", "migrate", haiku_cfg="/etc/h.yaml")

        assert result["returncode"] == 0
        assert result["timed_out"] is False
        assert result["stdout"] == "line1"
        assert result["stderr"] == "warn"
        assert result["db"].replace("\\", "/").endswith("src.lancedb")
        # Output is logged in one part per stream, not one record per line.
        assert "haiku migrate src stdout output part 1:\nline1" in caplog.text
        assert "haiku migrate for source 'src' completed" in caplog.text
        # cwd honours haiku_load_cwd (None here = inherit).
        assert mock_exec.call_args.kwargs["cwd"] is None

    async def test_env_carries_source_and_download_dir(self, maint_env):
        proc = _fake_proc()
        with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
            await haiku_maint.run_verb("gitea:admin:repo", "vacuum", haiku_cfg="/etc/h.yaml")

        env = mock_exec.call_args.kwargs["env"]
        # SOURCE is the sanitized download-folder name, as for a load.
        assert env["SOURCE"] == "gitea_admin_repo"
        assert env["DOWNLOAD_DIR"] == "downloads"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["OTEL_SERVICE_NAME"].endswith(".haiku-rag.vacuum.gitea:admin:repo")
        assert "LOGFIRE_TOKEN" not in env

    async def test_env_includes_logfire_token_when_set(self, monkeypatch, maint_env):
        monkeypatch.setattr(settings, "logfire_token", SecretStr("tok"), raising=False)
        proc = _fake_proc()
        with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
            await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None)

        assert mock_exec.call_args.kwargs["env"]["LOGFIRE_TOKEN"] == "tok"

    async def test_cwd_from_settings(self, monkeypatch, maint_env):
        monkeypatch.setattr(settings, "haiku_load_cwd", "/var/lib/ingester", raising=False)
        proc = _fake_proc()
        with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
            await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None)

        assert mock_exec.call_args.kwargs["cwd"] == "/var/lib/ingester"

    async def test_nonzero_exit_is_reported_not_raised(self, maint_env, caplog):
        proc = _fake_proc(returncode=2, stdout_lines=[], stderr_lines=[b"boom\n"])
        with caplog.at_level(logging.ERROR), patch(_EXEC, new_callable=AsyncMock, return_value=proc):
            result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None)

        assert result["returncode"] == 2
        assert result["timed_out"] is False
        assert "failed (rc=2)" in caplog.text
        assert "last stderr:\nboom" in caplog.text

    async def test_negative_exit_logs_signal(self, maint_env, caplog):
        proc = _fake_proc(returncode=-9)
        with caplog.at_level(logging.ERROR), patch(_EXEC, new_callable=AsyncMock, return_value=proc):
            result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None)

        assert result["returncode"] == -9
        assert "memory limit" in caplog.text

    async def test_timeout_kills_and_reports(self, maint_env, caplog):
        proc = _fake_proc()
        with (
            caplog.at_level(logging.ERROR),
            patch(_EXEC, new_callable=AsyncMock, return_value=proc),
            patch(_TIMEOUT, _RaisingTimeout),
        ):
            result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None, timeout=5)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
        assert result["returncode"] is None
        assert result["timed_out"] is True
        assert result["stdout"] == ""
        assert "timed out after 5s" in caplog.text

    async def test_explicit_timeout_overrides_setting(self, maint_env):
        proc = _fake_proc()
        with patch(_EXEC, new_callable=AsyncMock, return_value=proc):
            result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None, timeout=60)

        assert result["timeout"] == 60

    async def test_raises_when_lancedb_dir_unset(self, monkeypatch, maint_env):
        monkeypatch.setattr(settings, "lancedb_dir", None, raising=False)
        with pytest.raises(ValueError, match="LANCEDB_DIR"):
            await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None)


# --- backfill-metadata ---


def test_build_backfill_argv_runs_this_interpreter():
    import sys

    argv = haiku_maint.build_backfill_argv(
        "/etc/my haiku/h.yaml",
        missing=["page_count", "pdf_title"],
        content_types=["application/pdf"],
        doc_filter="uri LIKE '%x y%'",
        db_name="db",
        batch_size=50,
        check=True,
    )
    assert argv == [
        sys.executable,
        "-m",
        "soliplex.agents.haiku_backfill",
        "--config=/etc/my haiku/h.yaml",
        "--db-name=db",
        "--missing=page_count",
        "--missing=pdf_title",
        "--content-type=application/pdf",
        "--filter=uri LIKE '%x y%'",
        "--batch-size=50",
        "--check",
    ]


def test_build_backfill_argv_without_config_or_options():
    assert haiku_maint.build_backfill_argv(None)[3:] == []


def test_build_backfill_argv_without_attachments():
    assert haiku_maint.build_backfill_argv(None, attachments=False)[3:] == ["--no-attachments"]


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ('noise\nBACKFILL_SUMMARY {"updated": 1}\n', {"updated": 1}),
        ('BACKFILL_SUMMARY {"updated": 1}\nBACKFILL_SUMMARY {"updated": 2}', {"updated": 2}),
        ("BACKFILL_SUMMARY {not json", None),
        ("no summary here", None),
        ("", None),
    ],
)
def test_parse_backfill_summary(stdout, expected):
    assert haiku_maint.parse_backfill_summary(stdout) == expected


@pytest.mark.asyncio
class TestBackfillVerb:
    async def test_dry_run_builds_the_backfill_command(self, maint_env):
        result = await haiku_maint.run_verb(
            "src", "backfill-metadata", haiku_cfg="/etc/h.yaml", dry_run=True, options={"missing": ["page_count"]}
        )
        assert result["argv"][1:] == ["-m", "soliplex.agents.haiku_backfill", "--config=/etc/h.yaml", "--missing=page_count"]
        assert result["dry_run"] is True

    async def test_run_reports_the_summary(self, maint_env):
        proc = _fake_proc(returncode=0, stdout_lines=[b'INFO ...\nBACKFILL_SUMMARY {"updated": 4}\n'])
        with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
            result = await haiku_maint.run_verb("src", "backfill-metadata", haiku_cfg="/etc/h.yaml")
        assert result["summary"] == {"updated": 4}
        assert mock_exec.call_args.args[1:3] == ("-m", "soliplex.agents.haiku_backfill")
        assert mock_exec.call_args.kwargs["env"]["SOURCE"] == "src"

    async def test_other_verbs_have_no_summary(self, maint_env):
        with patch(_EXEC, new_callable=AsyncMock, return_value=_fake_proc()):
            result = await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None)
        assert "summary" not in result

    async def test_other_verbs_take_no_options(self, maint_env):
        with pytest.raises(ValueError, match="haiku vacuum takes no options"):
            await haiku_maint.run_verb("src", "vacuum", haiku_cfg=None, options={"missing": ["x"]})

    async def test_run_maintenance_passes_options(self, tmp_path, maint_env):
        path = tmp_path / "m.yml"
        path.write_text("id: m\nname: M\nsource: src\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n")
        results = await haiku_maint.run_maintenance(
            "backfill-metadata", str(path), dry_run=True, options={"content_types": ["application/pdf"]}
        )
        assert results[0]["argv"][-1] == "--content-type=application/pdf"


# --- plan_targets ---


class TestPlanTargets:
    def test_resolves_each_manifest(self, maint_env):
        entries = haiku_maint.plan_targets("vacuum", [_manifest(source="a"), _manifest(source="b", manifest_id="m2")])
        assert [e["manifest"].source for e in entries] == ["a", "b"]
        assert all(e["haiku_cfg"].endswith("haiku.rag.default.yaml") for e in entries)

    def test_manifest_override_used_for_cfg(self, maint_env):
        entries = haiku_maint.plan_targets("vacuum", [_manifest(haiku_config="/abs/custom.yaml")])
        assert entries[0]["haiku_cfg"].replace("\\", "/") == "/abs/custom.yaml"

    def test_duplicate_db_is_skipped(self, maint_env, caplog):
        manifests = [_manifest(source="same"), _manifest(source="same", manifest_id="m2")]
        with caplog.at_level(logging.INFO):
            entries = haiku_maint.plan_targets("vacuum", manifests)

        assert "manifest" in entries[0]
        assert entries[1]["report"]["skipped"] == "duplicate-db"
        assert entries[1]["report"]["manifest_id"] == "m2"
        assert "already handled" in caplog.text

    def test_same_source_different_cfg_both_run(self, maint_env):
        manifests = [
            _manifest(source="same", haiku_config="/abs/one.yaml"),
            _manifest(source="same", manifest_id="m2", haiku_config="/abs/two.yaml"),
        ]
        entries = haiku_maint.plan_targets("vacuum", manifests)
        assert all("manifest" in e for e in entries)

    def test_resolution_failure_becomes_report(self, monkeypatch, maint_env, caplog):
        monkeypatch.setattr(settings, "lancedb_dir", None, raising=False)
        with caplog.at_level(logging.ERROR):
            entries = haiku_maint.plan_targets("vacuum", [_manifest()])

        assert "LANCEDB_DIR" in entries[0]["report"]["error"]
        assert entries[0]["report"]["verb"] == "vacuum"
        assert "Cannot vacuum" in caplog.text


# --- run_maintenance ---


@pytest.mark.asyncio
class TestRunMaintenance:
    async def test_runs_single_file(self, tmp_path, maint_env):
        path = tmp_path / "m.yml"
        path.write_text(
            "id: m\nname: M\nsource: src\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n",
        )
        proc = _fake_proc()
        with patch(_EXEC, new_callable=AsyncMock, return_value=proc):
            results = await haiku_maint.run_maintenance("vacuum", str(path))

        assert len(results) == 1
        assert results[0]["manifest_id"] == "m"
        assert results[0]["verb"] == "vacuum"
        assert results[0]["returncode"] == 0

    async def test_uses_the_installation_download_target(self, tmp_path, monkeypatch, maint_env):
        """DOWNLOAD_URI is where the run wrote: the installation's store.

        Storage is chosen per installation, so there's no per-manifest target
        to resolve and no settings are rewritten around the verb.
        """
        from soliplex.agents import store as agent_store

        monkeypatch.setattr(settings, "download_s3_bucket", "s3://bucket/ingester", raising=False)
        monkeypatch.setattr(agent_store, "_make_s3_store", lambda bucket, options: None)
        agent_store.reset_store_cache()
        path = tmp_path / "m.yml"
        path.write_text("id: m\nname: M\nsource: src\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n")
        seen = {}

        async def capture(*argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _fake_proc()

        try:
            with patch(_EXEC, new_callable=AsyncMock, side_effect=capture):
                await haiku_maint.run_maintenance("vacuum", str(path))
        finally:
            agent_store.reset_store_cache()

        assert seen["env"]["DOWNLOAD_URI"] == "s3://bucket/ingester/downloads/src"
        assert settings.download_s3_bucket == "s3://bucket/ingester"

    async def test_all_uses_manifest_dir(self, tmp_path, monkeypatch, maint_env):
        for name, source in (("a.yml", "sa"), ("b.yaml", "sb")):
            (tmp_path / name).write_text(
                f"id: {source}\nname: M\nsource: {source}\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n",
            )
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        with patch(_EXEC, new_callable=AsyncMock, side_effect=lambda *a, **k: _fake_proc()):
            results = await haiku_maint.run_maintenance("migrate")

        assert [r["source"] for r in results] == ["sa", "sb"]

    async def test_runs_sequentially(self, tmp_path, monkeypatch, maint_env):
        for name in ("a.yml", "b.yml"):
            (tmp_path / name).write_text(
                f"id: {name}\nname: M\nsource: {name}\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n",
            )
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        concurrent = 0
        peak = 0

        async def _fake_run_verb(source, verb, **kwargs):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            concurrent -= 1
            return {"returncode": 0, "timed_out": False}

        with patch.object(haiku_maint, "run_verb", new=_fake_run_verb):
            await haiku_maint.run_maintenance("vacuum")

        assert peak == 1

    async def test_reports_are_interleaved_in_manifest_order(self, tmp_path, monkeypatch, maint_env):
        # a.yml and c.yml share a source; the duplicate report must stay in
        # position rather than being appended after every successful run.
        (tmp_path / "a.yml").write_text(
            "id: a\nname: M\nsource: dup\ncomponents:\n  - type: fs\n    name: c\n    path: /d\n",
        )
        (tmp_path / "b.yml").write_text(
            "id: b\nname: M\nsource: other\ncomponents:\n  - type: fs\n    name: c\n    path: /d\n",
        )
        (tmp_path / "c.yml").write_text(
            "id: c\nname: M\nsource: dup\ncomponents:\n  - type: fs\n    name: c\n    path: /d\n",
        )
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        with patch(_EXEC, new_callable=AsyncMock, side_effect=lambda *a, **k: _fake_proc()):
            results = await haiku_maint.run_maintenance("vacuum")

        assert [r["manifest_id"] for r in results] == ["a", "b", "c"]
        assert results[2]["skipped"] == "duplicate-db"

    async def test_dry_run_spawns_nothing(self, tmp_path, monkeypatch, maint_env):
        (tmp_path / "a.yml").write_text(
            "id: a\nname: M\nsource: src\ncomponents:\n  - type: fs\n    name: c\n    path: /d\n",
        )
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        with patch(_EXEC, new_callable=AsyncMock) as mock_exec:
            results = await haiku_maint.run_maintenance("vacuum", dry_run=True)

        mock_exec.assert_not_called()
        assert results[0]["dry_run"] is True
        assert results[0]["command"].startswith("haiku-rag ")
        assert "--config=" in results[0]["command"]
        assert results[0]["argv"][2] == "vacuum"

    async def test_subprocess_failure_is_isolated(self, tmp_path, monkeypatch, maint_env, caplog):
        for name, source in (("a.yml", "sa"), ("b.yml", "sb")):
            (tmp_path / name).write_text(
                f"id: {source}\nname: M\nsource: {source}\ncomponents:\n  - type: fs\n    name: c\n    path: /d\n",
            )
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        calls = []

        async def _flaky(source, verb, **kwargs):
            calls.append(source)
            if source == "sa":
                raise FileNotFoundError("haiku-rag not found")
            return {"returncode": 0, "timed_out": False}

        with caplog.at_level(logging.ERROR), patch.object(haiku_maint, "run_verb", new=_flaky):
            results = await haiku_maint.run_maintenance("vacuum")

        # The second manifest still ran.
        assert calls == ["sa", "sb"]
        assert results[0]["error"] == "haiku-rag not found"
        assert results[1]["returncode"] == 0
        assert "haiku vacuum failed for manifest 'sa'" in caplog.text

    async def test_resolution_failure_reported(self, tmp_path, monkeypatch, maint_env):
        (tmp_path / "a.yml").write_text(
            "id: a\nname: M\nsource: src\ncomponents:\n  - type: fs\n    name: c\n    path: /d\n",
        )
        monkeypatch.setattr(settings, "manifest_dir", str(tmp_path), raising=False)
        monkeypatch.setattr(settings, "lancedb_dir", None, raising=False)
        results = await haiku_maint.run_maintenance("vacuum")

        assert "LANCEDB_DIR" in results[0]["error"]

    async def test_all_without_manifest_dir_raises(self, monkeypatch, maint_env):
        monkeypatch.setattr(settings, "manifest_dir", None, raising=False)
        with pytest.raises(FileNotFoundError, match="MANIFEST_DIR"):
            await haiku_maint.run_maintenance("vacuum")
