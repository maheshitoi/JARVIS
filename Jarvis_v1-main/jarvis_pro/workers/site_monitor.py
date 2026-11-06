"""Background website monitor + daily report.

    python workers/site_monitor.py            # run forever: check every N min, report daily at report.time
    python workers/site_monitor.py --check    # one quick check of all sites   (Task Scheduler)
    python workers/site_monitor.py --report   # build + email today's report   (Task Scheduler)

Settings: access.yaml -> capabilities.websites (sites, report.time, check_every_minutes, email_to_owner)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import audit, config, websites  # noqa: E402
from jarvis.access import DENY, get_policy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis.sites")
LAST_REPORT_FILE = config.DATA_DIR / "site_last_report.txt"


def _report_cfg() -> dict:
    return websites.settings().get("report") or {}


async def run_check() -> None:
    if get_policy().level("websites.check") == DENY:
        log.warning("websites.check is denied in access.yaml — skipping")
        return
    for r in await websites.check_all(full=False):
        (log.info if r["up"] and not r["issues"] else log.warning)(websites.summarise(r))
        if not r["up"]:
            audit.record({"capability": "websites.check", "summary": websites.summarise(r),
                          "status": "failed", "mode": "worker"})


async def run_report() -> None:
    if get_policy().level("websites.report") == DENY:
        log.warning("websites.report is denied in access.yaml — skipping")
        return
    report = await websites.build_daily_report()
    log.info("report saved: reports/%s.html — %s", report["date"], report["headline"])
    LAST_REPORT_FILE.write_text(report["date"], encoding="utf-8")
    if _report_cfg().get("email_to_owner"):
        try:
            to = await asyncio.to_thread(websites.email_report, report)
            log.info("report emailed to %s", to)
        except Exception as e:  # noqa: BLE001
            log.warning("could not email report: %s", e)


async def loop() -> None:
    log.info("monitoring %d site(s). Ctrl+C to stop.", len(websites.sites()))
    while True:
        cfg = _report_cfg()
        try:
            await run_check()
            due = datetime.strptime(str(cfg.get("time", "09:00")), "%H:%M").time()
            last = LAST_REPORT_FILE.read_text(encoding="utf-8").strip() if LAST_REPORT_FILE.exists() else ""
            if cfg.get("enabled", True) and datetime.now().time() >= due and last != date.today().isoformat():
                await run_report()
        except Exception:  # noqa: BLE001
            log.exception("monitor pass failed")
        await asyncio.sleep(max(1, int(cfg.get("check_every_minutes", 15))) * 60)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    if not websites.sites():
        sys.exit("No websites configured. Add them under capabilities.websites.sites in access.yaml.")
    if a.check:
        asyncio.run(run_check())
    elif a.report:
        asyncio.run(run_report())
    else:
        asyncio.run(loop())


if __name__ == "__main__":
    main()
