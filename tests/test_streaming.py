"""Offline tests for the thinking-streaming helper (no model server needed).

The helper sits between PydanticAI's streaming event handler and the pipeline
console, so these tests drive it with fake event streams/agents and assert the
exact thinking/text deltas forwarded to the callbacks.
"""

from __future__ import annotations

import asyncio
import io

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import (  # noqa: E402
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from modules.community.streaming import (  # noqa: E402
    ThinkingReporter,
    forward_events,
    run_agent_streamed,
)


async def _agen(items):
    for item in items:
        yield item


def _thinking_start(content: str) -> PartStartEvent:
    return PartStartEvent(index=0, part=ThinkingPart(content=content))


def _thinking_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=content))


def _text_start(content: str) -> PartStartEvent:
    return PartStartEvent(index=1, part=TextPart(content=content))


def _text_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=1, delta=TextPartDelta(content_delta=content))


class TestForwardEvents:
    def test_forwards_thinking_and_text(self):
        thinking: list[str] = []
        text: list[str] = []
        events = [
            _thinking_start("Let me "),
            _thinking_delta("think."),
            _text_start("Answer: "),
            _text_delta("391"),
        ]
        asyncio.run(
            forward_events(
                _agen(events), on_thinking=thinking.append, on_text=text.append
            )
        )
        assert "".join(thinking) == "Let me think."
        assert "".join(text) == "Answer: 391"

    def test_ignores_empty_and_unrelated_events(self):
        thinking: list[str] = []
        # An empty delta and a text delta (no on_text) must not emit anything.
        events = [_thinking_delta(""), _text_delta("nope")]
        asyncio.run(forward_events(_agen(events), on_thinking=thinking.append))
        assert thinking == []


class TestThinkingReporter:
    def test_prints_header_once_and_indents(self):
        buf = io.StringIO()
        reporter = ThinkingReporter("Pelican Bay / ai", stream=buf)
        reporter("line one\nline ")
        reporter("two")
        reporter.close()
        out = buf.getvalue()
        assert out.count("Pelican Bay / ai") == 1
        assert "line one\n    line two" in out
        assert out.endswith("\n")
        assert reporter.produced is True

    def test_disabled_reporter_is_silent(self):
        buf = io.StringIO()
        reporter = ThinkingReporter("x", stream=buf, enabled=False)
        reporter("nope")
        reporter.close()
        assert buf.getvalue() == ""
        assert reporter.produced is False


class _FakeResult:
    def __init__(self, output, thinking=""):
        self.output = output
        self._thinking = thinking

    def all_messages(self):
        parts = [ThinkingPart(content=self._thinking)] if self._thinking else []
        return [ModelResponse(parts=parts)]


class _FakeAgent:
    """Calls the event_stream_handler once per simulated model turn."""

    def __init__(self, turns, output):
        self._turns = turns  # list[list[event]]
        self._output = output
        self.handler_calls = 0
        self.run_calls = 0

    async def run(self, prompt, *, model_settings=None, event_stream_handler=None):
        self.run_calls += 1
        if event_stream_handler is not None:
            for turn in self._turns:
                self.handler_calls += 1
                await event_stream_handler(None, _agen(turn))
        return _FakeResult(self._output)


class _FakeFailingStreamAgent:
    """run() with a handler blows up; the retry without a handler succeeds."""

    def __init__(self):
        self.run_calls = 0

    async def run(self, prompt, *, model_settings=None, event_stream_handler=None):
        self.run_calls += 1
        if event_stream_handler is not None:
            raise RuntimeError("streaming unsupported")
        return _FakeResult("fallback-output", thinking="replayed reasoning")


class TestRunAgentStreamed:
    def test_streams_thinking_from_every_turn(self):
        agent = _FakeAgent(
            turns=[
                [_thinking_start("first turn reasoning")],
                [_thinking_start("second turn reasoning")],
            ],
            output="the-output",
        )
        thinking: list[str] = []
        out = asyncio.run(
            run_agent_streamed(agent, "prompt", on_thinking=thinking.append)
        )
        assert out == "the-output"
        # Both (tool) turns are surfaced, not just the final response.
        assert agent.handler_calls == 2
        assert "first turn reasoning" in "".join(thinking)
        assert "second turn reasoning" in "".join(thinking)

    def test_falls_back_and_replays_thinking(self):
        agent = _FakeFailingStreamAgent()
        thinking: list[str] = []
        out = asyncio.run(
            run_agent_streamed(agent, "prompt", on_thinking=thinking.append)
        )
        assert out == "fallback-output"
        assert agent.run_calls == 2
        assert "replayed reasoning" in "".join(thinking)

    def test_without_callbacks_uses_plain_run(self):
        agent = _FakeAgent(turns=[], output="plain-output")
        out = asyncio.run(run_agent_streamed(agent, "prompt"))
        assert out == "plain-output"
        assert agent.handler_calls == 0
        assert agent.run_calls == 1
