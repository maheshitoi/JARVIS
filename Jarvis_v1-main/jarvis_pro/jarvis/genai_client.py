"""Shared Google GenAI client for non-realtime calls (vision, email drafting).

Speed rules (measured — see README "Speed"):
- Fail fast: no automatic retries and a hard timeout. The SDK's default retrying on an
  overloaded (503) model kept JARVIS "thinking" for minutes.
- Fall back: try each model in config.TOOL_MODELS in order on 404/429/5xx/timeout.
- Minimal thinking: tool work (describe a screenshot, read a chat) doesn't need deep reasoning.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from . import config

logger = logging.getLogger("jarvis.genai")
_FALLBACK_CODES = {404, 408, 429, 500, 502, 503, 504}


@lru_cache(maxsize=1)
def client() -> genai.Client:
    # Reads GOOGLE_API_KEY (or GEMINI_API_KEY) from the environment.
    return genai.Client(
        http_options=types.HttpOptions(
            timeout=config.TOOL_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1),  # no hidden retries
        )
    )


def _with_minimal_thinking(cfg: types.GenerateContentConfig | None) -> types.GenerateContentConfig:
    cfg = cfg.model_copy() if cfg else types.GenerateContentConfig()
    if cfg.thinking_config is None:
        cfg.thinking_config = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
    return cfg


def _is_thinking_rejection(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and e.code == 400 and "think" in str(e).lower()


def _explain(e: Exception | None) -> str:
    """Short, speakable reason instead of a JSON dump."""
    code = getattr(e, "code", None)
    if code == 429:
        return ("Google's daily free-tier limit for the vision models is used up. "
                "It resets tomorrow, or Boss can enable billing on the Google AI Studio key.")
    if code in (500, 502, 503, 504) or isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return "Google's models are overloaded right now. Try again in a minute."
    return f"No Gemini model is available right now ({code or type(e).__name__})."


def _should_fallback(e: Exception) -> bool:
    if isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return True
    return isinstance(e, errors.APIError) and getattr(e, "code", None) in _FALLBACK_CODES


def generate(contents: Any, config_: types.GenerateContentConfig | None = None, *, fast: bool = False):
    last: Exception | None = None
    for model in (config.FAST_TOOL_MODELS if fast else config.TOOL_MODELS):
        cfg = _with_minimal_thinking(config_)
        for attempt in range(2):
            try:
                return client().models.generate_content(model=model, contents=contents, config=cfg)
            except Exception as e:  # noqa: BLE001
                if attempt == 0 and _is_thinking_rejection(e):
                    cfg = config_  # this model doesn't accept the thinking level — use its default
                    continue
                if not _should_fallback(e):
                    raise
                logger.warning("model %s unavailable (%s), trying next", model, getattr(e, "code", type(e).__name__))
                last = e
                break
    raise RuntimeError(_explain(last))


async def agenerate(contents: Any, config_: types.GenerateContentConfig | None = None, *, fast: bool = False):
    last: Exception | None = None
    for model in (config.FAST_TOOL_MODELS if fast else config.TOOL_MODELS):
        cfg = _with_minimal_thinking(config_)
        for attempt in range(2):
            try:
                return await client().aio.models.generate_content(model=model, contents=contents, config=cfg)
            except Exception as e:  # noqa: BLE001
                if attempt == 0 and _is_thinking_rejection(e):
                    cfg = config_
                    continue
                if not _should_fallback(e):
                    raise
                logger.warning("model %s unavailable (%s), trying next", model, getattr(e, "code", type(e).__name__))
                last = e
                break
    raise RuntimeError(_explain(last))
