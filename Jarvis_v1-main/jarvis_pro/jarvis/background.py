"""Always-on background work, run inside server.py (which keeps running when nobody is connected).

  - Scheduled automations: every 30 s, start the ones that are due (automations.py).
  - Away mode: while Boss is NOT connected, check for alerts (site down, SSL, phone messages,
    failures, meeting reminders) every `check_every_minutes` and push them to the phone (notify.py).
    While Boss is connected, the voice session (proactive.py) does this instead and speaks them.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import automations, digest, notify
from .access import DENY, get_policy

logger = logging.getLogger("jarvis.background")


async def _away_alerts() -> None:
    from .proactive import _load_state, _save_state, _settings, detect_alerts

    if get_policy().level("proactive.alerts") == DENY or not notify.configured():
        return
    state = _load_state()
    alerts = await asyncio.to_thread(detect_alerts, state, _settings())
    _save_state(state)
    for level, title, text in alerts:
        if notify.wants(level, away=True):
            await notify.push(title, text, level)


async def run_forever() -> None:
    last_alerts = 0.0
    while True:
        try:
            await automations.tick()
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception("automation tick failed")
        try:
            every = max(1, int(get_policy().section("proactive").get("check_every_minutes", 5))) * 60
            if time.monotonic() - last_alerts >= every and not digest.owner_present():
                last_alerts = time.monotonic()
                await _away_alerts()
        except Exception:  # noqa: BLE001
            logger.exception("away-mode alert check failed")
        await asyncio.sleep(30)
