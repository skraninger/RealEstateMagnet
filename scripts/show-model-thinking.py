"""Simple demo: show the local model's reasoning ("thinking") live on the console.

Run it (with the model server already started):

    .venv\\Scripts\\python.exe scripts\\show-model-thinking.py
    .venv\\Scripts\\python.exe scripts\\show-model-thinking.py "your own question"
    .venv\\Scripts\\python.exe scripts\\show-model-thinking.py --trace-file logs\\traces.jsonl

What it does:
  1. Probes the server directly and reports whether it returns ``reasoning_content``
     (the separate field llama.cpp uses with ``--reasoning-format deepseek``).
  2. Streams a tiny agent through the SAME helper the pipeline uses
     (``modules.community.streaming.run_agent_streamed`` + ``ThinkingReporter``),
     printing the reasoning as it is produced, then the answer.
  3. Optionally (``--trace-file``) writes OpenTelemetry spans as local JSONL via
     the custom ``FileSpanProcessor`` — nothing is sent to the cloud.

If nothing streams, the probe line above tells you whether the *server* produced
reasoning at all. Start the server with ``scripts/start-model-server.ps1`` (add
``-Reasoning on`` to force thinking).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow running from scripts/ (add the repo root so `modules` is importable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Keep the console clean (no pydantic-ai banner).
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

DEFAULT_PROMPT = (
    "A gated community charges $450/month HOA and an $1,800/year CDD assessment. "
    "What is the total annual cost? Think it through step by step, then give the "
    "final number."
)


def _base_url() -> str:
    return os.environ.get("MODEL_BASE_URL", "http://localhost:8080/v1").rstrip("/")


def _model_name() -> str:
    return os.environ.get("MODEL_NAME", "qwen3.8-27b")


def probe_raw(prompt: str) -> bool:
    """Query the server directly and report where the reasoning ended up."""
    try:
        import httpx
    except ImportError:
        print("[probe] httpx not installed; skipping raw probe")
        return True

    url = f"{_base_url()}/chat/completions"
    body = {
        "model": _model_name(),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0.3,
        "stream": False,
    }
    try:
        response = httpx.post(url, json=body, timeout=300)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"[probe] server request failed: {exc}")
        print("        Is the model server running?  scripts/start-model-server.ps1")
        return False

    message = response.json()["choices"][0]["message"]
    print(f"[probe] message fields from server: {sorted(message.keys())}")

    reasoning = message.get("reasoning_content")
    if reasoning:
        print(f"[probe] OK - server returned reasoning_content ({len(reasoning)} chars)")
        return True

    content = message.get("content") or ""
    if "<think>" in content or " thinking" in content:
        print("[probe] OK - thinking tags found inline in message.content")
        return True

    print("[probe] server returned NO reasoning for this prompt.")
    print("        The model may be in non-thinking mode; start the server with")
    print("        scripts/start-model-server.ps1 -Reasoning on")
    return False


async def stream_demo(prompt: str) -> None:
    """Stream an agent through the pipeline's streaming helper."""
    from pydantic_ai import Agent

    from modules.community.agent import build_model
    from modules.community.streaming import console_thinking_reporter, run_agent_streamed

    agent = Agent(
        build_model(),
        system_prompt="You are a helpful assistant. Think step by step before answering.",
    )
    reporter = console_thinking_reporter("show-model-thinking")

    print("\n" + "=" * 72)
    print("Streaming via run_agent_streamed() — reasoning appears as it is produced")
    print("=" * 72 + "\n")

    output = await run_agent_streamed(
        agent,
        prompt,
        model_settings={"max_tokens": 512},
        on_thinking=reporter,
    )
    reporter.close()

    if not reporter.produced:
        print("\n  (no reasoning streamed — see the [probe] result above)")

    print("\n" + "-" * 72)
    print("ANSWER:")
    print(output)


def _parse_argv(argv: list[str]) -> tuple[str | None, str]:
    """Split out ``--trace-file PATH``; everything else is the prompt."""
    trace_file: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--trace-file" and i + 1 < len(argv):
            trace_file = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return trace_file, " ".join(rest).strip()


def main() -> None:
    trace_file, prompt = _parse_argv(sys.argv[1:])
    prompt = prompt or DEFAULT_PROMPT

    print(f"Model:  {_model_name()} @ {_base_url()}")
    print(f"Prompt: {prompt}\n")

    if trace_file:
        from modules.community.observability import setup_file_tracing

        setup_file_tracing(Path(trace_file))
        print(f"[trace] writing spans (local JSONL) to {trace_file}\n")

    probe_raw(prompt)
    asyncio.run(stream_demo(prompt))


if __name__ == "__main__":
    main()
