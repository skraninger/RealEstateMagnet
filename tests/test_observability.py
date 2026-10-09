"""Offline tests for the local file span processor (no network, no backends)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from modules.community.observability import FileSpanProcessor  # noqa: E402


def _read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestFileSpanProcessor:
    def test_writes_nested_spans_as_jsonl(self, tmp_path):
        trace_path = tmp_path / "traces.jsonl"
        provider = TracerProvider()
        provider.add_span_processor(FileSpanProcessor(trace_path))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("agent run") as parent:
            parent.set_attribute("gen_ai.system", "openai")
            parent.set_attribute("tags", ["a", "b"])
            with tracer.start_as_current_span("model request") as child:
                child.add_event("tool_call", {"tool": "web_search"})
        provider.shutdown()

        records = _read_records(trace_path)
        by_name = {r["name"]: r for r in records}
        assert set(by_name) == {"agent run", "model request"}

        run = by_name["agent run"]
        assert run["trace_id"] and run["span_id"]
        assert run["parent_span_id"] is None
        assert run["attributes"]["gen_ai.system"] == "openai"
        assert run["attributes"]["tags"] == ["a", "b"]
        assert run["duration_ms"] >= 0
        assert run["start_time"] and run["end_time"]

        request = by_name["model request"]
        assert request["trace_id"] == run["trace_id"]
        assert request["parent_span_id"] == run["span_id"]
        assert request["events"][0]["name"] == "tool_call"
        assert request["events"][0]["attributes"]["tool"] == "web_search"

    def test_appends_across_processors(self, tmp_path):
        trace_path = tmp_path / "traces.jsonl"
        for _ in range(2):
            provider = TracerProvider()
            provider.add_span_processor(FileSpanProcessor(trace_path))
            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("run"):
                pass
            provider.shutdown()
        assert len(_read_records(trace_path)) == 2

    def test_shutdown_is_idempotent(self, tmp_path):
        processor = FileSpanProcessor(tmp_path / "t.jsonl")
        processor.shutdown()
        processor.shutdown()  # must not raise
        assert processor.force_flush() is True


class TestSetupFileTracing:
    def test_configures_local_logfire_with_our_processor(self, tmp_path, monkeypatch):
        import logfire

        from modules.community import observability

        calls: dict = {}
        monkeypatch.setattr(observability, "_configured", False)
        monkeypatch.setattr(logfire, "configure", lambda **kw: calls.update(kw))
        monkeypatch.setattr(
            logfire,
            "instrument_pydantic_ai",
            lambda: calls.setdefault("instrumented", True),
        )

        processor = observability.setup_file_tracing(
            tmp_path / "t.jsonl", service_name="svc"
        )

        assert calls["send_to_logfire"] is False  # never leaves the machine
        assert calls["console"] is False
        assert calls["service_name"] == "svc"
        assert calls["additional_span_processors"] == [processor]
        assert calls["instrumented"] is True
        processor.shutdown()
