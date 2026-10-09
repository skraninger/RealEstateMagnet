"""Local, file-based OpenTelemetry tracing for PydanticAI agents.

This is the "no Docker, no cloud" observability option: a tiny
``SpanProcessor`` that appends every finished span as one JSON object per line
to a file. Each agent run becomes a trace, with nested spans for model requests
and tool calls, so you can answer "which model turn stalled?", "what did the
tool return?", and "how many tokens/latency".

Enable it for the pipeline with ``--trace-file logs/traces.jsonl`` (or
``PIPELINE_TRACE_FILE``). It configures Logfire locally
(``send_to_logfire=False``) purely as the PydanticAI instrumentation hook and
routes spans to :class:`FileSpanProcessor` instead of the Logfire cloud.

The JSONL is append-only, one record per span::

    {"name": "chat ...", "trace_id": "...", "span_id": "...",
     "parent_span_id": "...", "start_time": "...", "duration_ms": 123.4,
     "status": "OK", "attributes": {...}, "events": [...]}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

DEFAULT_TRACE_SERVICE_NAME = "realestatemagnet"

# Guard so repeated setup calls do not attach duplicate span processors.
_configured = False


def _ns_to_iso(ns: Optional[int]) -> Optional[str]:
    if not ns:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _hex(value: Optional[int], width: int) -> Optional[str]:
    return format(value, f"0{width}x") if value else None


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so an arbitrary attribute value can be serialized."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    """Turn a finished span into a flat, JSON-friendly record."""
    ctx = span.context
    parent = span.parent
    status = getattr(span.status, "status_code", None)

    start = span.start_time
    end = span.end_time
    duration_ms = (
        round((end - start) / 1e6, 3) if start is not None and end is not None else None
    )

    return {
        "name": span.name,
        "trace_id": _hex(ctx.trace_id, 32) if ctx else None,
        "span_id": _hex(ctx.span_id, 16) if ctx else None,
        "parent_span_id": _hex(parent.span_id, 16) if parent else None,
        "kind": getattr(span.kind, "name", None),
        "start_time": _ns_to_iso(start),
        "end_time": _ns_to_iso(end),
        "duration_ms": duration_ms,
        "status": getattr(status, "name", None),
        "scope": getattr(span.instrumentation_scope, "name", None),
        "attributes": _jsonable(dict(span.attributes or {})),
        "events": [
            {
                "name": event.name,
                "timestamp": _ns_to_iso(event.timestamp),
                "attributes": _jsonable(dict(event.attributes or {})),
            }
            for event in (span.events or [])
        ],
        "resource": _jsonable(dict(span.resource.attributes or {})) if span.resource else {},
    }


class FileSpanProcessor(SpanProcessor):
    """Append finished spans as JSON Lines to ``path`` (thread-safe).

    This is intentionally tiny: no batching, no network, no backend. Flush after
    every span by default so a crash still leaves a usable trace; raise
    ``flush_every`` for high-volume runs.
    """

    def __init__(self, path: Path | str, *, flush_every: int = 1) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = max(1, flush_every)
        self._lock = threading.Lock()
        self._since_flush = 0
        self._file = self.path.open("a", encoding="utf-8", newline="\n")

    # SpanProcessor API -------------------------------------------------------

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # noqa: D401
        """No-op; spans are written when they end."""

    def on_end(self, span: ReadableSpan) -> None:
        try:
            record = span_to_dict(span)
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover - never let tracing break the run
            return

        with self._lock:
            if self._file.closed:
                return
            self._file.write(line + "\n")
            self._since_flush += 1
            if self._since_flush >= self._flush_every:
                self._file.flush()
                self._since_flush = 0

    def force_flush(self, timeout_millis: Optional[int] = None) -> bool:  # noqa: ARG002
        with self._lock:
            if not self._file.closed:
                self._file.flush()
                self._since_flush = 0
        return True

    def shutdown(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.flush()
                self._file.close()


def setup_file_tracing(
    trace_file: Path | str,
    *,
    service_name: str = DEFAULT_TRACE_SERVICE_NAME,
    instrument_pydantic_ai: bool = True,
    flush_every: int = 1,
) -> FileSpanProcessor:
    """Route PydanticAI spans to a local JSONL file, with no cloud sending.

    Uses Logfire only as the PydanticAI instrumentation hook
    (``send_to_logfire=False``, console output disabled) and adds
    :class:`FileSpanProcessor` as an additional span processor.
    """
    global _configured

    import logfire

    processor = FileSpanProcessor(trace_file, flush_every=flush_every)

    if not _configured:
        # Logfire is used only as the PydanticAI instrumentation hook; nothing is
        # sent to the Logfire cloud and console span output is disabled.
        logfire.configure(
            send_to_logfire=False,
            console=False,
            service_name=service_name,
            additional_span_processors=[processor],
        )
        _configured = True
    if instrument_pydantic_ai:
        logfire.instrument_pydantic_ai()
    return processor
