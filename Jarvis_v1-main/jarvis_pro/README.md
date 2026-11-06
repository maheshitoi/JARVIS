# JARVIS PRO — Permission-Controlled AI Agent

JARVIS PRO is the advanced version of JARVIS. It's a real-time voice agent that can work with your **Windows PC and screen**, **email**, **phone calls** and **social media**. A single **access file** (`access.yaml`) controls everything it's allowed to do, and a **web dashboard** lets you talk to it, share your screen, approve risky actions, and see every action it takes.

> The original project (`../agent.py`, `../prompt.py`) is unchanged and still works on its own.

---

## Contents
1. [Architecture](#1-architecture)
2. [The access file](#2-the-access-file-accessyaml)
3. [Quick start](#3-quick-start)
4. [The dashboard](#4-the-dashboard)
5. [Capabilities & setup](#5-capabilities--setup)
6. [Adding your own tool](#6-adding-your-own-tool)
7. [Security model](#7-security-model)
8. [Folder structure](#8-folder-structure)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Architecture

```
            ┌───────────── Browser dashboard (web/) ─────────────┐
            │ voice orb · transcript · access panel · activity    │
            │ screen share · camera · approval popups             │
            └──────────────┬──────────────────────────────────────┘
          token (server.py)│  WebRTC audio/video + data + RPC
                           ▼
 Phone (SIP) ──────► LiveKit Room ◄──────► agent.py  (LiveKit worker)
                                               │
                                     AgentSession + Gemini Live (speech↔speech, sees video)
                                               │ tool call
                                               ▼
                                   ┌──── guard.action() ─────┐
                                   │ 1. access.yaml level    │  deny → refused
                                   │ 2. confirm → approval   │  dashboard popup / spoken yes
                                   │ 3. audit log + live feed│
                                   └──────────┬──────────────┘
                                              ▼
            memory · system · screen · email · phone · X · Facebook · Instagram · LinkedIn

 workers/email_autoreply.py  — background inbox auto-replies (same access.yaml + audit log)
```

**Session types** (picked automatically in `agent.py`):

| Who connects | Persona | Tools |
|---|---|---|
| Dashboard / `console` | **OwnerAgent**: full assistant | Everything enabled in `access.yaml` |
| Unknown phone caller | **CallerAgent**: receptionist | `take_message`, `transfer_to_owner`, `end_call` only |
| Your own number (`trust_caller_id: true`) | OwnerAgent | Everything, with spoken confirmation |
| Outbound call from `make_phone_call` | CallerAgent with a purpose | `take_message`, `end_call` |

Phone callers can **never** trigger owner actions, even if they ask JARVIS to. The permission gate refuses them in code, so this doesn't rely on the prompt.

---

## 2. The access file (`access.yaml`)

This is the one file that answers *"what can JARVIS access?"*.

```yaml
capabilities:
  email:
    enabled: true          # master switch
    actions:
      read: allow          # JARVIS may do it on its own
      draft: allow
      send: confirm        # JARVIS must get approval first
```

| Level | Meaning |
|---|---|
| `allow` | JARVIS does it without asking |
| `confirm` | Approve/Deny popup on the dashboard. With no dashboard open, a spoken "yes" counts (`approval.allow_voice_confirmation`) |
| `deny` | Blocked. Anything missing or misspelled also counts as deny |

The full list of access JARVIS can have:

| Capability | Actions | Default |
|---|---|---|
| `memory` | `read`, `write` | on |
| `system` | `info`, `open_url`, `open_app` (only apps in the `apps:` list) | on, `open_app` = confirm |
| `screen` | `view` (screenshot), `live_share` (dashboard screen share), `windows`, `control` (typing/hotkeys) | on, `control` = **deny** |
| `email` | `read`, `draft`, `send`, plus `auto_reply` settings | off |
| `phone` | `call_out`, `read_messages`, `take_message`, `end_call`, `transfer` | off |
| `social.x` | `post` | off |
| `social.facebook` | `read`, `post`, `reply` | off |
| `social.instagram` | `read`, `post` | off |
| `social.linkedin` | `post` | off |
| `social.whatsapp` | `view`, `read`, `edit` (type without sending), `send` | on, `send` = confirm |

- **Levels apply live.** Save the file and the next action uses the new level.
- **Turning `enabled:` on or off** changes which tools JARVIS knows about, so reconnect afterwards.
- **Secrets never go in `access.yaml`.** They live in `.env`.

---

## 3. Quick start

```powershell
cd D:\project\jarvis\JARVIS\Jarvis_v1-main\jarvis_pro
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # fill in LIVEKIT_* and GOOGLE_API_KEY (or keep them in ../.env)
```

Open **two terminals**:

```powershell
# Terminal 1 — the agent
python agent.py dev

# Terminal 2 — the dashboard
python server.py
```

Open **http://127.0.0.1:8000** and click **Connect**.

Other ways to run:
```powershell
python agent.py console                    # voice in the terminal only, no dashboard
python workers/email_autoreply.py          # background email auto-reply
python workers/email_autoreply.py --once   # single pass (for Windows Task Scheduler)
```

---

## 4. The dashboard

| Area | What it does |
|---|---|
| **Orb** | Shows JARVIS's state (listening, thinking, speaking, awaiting approval) and reacts to the voices |
| **Controls** | Connect, mute, **share screen** (JARVIS watches it live), camera |
| **Type box** | Text chat with JARVIS when you can't talk |
| **Access panel** | Live view of `access.yaml`: every capability, its risk, and each action's level |
| **Conversation tab** | Live transcript of both sides |
| **Activity tab** | Every action: running, done, blocked, denied, failed, and who approved it |
| **Approval popup** | Appears for `confirm` actions, with the full details and a countdown. No answer = deny |

---

## 5. Capabilities & setup

### 5.1 Screen (Windows)
- **"What's on my screen?" / "Read this error"**: JARVIS takes a screenshot of your primary monitor and analyses it with Gemini (`screen.view`).
- **Live**: click **Share screen** in the dashboard and JARVIS sees it continuously (`screen.live_share`).
- **"Which windows are open?"**: `screen.windows`.
- **Keyboard control** (`switch_to_window`, `type_text`, `press_hotkey`): off by default. Set `control: confirm` to enable it, and never `allow`.

### 5.2 System
- "How's my PC doing?" returns CPU, RAM, disk, battery and the time.
- "Open VS Code" works only for names listed under `system.apps`. JARVIS can't run arbitrary commands.

### 5.3 Email
1. For Gmail: turn on 2-Step Verification, create an **App Password** (https://myaccount.google.com/apppasswords), and enable IMAP.
2. Fill in `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `IMAP_HOST`, `SMTP_HOST` and `SMTP_PORT` in `.env`. For Outlook use `outlook.office365.com`, `smtp.office365.com` and port `587`.
3. Set `email.enabled: true` in `access.yaml`.

Voice: *"Any important emails?"*, *"Reply to Ravi that I'll send the report tomorrow."*

**Auto-reply:** set `email.auto_reply.enabled: true` and keep `mode: draft` until you trust it. The worker:
- skips no-reply senders, newsletters, mailing lists and auto-generated mail (prevents reply loops)
- can be limited to certain senders or domains with `only_from`
- **never changes your read/unread flags**; it tracks handled mail in `data/autoreply_seen.json`
- logs every reply to the Activity feed

### 5.4 Phone calls (LiveKit SIP)
You need a SIP trunk provider (Twilio Elastic SIP, Telnyx, Plivo, or Exotel for India) and the LiveKit CLI (`winget install LiveKit.LiveKitCLI`, then `lk cloud auth`).

**Inbound (JARVIS answers your number):**
```json
// inbound-trunk.json
{ "trunk": { "name": "JARVIS inbound", "numbers": ["+15551234567"], "krispEnabled": true } }
```
```json
// dispatch-rule.json
{ "dispatch_rule": {
    "name": "JARVIS",
    "rule": { "dispatchRuleIndividual": { "roomPrefix": "call-" } },
    "roomConfig": { "agents": [ { "agentName": "jarvis-pro" } ] } } }
```
```powershell
lk sip inbound create inbound-trunk.json
lk sip dispatch create dispatch-rule.json
```
Callers can leave messages (saved to `data/phone_messages.jsonl`, and emailed to you if email is on). They can also be transferred to `phone.transfer_number`, which must be allowed by your provider.

**Outbound (JARVIS calls for you):**
```json
// outbound-trunk.json
{ "trunk": { "name": "JARVIS outbound", "address": "yourtrunk.pstn.twilio.com",
             "numbers": ["+15551234567"], "authUsername": "...", "authPassword": "..." } }
```
```powershell
lk sip outbound create outbound-trunk.json   # put the ST_xxx id in .env as SIP_OUTBOUND_TRUNK_ID
```
Then say: *"Jarvis, call Ravi and confirm tomorrow's 5pm meeting."* JARVIS dispatches a separate call session and records the outcome with `take_message`. Ask *"any phone messages?"* to hear it. Numbers are limited to `phone.allowed_country_codes`.

### 5.5 Social media
| Platform | What you need (.env) | Notes |
|---|---|---|
| **X** | `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | developer.x.com app with **Read and Write** permission. Posting works on the free tier |
| **Facebook Page** | `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN` | Meta developer app; page token with `pages_manage_posts`, `pages_read_engagement`, `pages_manage_engagement` |
| **Instagram** | `META_IG_USER_ID`, same page token | Must be a **Business/Creator** account linked to the Page; `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`. Images must be public https URLs |
| **LinkedIn** | `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_PERSON_ID` | LinkedIn app with *Share on LinkedIn* (`w_member_social`) |

Personal Instagram accounts and personal Facebook profiles have **no official posting API**. Tools that automate them break platform rules and get accounts banned, so JARVIS doesn't do that.

### 5.6 WhatsApp (Desktop app)
Personal WhatsApp has no API, so JARVIS **operates the WhatsApp Desktop app on your screen**. It focuses the window, reads it with Gemini vision, and clicks and pastes like you would. Requirements: WhatsApp Desktop installed and logged in, and the screen unlocked.

| Say | Tool | Permission |
|---|---|---|
| "Any new WhatsApp messages?" | `whatsapp_overview` | `view` |
| "Read my chat with Mom" | `whatsapp_read_chat` | `read` |
| "Write 'running late' to Ravi but don't send it" | `whatsapp_write_message` | `edit` |
| "Send Ravi 'running late, 10 min'" | `whatsapp_send_message` | `send` (approval) |

- Chats open by phone number when the contact is in `data/contacts.json`, which is the most reliable way. Otherwise JARVIS uses WhatsApp's search box.
- Before typing, JARVIS checks that the open chat's name matches the person you asked for. If it doesn't match, it stops.
- Emoji and any language work, because the text is pasted from the clipboard. **This replaces whatever is on your clipboard.**
- Each action takes a few seconds and briefly uses your mouse and keyboard, so don't touch them meanwhile.
- JARVIS can only read messages that are visible on screen, usually the last 10–20.

### 5.7 Websites + daily report (MCP)
JARVIS monitors your websites through its own **MCP server** (`mcp_servers/website_monitor.py`).

**1. Add your sites** in `access.yaml`:
```yaml
  websites:
    sites:
      - name: "My Shop"
        url: "https://myshop.com"
        keywords: ["Add to cart"]     # optional: text that must be on the page
    report:
      time: "09:00"                   # daily report time
      check_every_minutes: 15
      email_to_owner: true            # emails the report to EMAIL_ADDRESS
```

**2. Keep the monitor running** for uptime history and the daily report:
```powershell
python workers/site_monitor.py            # runs forever
```
Or use Windows Task Scheduler with `--check` every 15 minutes and `--report` once a day.

**What's checked:** up/down, HTTP status, response time, SSL certificate expiry, expected text, title, meta description, H1, images without alt text, page size, and broken internal links (in the daily report).

**Where reports go:** `reports/YYYY-MM-DD.html` and `.md`, the **Websites** tab on the dashboard, your email, and JARVIS by voice:
- *"Jarvis, are my websites up?"* runs `check_all_websites`
- *"How was My Shop's uptime this week?"* runs `website_history`
- *"Read me today's website report."* runs `get_website_report`

**Using it from other MCP clients.** The same server works in Claude Desktop, Claude Code or any other MCP client:
```json
"mcpServers": {
  "jarvis-websites": {
    "command": "python",
    "args": ["D:/project/jarvis/JARVIS/Jarvis_v1-main/jarvis_pro/mcp_servers/website_monitor.py"]
  }
}
```
For HTTP transport, run `python mcp_servers/website_monitor.py --http`, which serves `http://127.0.0.1:8765/mcp`.

**Adding more MCP servers** (for example an analytics or Search Console MCP): add them under `mcp.servers` in `access.yaml`. Use `url` for remote servers or `command` for local ones, and `allowed_tools` to limit them. JARVIS's approval gate does **not** check each call to an external MCP tool, so only add servers you trust.

---

## 6. Adding your own tool

1. Write it in `jarvis/tools/<area>_tools.py`:
```python
@function_tool
async def get_weather(context: RunContext, city: str) -> str:
    """Get the current weather for a city."""
    async with action(context, "web.weather", f"Weather for {city}"):
        ...  # call an API
    return "22°C and sunny"

TOOLS = [("web.weather", get_weather)]
```
2. Register the module in `jarvis/tools/__init__.py`.
3. Add it to `access.yaml`:
```yaml
  web:
    description: "Weather and web lookups"
    risk: low
    enabled: true
    actions:
      weather: allow
```
The new capability appears in the dashboard's access panel automatically.

---

## 7. Security model

- **Deny by default.** Unknown capabilities, missing actions and a missing access file all mean *deny*. If `access.yaml` has a syntax error, the last good policy stays in force.
- **Least knowledge.** Disabled tools are never sent to the model.
- **Checked twice.** Each tool re-checks the policy when it runs, so live edits take effect.
- **Human in the loop.** `confirm` actions show the exact details (recipient, text, number) before anything happens. A timeout counts as deny.
- **Caller isolation.** Phone callers get a separate persona and tool set, and the gate refuses owner actions in caller sessions.
- **Audit trail.** Every attempt is logged to `logs/audit.jsonl` with secrets redacted.
- **Local dashboard.** `server.py` binds to `127.0.0.1` and refuses to listen on the network without `JARVIS_UI_PASSWORD`.
- **Secrets** live only in `.env`, which is git-ignored.

**Legal:** tell people when they're speaking to an AI, get consent before recording calls, follow local calling and telemarketing rules (TCPA, TRAI DND), and follow each platform's automation policy.

---

## 8. Folder structure

```
jarvis_pro/
├── access.yaml              ← THE access file
├── agent.py                 ← voice agent entrypoint (owner / caller / outbound sessions)
├── server.py                ← dashboard server + token API
├── requirements.txt
├── .env.example
├── jarvis/
│   ├── config.py            ← paths, env vars, model names
│   ├── access.py            ← loads access.yaml, hot reload, levels
│   ├── guard.py             ← permission gate, approvals, events
│   ├── audit.py             ← audit log
│   ├── agents.py            ← OwnerAgent / CallerAgent personas
│   ├── genai_client.py
│   └── tools/
│       ├── memory_tools.py  ← remember / recall / contacts
│       ├── system_tools.py  ← PC status, apps, websites
│       ├── screen_tools.py  ← screenshot vision, windows, keyboard
│       ├── email_tools.py   ← read / draft / send
│       ├── phone_tools.py   ← calls, messages, transfer, hang up
│       └── social_tools.py  ← X, Facebook, Instagram, LinkedIn
├── workers/
│   └── email_autoreply.py
├── web/                     ← dashboard (index.html, styles.css, app.js)
├── data/                    ← memory, contacts, phone messages (local, git-ignored)
└── logs/                    ← audit.jsonl
```

To use contacts, copy `data/contacts.example.json` to `data/contacts.json` and add people. Then *"email Ravi"* and *"call Ravi"* work by name.

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard says "LIVEKIT_URL … missing" | Fill in `.env` (or `../.env`) |
| Connected, but JARVIS never joins | `agent.py dev` must be running, and `JARVIS_AGENT_NAME` must match in both processes |
| "Access denied: … turned off" | Enable it in `access.yaml` and reconnect |
| Approval popup never appears | It only appears when the dashboard is connected. In `console` mode, confirm by voice |
| Screenshot fails | `pip install mss Pillow`; the screenshot works on the primary monitor of the PC running `agent.py` |
| JARVIS can't see the screen share | `screen.live_share` must not be `deny`; reconnect after changing it |
| Gmail `AUTHENTICATIONFAILED` | Use an App Password and enable IMAP |
| Outbound call fails (SIP 403/404) | Check the trunk address, credentials, and that the number is in E.164 format (`+91…`) |
| Meta API error "(#200) permissions" | The token is missing a permission from section 5.5, or the app is in dev mode |
