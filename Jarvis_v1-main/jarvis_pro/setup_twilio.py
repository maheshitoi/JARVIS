"""One-time phone setup: Twilio Elastic SIP trunk + LiveKit outbound trunk.

Reads from .env:  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER,
                  LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
Optional:         LIVEKIT_SIP_URI (e.g. sip:abc123.sip.livekit.cloud) -> also routes INBOUND calls
Writes to .env:   TWILIO_SIP_USERNAME, TWILIO_SIP_PASSWORD, TWILIO_TRUNK_DOMAIN, SIP_OUTBOUND_TRUNK_ID

Safe to re-run: it reuses what already exists.

    python setup_twilio.py
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import string
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit import api

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"
load_dotenv(ENV_FILE)
load_dotenv(BASE.parent / ".env")

NAME = "JARVIS"
LK_TRUNK_NAME = "JARVIS Twilio"
API = "https://api.twilio.com/2010-04-01"
TRUNKING = "https://trunking.twilio.com/v1"


def need(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        sys.exit(f"{key} is missing in .env")
    return value


def set_env(key: str, value: str) -> None:
    """Add or replace KEY=value in jarvis_pro/.env."""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    os.environ[key] = value


def strong_password() -> str:
    # Twilio: >= 12 chars with upper, lower and a digit.
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def check(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("message", resp.text)
        except Exception:
            msg = resp.text
        sys.exit(f"Twilio error {resp.status_code} on {resp.request.method} {resp.request.url.path}: {msg}")
    return resp.json() if resp.content else {}


def setup_twilio() -> tuple[str, str, str, str]:
    sid, token = need("TWILIO_ACCOUNT_SID"), need("TWILIO_AUTH_TOKEN")
    number = need("TWILIO_PHONE_NUMBER")
    tw = httpx.Client(auth=(sid, token), timeout=30)

    account = check(tw.get(f"{API}/Accounts/{sid}.json"))
    print(f"Twilio account: {account.get('friendly_name')} ({account.get('type')}, {account.get('status')})")
    trial = account.get("type") == "Trial"

    nums = check(tw.get(f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json", params={"PhoneNumber": number}))
    if not nums.get("incoming_phone_numbers"):
        sys.exit(f"{number} is not a phone number on this Twilio account.")
    number_sid = nums["incoming_phone_numbers"][0]["sid"]

    # 1. Elastic SIP trunk
    trunks = check(tw.get(f"{TRUNKING}/Trunks", params={"PageSize": 100}))["trunks"]
    trunk = next((t for t in trunks if t["friendly_name"] == NAME), None)
    if trunk is None:
        domain = f"jarvis-{sid[-10:].lower()}.pstn.twilio.com"
        trunk = check(tw.post(f"{TRUNKING}/Trunks", data={"FriendlyName": NAME, "DomainName": domain}))
        print(f"Created SIP trunk {trunk['sid']} ({domain})")
    else:
        print(f"Using SIP trunk {trunk['sid']} ({trunk['domain_name']})")
    trunk_sid, domain = trunk["sid"], trunk["domain_name"]

    # 2. Credential list (username/password LiveKit uses to place calls)
    username = os.getenv("TWILIO_SIP_USERNAME") or "jarvis"
    password = os.getenv("TWILIO_SIP_PASSWORD") or strong_password()
    lists = check(tw.get(f"{API}/Accounts/{sid}/SIP/CredentialLists.json"))["credential_lists"]
    cl = next((c for c in lists if c["friendly_name"] == NAME), None)
    if cl is None:
        cl = check(tw.post(f"{API}/Accounts/{sid}/SIP/CredentialLists.json", data={"FriendlyName": NAME}))
    creds_url = f"{API}/Accounts/{sid}/SIP/CredentialLists/{cl['sid']}/Credentials"
    creds = check(tw.get(f"{creds_url}.json"))["credentials"]
    existing = next((c for c in creds if c["username"] == username), None)
    if existing:
        check(tw.post(f"{creds_url}/{existing['sid']}.json", data={"Password": password}))
    else:
        check(tw.post(f"{creds_url}.json", data={"Username": username, "Password": password}))
    attached = check(tw.get(f"{TRUNKING}/Trunks/{trunk_sid}/CredentialLists"))["credential_lists"]
    if not any(c["sid"] == cl["sid"] for c in attached):
        check(tw.post(f"{TRUNKING}/Trunks/{trunk_sid}/CredentialLists", data={"CredentialListSid": cl["sid"]}))
    print(f"SIP credentials ready (user '{username}')")

    # 3. Inbound routing to LiveKit (optional)
    sip_uri = os.getenv("LIVEKIT_SIP_URI", "").strip()
    if sip_uri:
        if not sip_uri.startswith("sip:"):
            sip_uri = "sip:" + sip_uri
        origs = check(tw.get(f"{TRUNKING}/Trunks/{trunk_sid}/OriginationUrls"))["origination_urls"]
        if not any(o["sip_url"] == sip_uri for o in origs):
            check(tw.post(f"{TRUNKING}/Trunks/{trunk_sid}/OriginationUrls", data={
                "FriendlyName": "LiveKit", "SipUrl": sip_uri, "Priority": 1, "Weight": 1, "Enabled": "true"}))
        print(f"Inbound calls to {number} -> {sip_uri}")

        # 4. Attach the number to the trunk (only with inbound routing, otherwise inbound calls would fail)
        attached_nums = check(tw.get(f"{TRUNKING}/Trunks/{trunk_sid}/PhoneNumbers"))["phone_numbers"]
        if not any(n["sid"] == number_sid for n in attached_nums):
            check(tw.post(f"{TRUNKING}/Trunks/{trunk_sid}/PhoneNumbers", data={"PhoneNumberSid": number_sid}))
            print(f"Attached {number} to the trunk")
    else:
        print("LIVEKIT_SIP_URI not set: outbound only (set it and re-run to receive calls too).")

    set_env("TWILIO_SIP_USERNAME", username)
    set_env("TWILIO_SIP_PASSWORD", password)
    set_env("TWILIO_TRUNK_DOMAIN", domain)
    if trial:
        print("\nNOTE: Trial account — Twilio only lets you call numbers verified at "
              "console.twilio.com > Phone Numbers > Verified Caller IDs, and plays a trial message first.")
    return domain, number, username, password


async def setup_livekit(domain: str, number: str, username: str, password: str) -> str:
    for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        need(key)
    async with api.LiveKitAPI() as lk:
        found = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        trunk = next((t for t in found.items if t.name == LK_TRUNK_NAME), None)
        info = api.SIPOutboundTrunkInfo(name=LK_TRUNK_NAME, address=domain, numbers=[number],
                                        auth_username=username, auth_password=password)
        if trunk:
            await lk.sip.delete_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk.sip_trunk_id))
        trunk = await lk.sip.create_outbound_trunk(api.CreateSIPOutboundTrunkRequest(trunk=info))
    print(f"LiveKit outbound trunk: {trunk.sip_trunk_id}")
    return trunk.sip_trunk_id


def main() -> None:
    domain, number, user, pw = setup_twilio()
    trunk_id = asyncio.run(setup_livekit(domain, number, user, pw))
    set_env("SIP_OUTBOUND_TRUNK_ID", trunk_id)
    print("\nDone. SIP_OUTBOUND_TRUNK_ID saved to .env. Restart `python agent.py dev`.")


if __name__ == "__main__":
    main()
