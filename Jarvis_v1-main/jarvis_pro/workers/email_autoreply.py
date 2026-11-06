"""Background email auto-reply worker.

    python workers/email_autoreply.py           # run forever
    python workers/email_autoreply.py --once    # one pass (good for Task Scheduler)

Settings come from access.yaml -> capabilities.email.auto_reply.
Every reply is written to logs/audit.jsonl and shows up in the dashboard's Activity tab.
"""

from __future__ import annotations

import argparse
import email
import json
import logging
import sys
import time
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import audit, config  # noqa: E402
from jarvis.access import get_policy  # noqa: E402
from jarvis.genai_client import generate  # noqa: E402
from jarvis.tools import email_tools as et  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis.autoreply")

PROMPT = """You are JARVIS, the email assistant for {owner} <{me}>.
Write a short, polite, professional reply to the email below.

Rules:
- If it needs {owner}'s personal decision (money, meetings, contracts, anything legal), acknowledge it and
  say {owner} will get back to them soon.
- Never invent facts, prices, dates, availability or promises.
- Reply in the same language as the email.
- Plain text only. No subject line. End with exactly this signature: {signature}

From: {sender}
Subject: {subject}

{body}
"""


SEEN_FILE = config.DATA_DIR / "autoreply_seen.json"


def _load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]), encoding="utf-8")


def should_skip(msg, cfg: dict, me: str) -> str | None:
    sender = parseaddr(msg["From"] or "")[1].lower()
    if not sender or sender == me.lower():
        return "own address"
    if any(s.lower() in sender for s in cfg.get("never_reply_to") or []):
        return "blocked sender"
    only = [s.lower() for s in cfg.get("only_from") or []]
    if only and not any(s in sender for s in only):
        return "not in only_from"
    if (msg.get("Auto-Submitted") or "no").lower() != "no":
        return "automatic email"
    if msg.get("List-Unsubscribe") or (msg.get("Precedence") or "").lower() in ("bulk", "list", "junk"):
        return "mailing list"
    return None


def process_once() -> int:
    policy = get_policy()
    cfg = policy.section("email").get("auto_reply") or {}
    if not cfg.get("enabled"):
        log.info("auto_reply is disabled in access.yaml")
        return 0
    mode = "send" if cfg.get("mode") == "send" else "draft"
    capability = "email.send" if mode == "send" else "email.draft"
    if not policy.allowed(capability):
        log.warning("%s is denied in access.yaml — skipping", capability)
        return 0

    me = et.creds()["EMAIL_ADDRESS"]
    seen = _load_seen()
    handled = 0
    with et.imap_connect() as imap:
        imap.select("INBOX", readonly=True)  # never changes read/unread flags in your mailbox
        _, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = et.decode(msg["Subject"])
            sender = parseaddr(msg["From"] or "")[1]

            msg_id = msg["Message-ID"] or f"{sender}|{subject}|{msg['Date']}"
            if msg_id in seen:
                continue
            seen.add(msg_id)
            _save_seen(seen)

            reason = should_skip(msg, cfg, me)
            if reason:
                log.info("skip %s (%s)", sender, reason)
                continue

            reply = generate(
                PROMPT.format(owner=policy.owner_name, me=me, sender=sender, subject=subject,
                                       body=et.text_body(msg)[:6000],
                                       signature=cfg.get("signature", "— JARVIS")),
            ).text or ""
            if not reply.strip():
                continue

            out = et.build_message(sender, subject if subject.lower().startswith("re:") else f"Re: {subject}",
                                   reply.strip(), in_reply_to=msg["Message-ID"])
            if mode == "send":
                et.smtp_send(out)
            else:
                et.save_draft(out)
            audit.record({"capability": capability, "summary": f"Auto-reply ({mode}) to {sender}: {subject}",
                          "status": "done", "approved_by": "auto_reply", "mode": "worker"})
            log.info("%s reply for %s: %s", "sent" if mode == "send" else "drafted", sender, subject)
            handled += 1
    return handled


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            process_once()
        except Exception:  # noqa: BLE001
            log.exception("auto-reply pass failed")
        if args.once:
            break
        interval = (get_policy().section("email").get("auto_reply") or {}).get("check_every_seconds", 60)
        time.sleep(max(15, int(interval)))


if __name__ == "__main__":
    main()
