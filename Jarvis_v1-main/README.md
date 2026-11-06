# FRIDAY — Voice AI Assistant

FRIDAY is a real-time voice assistant built on **LiveKit Agents** and **Google Gemini Live** (native audio). You talk to it and it talks back in a natural voice. It has its own personality ("Boss"), and you can extend it with tools such as sending email, answering your inbox, and making or answering phone calls.

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Project structure](#2-project-structure)
3. [Setup](#3-setup)
4. [Running FRIDAY](#4-running-friday)
5. [Where you can use it](#5-where-you-can-use-it)
6. [Adding tools (how FRIDAY "does things")](#6-adding-tools-how-friday-does-things)
7. [Email: send by voice + auto-reply](#7-email-send-by-voice--auto-reply)
8. [Phone calls: inbound and outbound](#8-phone-calls-inbound-and-outbound)
9. [More ideas](#9-more-ideas)
10. [Known issues in the current code](#10-known-issues-in-the-current-code)
11. [Security, legal and cost notes](#11-security-legal-and-cost-notes)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. How it works

```
 You (mic / browser / phone)
          │  audio
          ▼
 ┌──────────────────────┐        WebRTC         ┌───────────────────────┐
 │  LiveKit Room        │ ◄───────────────────► │  agent.py (worker)    │
 │  (LiveKit Cloud or   │                       │  AgentSession         │
 │   self-hosted)       │                       │   ├─ Noise cancel BVC │
 └──────────────────────┘                       │   └─ Gemini Live      │
                                                │       (speech in →    │
                                                │        speech out)    │
                                                └──────────┬────────────┘
                                                           │ tool calls
                                                           ▼
                                             Email / Phone / APIs / PC
```

1. **The worker starts.** `python agent.py dev` registers a worker with your LiveKit server, using `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` from `.env`.
2. **A user joins a room.** This can be from a browser (LiveKit Agents Playground), a phone call (SIP), or a mobile app. LiveKit then dispatches a job to your worker.
3. **`entrypoint()` runs.** It creates an `AgentSession` that uses `google.beta.realtime.RealtimeModel`. This is a *speech-to-speech* model: audio goes straight to Gemini and Gemini returns spoken audio, so there's no separate STT or TTS step and latency stays low.
4. **Personality.** `prompt.py → AGENT_INSTRUCTION` is the system prompt. It makes FRIDAY call you "Boss" and stay in character.
5. **Noise cancellation.** `noise_cancellation.BVC()` cleans up your mic audio before Gemini hears it.
6. **Tools.** When you add `@function_tool` methods (sections 6–8), Gemini can decide to call them, for example "send an email to Ravi". The Python function runs, and FRIDAY speaks the result.

---

## 2. Project structure

| File | Purpose |
|---|---|
| `agent.py` | Main voice agent: the `Assistant` class and `entrypoint` |
| `prompt.py` | Personality / system prompt (`AGENT_INSTRUCTION`, `AGENT_RESPONSE`) |
| `list_models.py` | Lists the Gemini models that support live (bidi) audio and writes them to `bidi_model_name.txt` |
| `requirements.txt` | Python dependencies |
| `.env` | Your secret keys (**never commit or share this file**) |

---

## 3. Setup

### 3.1 Requirements
- Python 3.10 or newer
- A free **LiveKit Cloud** project (https://cloud.livekit.io) or a self-hosted LiveKit server
- A **Google AI Studio** API key (https://aistudio.google.com/apikey)

### 3.2 Install

`requirements.txt` is inside `JARVIS\Jarvis_v1-main`, not in `D:\project\jarvis`, so `cd` into that folder first:

```powershell
cd D:\project\jarvis\JARVIS\Jarvis_v1-main
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks `Activate.ps1`, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 3.3 `.env`

```env
# LiveKit
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxx

# Google Gemini
GOOGLE_API_KEY=AIzaxxxxxxxxxxxxxxxx

# Email (section 7)
EMAIL_ADDRESS=you@gmail.com
EMAIL_APP_PASSWORD=abcd efgh ijkl mnop
IMAP_HOST=imap.gmail.com
SMTP_HOST=smtp.gmail.com
AUTOREPLY_MODE=draft          # draft = save replies as drafts, send = send automatically

# Phone (section 8)
SIP_OUTBOUND_TRUNK_ID=ST_xxxxxxxx
```

---

## 4. Running FRIDAY

```powershell
python agent.py console   # talk in the terminal with your PC mic and speakers (quickest test)
python agent.py dev       # connect to LiveKit and auto-reload when files change
python agent.py start     # production mode
```

To talk from a browser: run `dev`, then open https://agents-playground.livekit.io, sign in to the same LiveKit project, and click **Connect**.

To change the voice or model, edit `entrypoint()` in `agent.py`:
```python
google.beta.realtime.RealtimeModel(
    model="gemini-2.5-flash-native-audio-preview-12-2025",  # run list_models.py to see what's available
    voice="Aoede",        # other voices: Puck, Charon, Kore, Fenrir ...
    temperature=0.8,
)
```

---

## 5. Where you can use it

| Use case | How |
|---|---|
| **Personal desktop assistant** | `console` mode, plus tools that open apps, search the web or control music |
| **Email assistant** | Read, summarise and send email by voice; auto-reply to your inbox (section 7) |
| **Phone receptionist** | Answers your business number, takes messages, books appointments (section 8) |
| **Outbound calls** | Reminders, appointment confirmations, follow-ups (section 8) |
| **Customer support bot** | Website voice widget built with the LiveKit JS/React SDK |
| **Smart home** | Tools that call Home Assistant or MQTT APIs |
| **Mobile app** | LiveKit Flutter / Swift / Kotlin SDKs connect to the same agent |
| **Accessibility** | Hands-free computer use for people who can't type easily |

---

## 6. Adding tools (how FRIDAY "does things")

In LiveKit Agents 1.x, any `Agent` method decorated with `@function_tool()` becomes a tool Gemini can call. Gemini uses the **docstring** and **parameter names** to decide when and how to call it, so write them clearly.

```python
from livekit.agents import Agent, RunContext, function_tool
import datetime

class Assistant(Agent):
    def __init__(self):
        super().__init__(instructions=AGENT_INSTRUCTION)

    @function_tool()
    async def get_time(self, context: RunContext) -> str:
        """Get the current local date and time."""
        return datetime.datetime.now().strftime("%A %d %B %Y, %I:%M %p")
```

Then try saying: *"Friday, what time is it?"*

Rules of thumb:
- Return a short string. FRIDAY reads it back in its own words.
- Run blocking work (SMTP, `requests`) through `await asyncio.to_thread(...)` so the voice doesn't freeze.
- For anything that's hard to undo, such as sending email or placing a call, tell the model in the docstring to **confirm with Boss first**.

---

## 7. Email: send by voice + auto-reply

There are two separate features:
- **A. Voice tools.** "Friday, read my latest emails" or "Friday, email Ravi that I'll be late".
- **B. Auto-reply bot.** A background script that checks your inbox and drafts or sends replies with Gemini.

### 7.1 Gmail setup (one time)
1. Turn on **2-Step Verification** on your Google account.
2. Go to https://myaccount.google.com/apppasswords and create an **App Password**.
3. Put that 16-character password in `.env` as `EMAIL_APP_PASSWORD`. **Don't** use your normal Gmail password.
4. Gmail → Settings → *Forwarding and POP/IMAP* → **Enable IMAP**.

Outlook / Office 365 and other providers work the same way. Change `IMAP_HOST` / `SMTP_HOST` to the provider's servers (for example `outlook.office365.com` / `smtp.office365.com`).

Add this to `requirements.txt`:
```
google-genai
```

### 7.2 `email_tools.py` (shared helpers)

```python
import email
import imaplib
import os
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

EMAIL = os.environ["EMAIL_ADDRESS"]
PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")


def decode(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def get_text_body(msg: email.message.Message) -> str:
    """Return the plain-text part of an email."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


def send_email(to: str, subject: str, body: str, in_reply_to: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = EMAIL
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
        smtp.login(EMAIL, PASSWORD)
        smtp.send_message(msg)


def save_draft(to: str, subject: str, body: str, in_reply_to: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = EMAIL
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    drafts = '"[Gmail]/Drafts"' if "gmail" in IMAP_HOST else "Drafts"
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(EMAIL, PASSWORD)
        imap.append(drafts, r"\Draft", imaplib.Time2Internaldate(email.utils.localtime()), msg.as_bytes())


def latest_emails(count: int = 5, unread_only: bool = False) -> list[dict]:
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(EMAIL, PASSWORD)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "UNSEEN" if unread_only else "ALL")
        ids = data[0].split()[-count:]
        out = []
        for i in reversed(ids):
            _, msg_data = imap.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            out.append({
                "from": decode(msg["From"]),
                "subject": decode(msg["Subject"]),
                "body": get_text_body(msg)[:1500],
            })
        return out
```

### 7.3 A. Voice email tools (add to `agent.py`)

```python
import asyncio
from livekit.agents import RunContext, function_tool
import email_tools

class Assistant(Agent):
    ...

    @function_tool()
    async def read_emails(self, context: RunContext, count: int = 5, unread_only: bool = True) -> str:
        """Read Boss's latest emails. Summarise them briefly; don't read the whole body aloud unless asked."""
        mails = await asyncio.to_thread(email_tools.latest_emails, count, unread_only)
        if not mails:
            return "No emails found."
        return "\n\n".join(f"From: {m['from']}\nSubject: {m['subject']}\n{m['body'][:400]}" for m in mails)

    @function_tool()
    async def send_email(self, context: RunContext, to: str, subject: str, body: str) -> str:
        """Send an email for Boss. ALWAYS read the recipient, subject and body back to Boss
        and get a clear 'yes' before calling this tool."""
        await asyncio.to_thread(email_tools.send_email, to, subject, body)
        return f"Email sent to {to}."
```

> Email addresses are hard to dictate by voice. Keep a small contacts dict (`{"ravi": "ravi@example.com"}`) and add a `lookup_contact` tool.

### 7.4 B. Auto-reply bot: `email_autoreply.py`

This runs separately from the voice agent: `python email_autoreply.py`. Every 60 seconds it checks for unread mail, asks Gemini to write a reply in FRIDAY's style, and either **saves a draft** (the safe default) or **sends** it.

```python
import email
import imaplib
import os
import time
from email.utils import parseaddr

from dotenv import load_dotenv
from google import genai

import email_tools as et

load_dotenv()
MODE = os.getenv("AUTOREPLY_MODE", "draft")   # "draft" or "send"
CHECK_EVERY = 60                              # seconds
client = genai.Client()                       # uses GOOGLE_API_KEY from .env

SKIP_SENDERS = ("noreply", "no-reply", "donotreply", "mailer-daemon", "notifications")

REPLY_PROMPT = """You are the email assistant for Boss ({me}).
Write a short, polite, professional reply to the email below.
- If it needs Boss's personal decision (money, meetings, legal), say Boss will get back to them soon.
- Never invent facts, prices, dates or promises.
- Plain text only, no subject line. Sign off as "FRIDAY, assistant to {me}".

From: {sender}
Subject: {subject}

{body}
"""


def should_skip(msg) -> bool:
    sender = parseaddr(msg["From"])[1].lower()
    if sender == et.EMAIL.lower() or any(s in sender for s in SKIP_SENDERS):
        return True
    # Don't reply to automatic mail or mailing lists (prevents reply loops)
    if msg.get("Auto-Submitted", "no").lower() != "no":
        return True
    if msg.get("List-Unsubscribe") or msg.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return True
    return False


def process_inbox():
    with imaplib.IMAP4_SSL(et.IMAP_HOST) as imap:
        imap.login(et.EMAIL, et.PASSWORD)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")   # marks the email as read
            msg = email.message_from_bytes(msg_data[0][1])
            if should_skip(msg):
                continue

            sender = parseaddr(msg["From"])[1]
            subject = et.decode(msg["Subject"])
            body = et.get_text_body(msg)[:4000]

            reply = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=REPLY_PROMPT.format(me=et.EMAIL, sender=sender, subject=subject, body=body),
            ).text

            re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            if MODE == "send":
                et.send_email(sender, re_subject, reply, in_reply_to=msg["Message-ID"])
                print(f"✉️  Sent reply to {sender}: {subject}")
            else:
                et.save_draft(sender, re_subject, reply, in_reply_to=msg["Message-ID"])
                print(f"📝 Draft saved for {sender}: {subject}")


if __name__ == "__main__":
    print(f"FRIDAY auto-reply running in '{MODE}' mode. Ctrl+C to stop.")
    while True:
        try:
            process_inbox()
        except Exception as e:
            print("Error:", e)
        time.sleep(CHECK_EVERY)
```

**Recommended workflow:** run in `draft` mode for a week, check the drafts in Gmail, adjust `REPLY_PROMPT`, and switch to `send` only once you trust it.

Optional upgrades:
- Only auto-reply to certain senders or labels (`imap.select('"Label Name"')`).
- Have FRIDAY announce new mail by voice: run `process_inbox` as an `asyncio` task inside the agent and call `session.say(...)`.
- Use the official Gmail API with OAuth instead of an App Password if this is for a company.

---

## 8. Phone calls: inbound and outbound

LiveKit connects to the normal phone network through **SIP**. You need a SIP trunk provider, such as **Twilio Elastic SIP Trunking**, **Telnyx**, Plivo or Exotel. India numbers are easier through Exotel or Plivo.

```
Phone network ──► Twilio/Telnyx number ──SIP──► LiveKit SIP ──► Room ──► FRIDAY
```

### 8.1 One-time setup

1. **Install the LiveKit CLI** (`lk`): https://docs.livekit.io/home/cli/
   ```powershell
   winget install LiveKit.LiveKitCLI
   lk cloud auth
   ```
2. **Buy a phone number** from your SIP provider and create an **Elastic SIP Trunk**:
   - *Origination* (inbound) URI: your LiveKit SIP URI from LiveKit Cloud → Settings → *SIP URI* (looks like `sip:xxxx.sip.livekit.cloud`).
   - *Termination* (outbound): note the termination URI (for example `friday.pstn.twilio.com`) and create a username and password credential.
   - Attach the number to the trunk.

### 8.2 Name your agent (explicit dispatch)

For telephony, give the worker a name so LiveKit dispatches it to calls. In `agent.py`:

```python
if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="friday"))
```

> When `agent_name` is set, the agent **no longer auto-joins** every room. The Playground needs an explicit dispatch too (`lk dispatch create --room <room> --agent-name friday`).

### 8.3 Inbound calls (FRIDAY answers your number)

`inbound-trunk.json`:
```json
{
  "trunk": {
    "name": "FRIDAY inbound",
    "numbers": ["+15551234567"],
    "krispEnabled": true
  }
}
```

`dispatch-rule.json` (every caller gets their own room with FRIDAY in it):
```json
{
  "dispatch_rule": {
    "name": "FRIDAY dispatch",
    "rule": { "dispatchRuleIndividual": { "roomPrefix": "call-" } },
    "roomConfig": { "agents": [ { "agentName": "friday" } ] }
  }
}
```

```powershell
lk sip inbound create inbound-trunk.json
lk sip dispatch create dispatch-rule.json
python agent.py dev
```

Now call your number and FRIDAY answers.

### 8.4 Outbound calls (FRIDAY calls someone)

`outbound-trunk.json`:
```json
{
  "trunk": {
    "name": "FRIDAY outbound",
    "address": "friday.pstn.twilio.com",
    "numbers": ["+15551234567"],
    "authUsername": "your_sip_username",
    "authPassword": "your_sip_password"
  }
}
```

```powershell
lk sip outbound create outbound-trunk.json   # prints ST_xxxx -> put it in .env as SIP_OUTBOUND_TRUNK_ID
```

Update `entrypoint()` so it dials out when the job metadata contains a phone number:

```python
import json
import os
from livekit import api

async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    meta = json.loads(ctx.job.metadata or "{}")
    phone_number = meta.get("phone_number")      # set for outbound calls
    purpose = meta.get("purpose", "")            # e.g. "Remind Ravi about the 5pm meeting"

    instructions = AGENT_INSTRUCTION
    if phone_number:
        instructions += (
            f"\n\nYou are on a PHONE CALL with someone who is NOT Boss. "
            f"Introduce yourself as FRIDAY, Boss's assistant. Purpose of the call: {purpose}. "
            f"Be brief and polite, and end the call once the purpose is done."
        )

    session = AgentSession(
        llm=google.beta.realtime.RealtimeModel(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Aoede",
            temperature=0.8,
        ),
    )

    await session.start(
        room=ctx.room,
        agent=Agent(instructions=instructions),   # or your Assistant class with these instructions
        room_input_options=RoomInputOptions(
            # BVCTelephony is tuned for phone audio
            noise_cancellation=noise_cancellation.BVCTelephony() if phone_number else noise_cancellation.BVC(),
        ),
    )

    if phone_number:
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=os.environ["SIP_OUTBOUND_TRUNK_ID"],
                    sip_call_to=phone_number,
                    participant_identity=phone_number,
                    wait_until_answered=True,
                )
            )
            await session.generate_reply(instructions="Greet the person and explain why you are calling.")
        except api.TwirpError as e:
            print(f"Call failed: {e.message} (SIP status {e.metadata.get('sip_status_code')})")
            ctx.shutdown()
```

**Start a call from the terminal:**
```powershell
lk dispatch create --new-room --agent-name friday --metadata '{\"phone_number\": \"+919876543210\", \"purpose\": \"Remind Ravi about the 5pm meeting\"}'
```

**Or by voice ("Friday, call Ravi and remind him about the meeting").** Add this tool to `Assistant`:

```python
import json, uuid
from livekit import api

@function_tool()
async def make_phone_call(self, context: RunContext, phone_number: str, purpose: str) -> str:
    """Place a phone call for Boss. phone_number must be in international format (+91...).
    Confirm the number and purpose with Boss before calling."""
    lkapi = api.LiveKitAPI()   # reads LIVEKIT_URL / API_KEY / API_SECRET from env
    try:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="friday",
                room=f"call-{uuid.uuid4().hex[:8]}",
                metadata=json.dumps({"phone_number": phone_number, "purpose": purpose}),
            )
        )
    finally:
        await lkapi.aclose()
    return f"Calling {phone_number} now, Boss."
```

### 8.5 Useful phone extras
- **Hang up from the agent:** add a tool `end_call` that runs `await get_job_context().api.room.delete_room(api.DeleteRoomRequest(room=get_job_context().room.name))`.
- **Transfer to your real phone:** use `ctx.api.sip.transfer_sip_participant(...)`. This is a "let me connect you to Boss" feature.
- **Call summaries:** when the call ends, send yourself an email summary with the `send_email` helper from section 7.
- **Voicemail detection:** tell the model in its instructions that if it hears a voicemail greeting, it should leave a short message and end the call.

---

## 9. More ideas

| Feature | Approach |
|---|---|
| Web search | Tool that calls Tavily / SerpAPI / DuckDuckGo |
| Open apps on PC | Tool that uses `subprocess.Popen(["notepad"])` (desktop only; be careful) |
| Calendar | Google Calendar API tool: "What's on my schedule tomorrow?" |
| WhatsApp / SMS | Twilio Messaging API tool |
| Weather | Tool that calls the Open-Meteo API (free, no key) |
| Memory | Save facts to a JSON or SQLite file with `remember` / `recall` tools |
| Vision | Enable video on the realtime model + `RoomInputOptions(video_enabled=True)` so FRIDAY can see your camera or screen |

---

## 10. Known issues in the current code

- **`on_message` and `on_session_start` are never called.** These aren't LiveKit Agents 1.x hooks, so the "sleep now" / "Friday" wake-word logic, the idle monitor and the emotion detection don't currently run. The real hooks are:
  - `async def on_enter(self)`: runs when the agent becomes active. Start the idle monitor here.
  - `async def on_user_turn_completed(self, turn_ctx, new_message)`: runs after each user utterance. `new_message.text_content` holds the text. Put emotion or wake-word logic here. To skip responding, `raise StopResponse()` (from `livekit.agents`).
- `asyncio.get_event_loop().time()` in `__init__` should be `time.monotonic()`.
- `list_models.py` imports `google.generativeai`, which isn't in `requirements.txt`. Install it with `pip install google-generativeai`, or rewrite it with `google-genai`.
- The stray file named `Get-ExecutionPolicy` in the project folder was created by accident and can be deleted.
- `prompt.py` mentions "Boss (Ghost)" and "him". Change these to suit you.

---

## 11. Security, legal and cost notes

- **Never commit `.env`.** Add it to `.gitignore`. If your keys were ever shared, rotate them.
- **Email auto-send** can embarrass you or leak information. Start in `draft` mode and keep the "never invent facts" rule in the prompt.
- **Phone calls:** many countries require you to **tell people they're talking to an AI** and to get **consent before recording**. Automated or marketing calls are regulated (for example the TCPA in the US and TRAI DND rules in India). Only call people who expect it.
- **Costs:** Gemini Live is billed per audio token, SIP providers charge per minute plus a monthly number fee, and LiveKit Cloud has a free tier and then per-minute charges. Set budget alerts.

---

## 12. Troubleshooting

| Problem | Fix |
|---|---|
| `Could not open requirements file` | You're in the wrong folder. `cd D:\project\jarvis\JARVIS\Jarvis_v1-main` first |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Agent never joins the Playground room | If `agent_name` is set, create a dispatch manually (section 8.2) |
| `model not found` / 1008 error | Run `python list_models.py` and use a model listed in `bidi_model_name.txt` |
| Gmail `AUTHENTICATIONFAILED` | Use an App Password, and make sure IMAP is enabled |
| Outbound call fails with SIP 403/404 | Check the trunk address, credentials and number format (`+countrycode...`) |
| FRIDAY doesn't answer phone calls | Check the dispatch rule `agentName` matches `agent_name="friday"` and the worker is running |
