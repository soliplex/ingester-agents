"""Tests for the shared span helpers — 100% branch coverage required."""

import pytest
from opentelemetry.trace import StatusCode

from soliplex.agents import telemetry
from soliplex.agents.config import Manifest


def _manifest():
    return Manifest(
        id="docs-site",
        name="Docs",
        source="docs-src",
        components=[
            {"type": "fs", "name": "a", "path": "/a"},
            {"type": "fs", "name": "b", "path": "/b"},
        ],
    )


class TestSpan:
    def test_sets_message_and_drops_none_attributes(self, spans):
        with telemetry.span("component", "component wiki (fs)", {"component.name": "wiki", "gone": None}):
            pass
        (span,) = spans.named("component")
        assert span.attributes["logfire.msg"] == "component wiki (fs)"
        assert span.attributes["component.name"] == "wiki"
        assert "gone" not in span.attributes
        assert span.status.status_code is StatusCode.UNSET

    def test_without_attributes(self, spans):
        with telemetry.span("x", "x message"):
            pass
        assert spans.named("x")[0].attributes == {"logfire.msg": "x message"}

    def test_an_escaping_exception_fails_the_span(self, spans):
        with pytest.raises(RuntimeError), telemetry.span("x", "x"):
            raise RuntimeError("boom")
        (span,) = spans.named("x")
        assert span.status.status_code is StatusCode.ERROR
        assert span.events[0].name == "exception"


class TestFail:
    def test_with_exception(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.fail(span, "it broke", ValueError("bad"))
        (finished,) = spans.named("x")
        assert finished.status.status_code is StatusCode.ERROR
        assert finished.status.description == "it broke"
        assert finished.events[0].attributes["exception.type"] == "ValueError"

    def test_without_exception(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.fail(span, "3 file errors")
        (finished,) = spans.named("x")
        assert finished.status.description == "3 file errors"
        assert finished.events == ()


class TestManifestSpan:
    def test_name_message_and_attributes(self, spans):
        with telemetry.manifest_span("docs-site", {telemetry.MANIFEST_PATH: "/m/docs.yml"}) as span:
            telemetry.describe_manifest(span, _manifest())
        (finished,) = spans.named("manifest run")
        assert finished.attributes["logfire.msg"] == "manifest docs-site"
        assert finished.attributes[telemetry.MANIFEST_ID] == "docs-site"
        assert finished.attributes[telemetry.MANIFEST_PATH] == "/m/docs.yml"
        assert finished.attributes[telemetry.MANIFEST_NAME] == "Docs"
        assert finished.attributes[telemetry.MANIFEST_SOURCE] == "docs-src"
        assert finished.attributes[telemetry.MANIFEST_COMPONENTS] == 2

    def test_without_extra_attributes(self, spans):
        with telemetry.manifest_span("m"):
            pass
        assert spans.named("manifest run")[0].attributes[telemetry.MANIFEST_ID] == "m"


class TestRecordSummary:
    def test_clean_run_copies_counts_and_stays_ok(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.record_summary(span, {"components": 2, "component_errors": 0, "file_errors": 0, "ingested": 5})
        (finished,) = spans.named("x")
        assert finished.attributes["manifest.components"] == 2
        assert finished.attributes["manifest.ingested"] == 5
        assert finished.status.status_code is StatusCode.UNSET

    @pytest.mark.parametrize(
        "component_errors, file_errors, description",
        [
            (1, 0, "1 component errors, 0 file errors"),
            (0, 3, "0 component errors, 3 file errors"),
        ],
    )
    def test_any_failure_fails_the_span(self, spans, component_errors, file_errors, description):
        summary = {"component_errors": component_errors, "file_errors": file_errors}
        with telemetry.span("x", "x") as span:
            telemetry.record_summary(span, summary)
        (finished,) = spans.named("x")
        assert finished.status.status_code is StatusCode.ERROR
        assert finished.status.description == description

    def test_empty_summary(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.record_summary(span, {})
        assert spans.named("x")[0].status.status_code is StatusCode.UNSET
