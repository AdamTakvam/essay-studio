"""Thin wrapper around the Anthropic API. Every call is reported to the tool-output pane."""
import asyncio
import json
import re
import time
from typing import AsyncIterator, Callable

from .config import FAKE_LLM
from .logbus import logger

log = logger("claude")

# Used only when the Models API can't be reached; the live list is preferred.
FALLBACK_MODELS = [
    {"id": "claude-opus-4-1", "name": "Claude Opus 4.1"},
    {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
]

_client = None
_models_cache: tuple[float, list] | None = None


def client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    return _client


async def list_models() -> list[dict]:
    global _models_cache
    if FAKE_LLM:
        return [{"id": "fake-claude", "name": "Fake Claude (offline test mode)"}]
    if _models_cache and time.time() - _models_cache[0] < 3600:
        return _models_cache[1]
    try:
        page = await client().models.list(limit=100)
        models = [{"id": m.id, "name": m.display_name or m.id} for m in page.data if m.id.startswith("claude")]
        if models:
            _models_cache = (time.time(), models)
            log(f"Loaded {len(models)} models from the Anthropic Models API")
            return models
    except Exception as e:  # missing key, offline, etc.
        log(f"Could not list models ({type(e).__name__}: {e}); using built-in list", "warn")
    return FALLBACK_MODELS


async def stream(model: str, system: str, prompt: str, *, label: str, max_tokens: int = 16000,
                 fake: Callable[[], str] | None = None) -> AsyncIterator[str]:
    started = time.time()
    log(f"{label}: request → {model} (prompt {len(prompt):,} chars)")
    if FAKE_LLM:
        text = fake() if fake else f"[fake response for {label}]"
        for i in range(0, len(text), 40):
            await asyncio.sleep(0.01)
            yield text[i:i + 40]
        log(f"{label}: done (fake) in {time.time() - started:.1f}s")
        return
    try:
        async with client().messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as s:
            async for text in s.text_stream:
                yield text
            final = await s.get_final_message()
        u = final.usage
        log(f"{label}: done in {time.time() - started:.1f}s · {u.input_tokens:,} in / "
            f"{u.output_tokens:,} out tokens · stop={final.stop_reason}")
    except asyncio.CancelledError:
        log(f"{label}: cancelled", "warn")
        raise
    except Exception as e:
        log(f"{label}: failed — {type(e).__name__}: {e}", "error")
        raise


async def complete(model: str, system: str, prompt: str, *, label: str, max_tokens: int = 16000,
                   fake: Callable[[], str] | None = None) -> str:
    parts = []
    async for chunk in stream(model, system, prompt, label=label, max_tokens=max_tokens, fake=fake):
        parts.append(chunk)
    return "".join(parts).strip()


def parse_json(text: str):
    """Pull the first JSON object/array out of a model reply (tolerates ``` fences)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("No JSON found in model reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj
