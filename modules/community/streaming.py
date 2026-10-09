"""Stream a PydanticAI agent's reasoning ("thinking") to the console.

llama.cpp/Qwen exposes chain-of-thought either as a separate ``reasoning_content``
delta (with ``--reasoning-format deepseek``) or as inline `` thinking...`` tags in
the message content. PydanticAI maps both to ``ThinkingPart``s, and its streaming
event stream forwards them incrementally.

Why an event handler instead of ``agent.run_stream()``: ``run_stream`` only
streams the *final* response, so reasoning produced while the model is deciding
which tool to call is never shown. ``agent.run(..., event_stream_handler=...)``
invokes the handler for **every** model response (and tool event) during the run,
so tool-using agents (the research agent, the browser condenser) show their
reasoning too.

A non-streaming ``agent.run()`` fallback replays any thinking captured in the
final message history, so something is always shown even if the streaming path
fails.

Usage::

    reporter = console_thinking_reporter("Pelican Bay / research")
    output = await run_agent_streamed(
        agent, prompt, model_settings={"max_tokens": 4096}, on_thinking=reporter
    )
    reporter.close()
"""

from __future__ import annotations

import logging
import sys
from typing import Any, AsyncIterable, Callable, Optional

from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
)

logger = logging.getLogger(__name__)

ThinkingCallback = Callable[[str], None]


class ThinkingReporter:
    """Console callback that prints streamed thinking beneath a labeled header.

    Writes partial tokens as they arrive (no trailing newline until ``close()``),
    indenting wrapped lines so the reasoning is visually grouped.
    """

    def __init__(
        self,
        label: str,
        *,
        stream: Any | None = None,
        enabled: bool = True,
        indent: str = "    ",
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._label = label
        self._enabled = enabled
        self._indent = indent
        self._open = False
        self._produced = False

    @property
    def produced(self) -> bool:
        """True once any non-empty reasoning delta has been written."""
        return self._produced

    def __call__(self, delta: str) -> None:
        if not self._enabled or not delta:
            return
        self._produced = True
        if not self._open:
            self._write(f"\n  🧠 {self._label} — thinking:\n{self._indent}")
            self._open = True
        self._write(delta.replace("\n", "\n" + self._indent))
        self._stream.flush()

    def close(self) -> None:
        if self._open:
            self._write("\n")
            self._stream.flush()
            self._open = False

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
        except UnicodeEncodeError:  # pragma: no cover - non-UTF8 console
            self._stream.write(text.encode("ascii", "replace").decode("ascii"))


def console_thinking_reporter(
    label: str,
    *,
    stream: Any | None = None,
    enabled: bool = True,
    indent: str = "    ",
) -> ThinkingReporter:
    """Build a :class:`ThinkingReporter` for a pipeline step."""
    return ThinkingReporter(label, stream=stream, enabled=enabled, indent=indent)


async def forward_events(
    events: AsyncIterable[Any],
    *,
    on_thinking: Optional[ThinkingCallback] = None,
    on_text: Optional[ThinkingCallback] = None,
) -> None:
    """Forward thinking/text deltas from one model response's event stream.

    ``events`` is the async iterable PydanticAI passes to an
    ``event_stream_handler`` (an ``AgentStream``); it yields ``PartStartEvent``
    and ``PartDeltaEvent`` objects. Only newly produced content is emitted, so
    callers receive an append-only reasoning trace.
    """
    async for event in events:
        if isinstance(event, PartStartEvent):
            part = event.part
            if on_thinking and isinstance(part, ThinkingPart) and part.content:
                on_thinking(part.content)
            elif on_text and isinstance(part, TextPart) and part.content:
                on_text(part.content)
        elif isinstance(event, PartDeltaEvent):
            delta = event.delta
            kind = getattr(delta, "part_delta_kind", None)
            content = getattr(delta, "content_delta", None)
            if not content:
                continue
            if kind == "thinking" and on_thinking:
                on_thinking(content)
            elif kind == "text" and on_text:
                on_text(content)


def replay_thinking_from_result(result: Any, on_thinking: ThinkingCallback) -> None:
    """Feed thinking captured in a completed (non-streamed) run to a callback."""
    for message in result.all_messages():
        parts = getattr(message, "parts", None)
        if not parts:
            continue
        for part in parts:
            if isinstance(part, ThinkingPart) and part.content:
                on_thinking(part.content)
                on_thinking("\n")


async def run_agent_streamed(
    agent: Any,
    prompt: str,
    *,
    model_settings: Optional[dict[str, Any]] = None,
    on_thinking: Optional[ThinkingCallback] = None,
    on_text: Optional[ThinkingCallback] = None,
    fallback: bool = True,
) -> Any:
    """Run ``agent``, forwarding reasoning from every model response, return output.

    Uses ``agent.run(..., event_stream_handler=...)`` so reasoning is streamed for
    all model turns (including tool-selection turns). Falls back to a regular
    non-streaming run (replaying any captured thinking) if that path fails.
    """
    if on_thinking is None and on_text is None:
        result = await agent.run(prompt, model_settings=model_settings)
        return result.output

    async def _handler(_ctx: Any, events: AsyncIterable[Any]) -> None:
        await forward_events(events, on_thinking=on_thinking, on_text=on_text)

    try:
        result = await agent.run(
            prompt,
            model_settings=model_settings,
            event_stream_handler=_handler,
        )
        return result.output
    except Exception:
        if not fallback:
            raise
        logger.warning(
            "Streaming run failed; retrying without streaming", exc_info=True
        )
        result = await agent.run(prompt, model_settings=model_settings)
        if on_thinking:
            replay_thinking_from_result(result, on_thinking)
        return result.output
