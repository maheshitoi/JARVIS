"""Central configuration: paths, environment variables, model names."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # .../jarvis_pro

# jarvis_pro/.env wins; the original project's .env is used as a fallback so
# LIVEKIT_* and GOOGLE_API_KEY don't have to be copied.
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
WEB_DIR = BASE_DIR / "web"
ACCESS_FILE = Path(os.getenv("JARVIS_ACCESS_FILE", str(BASE_DIR / "access.yaml")))

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

AGENT_NAME = os.getenv("JARVIS_AGENT_NAME", "jarvis-pro")
# Measured on this key (time to first spoken audio): 3.8-live 0.5 s, 3.1-flash-live 0.6 s,
# 2.5-native-audio 2.2 s (1.4 s with thinking off).
REALTIME_MODEL = os.getenv("JARVIS_MODEL", "gemini-3.8-live")
VOICE = os.getenv("JARVIS_VOICE", "Aoede")
TEMPERATURE = float(os.getenv("JARVIS_TEMPERATURE", "0.8"))

# ---- latency tuning ----
# How long you must be silent before JARVIS decides you've finished talking.
# Lower = faster replies; too low and it cuts you off mid-sentence. 400-800 is a good range.
SILENCE_MS = int(os.getenv("JARVIS_SILENCE_MS", "500"))
# 2.5 native-audio models "think" before answering by default (adds ~1-3 s).
# 0 = no thinking (fastest). -1 = let the model decide.
THINKING_BUDGET = int(os.getenv("JARVIS_THINKING_BUDGET", "0"))
# Text/vision models for screenshots, WhatsApp and email replies — comma-separated,
# tried in order (falls back when one is retired, overloaded or times out).
# 3.5-flash first: measured 1.1 s vs 4.7 s for 3.8-flash, which was also often overloaded (503).
TOOL_MODELS = [m.strip() for m in os.getenv("JARVIS_TOOL_MODEL", "gemini-3.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite").split(",")
               if m.strip()]
# Faster/lighter models for quick looks (describe the screen). Measured ~2 s vs ~3.5 s.
FAST_TOOL_MODELS = [m.strip() for m in os.getenv(
    "JARVIS_FAST_TOOL_MODEL", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3.8-flash").split(",") if m.strip()]
# Give up on a slow model after this long and try the next one.
TOOL_TIMEOUT_MS = int(os.getenv("JARVIS_TOOL_TIMEOUT_MS", "15000"))


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


def require_env(*names: str) -> dict[str, str]:
    """Return the requested env vars, or raise a clear error listing what's missing."""
    from livekit.agents import ToolError

    values = {n: env(n) for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise ToolError(
            f"Not configured: missing {', '.join(missing)} in jarvis_pro/.env. "
            "Tell Boss which setting is missing."
        )
    return values  # type: ignore[return-value]
