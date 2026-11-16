"""Make the AI phone customers and inform them of something.

One customer:
    python call_customer.py "Ravi" "Your AC service is confirmed for Saturday 10 AM."
    python call_customer.py +919876543210 "Your order 1042 is ready for pickup."

All customers (campaign) — {name}, {first_name}, {upcoming} or any field from customers.json
is filled in per customer. Without --yes it only PREVIEWS who would be called:
    python call_customer.py --all "Hi {first_name}, reminder: {upcoming}"
    python call_customer.py --all "Hi {first_name}, reminder: {upcoming}" --yes
    python call_customer.py --only C001,C002 "Our office is closed on Monday." --yes

Results:
    python call_customer.py --log

Needs `python agent.py dev` running (it handles the calls) and SIP_OUTBOUND_TRUNK_ID in .env.
Running this command yourself counts as your approval for the calls. Campaign calls go one at
a time, only within calls.campaign_hours (business.yaml), and skip customers with "do_not_call": true.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import business  # noqa: E402


def campaign(args: argparse.Namespace) -> None:
    only = args.only.split(",") if args.only else None
    targets, skipped = business.campaign_targets(only)
    print(f"{len(targets)} customer(s) to call, one at a time:")
    for c in targets:
        print(f"  - {c.get('name')} ({c.get('phone')}): {business.render_message(args.message, c)}")
    for s in skipped:
        print(f"  skip {s}")
    if not targets:
        sys.exit("Nobody to call.")
    if not args.yes:
        print("\nPreview only. Add --yes to place these calls.")
        return
    if not business.within_calling_hours():
        sys.exit(f"Not calling: outside calling hours {'-'.join(business.calling_window())} (calls.campaign_hours).")
    asyncio.run(business.run_campaign(args.message, targets, gap_s=args.gap))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("who", nargs="?", help="customer name/id or +number (single call)")
    p.add_argument("message", nargs="?", help="what to tell them")
    p.add_argument("--all", action="store_true", help="call every customer (campaign)")
    p.add_argument("--only", help="campaign for these ids/names only, comma-separated")
    p.add_argument("--yes", action="store_true", help="actually place campaign calls (default: preview)")
    p.add_argument("--gap", type=float, default=5, help="seconds between campaign calls")
    p.add_argument("--log", action="store_true", help="show the latest customer calls")
    args = p.parse_args()

    if args.log:
        for c in business.recent_calls(10, with_transcript=True):
            print(json.dumps(c, ensure_ascii=False, indent=2))
        return
    if args.all or args.only:
        # With --all/--only the first positional is the message.
        args.message = args.message or args.who
        if not args.message:
            p.error("give the message to send")
        campaign(args)
        return
    if not (args.who and args.message):
        p.error("give a customer and a message, or use --all / --only / --log")
    try:
        result = asyncio.run(business.dispatch_customer_call(args.who, args.message))
    except business.CallError as e:
        sys.exit(f"Not called: {e}")
    print(f"Calling {result['customer'] or result['number']} (room {result['room']}). "
          "Run `python call_customer.py --log` after the call for the summary.")


if __name__ == "__main__":
    main()
