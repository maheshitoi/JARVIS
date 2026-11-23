"""JARVIS PRO dashboard server.

    python server.py            -> http://127.0.0.1:8000

Serves the web dashboard and three small APIs:
  POST /api/token    LiveKit token for the dashboard (and dispatches the agent)
  GET  /api/access   capability summary from access.yaml
  GET  /api/audit    recent actions from logs/audit.jsonl

Listens on 127.0.0.1 only by default — anyone who can open the dashboard can
talk to JARVIS as the owner. If you expose it (JARVIS_UI_HOST=0.0.0.0), set
JARVIS_UI_PASSWORD too.
"""

from __future__ import annotations

import re
import secrets
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from livekit import api  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from contextlib import asynccontextmanager  # noqa: E402

from jarvis import audit, automations, background, business, calendar_feed, config, devices, insights, memory, notify, websites  # noqa: E402
from jarvis.access import DENY, get_policy  # noqa: E402
from jarvis.protocol import DEVICE_ATTR, DEVICE_ID_ATTR, DEVICE_NAME_ATTR, OWNER_ROLE_ATTR, WAKE_ATTR  # noqa: E402

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Scheduled automations + away-mode phone alerts keep running while nobody is connected.
    # Not on Vercel: functions stop between requests, and the jobs need the voice-agent packages.
    import asyncio

    task = None if config.ON_VERCEL else asyncio.create_task(background.run_forever())
    yield
    if task:
        task.cancel()


app = FastAPI(title="JARVIS PRO", docs_url=None, redoc_url=None, lifespan=lifespan)
PASSWORD = config.env("JARVIS_UI_PASSWORD") or ""

# ---- brute-force protection: 5 wrong passwords in 10 min -> that client is locked out 15 min
MAX_FAILURES, WINDOW_S, LOCKOUT_S = 5, 600, 900
_failures: dict[str, list[float]] = defaultdict(list)
_locked_until: dict[str, float] = {}


def client_ip(request: Request) -> str:
    """Real visitor IP. Behind Cloudflare Tunnel every request arrives from localhost,
    so trust Cloudflare's header only in that case (it can't be spoofed from outside)."""
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        if request.headers.get("cf-connecting-ip"):
            return request.headers["cf-connecting-ip"]
        if request.headers.get("x-forwarded-for"):  # ngrok and other local tunnels/proxies
            return request.headers["x-forwarded-for"].split(",")[0].strip()
    return peer


def _via_tunnel(request: Request) -> bool:
    return bool(request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"))


def _check_lockout(ip: str) -> None:
    now = time.time()
    if _locked_until.get(ip, 0) > now:
        raise HTTPException(429, f"Too many wrong attempts. Try again in {int(_locked_until[ip] - now) // 60 + 1} min.")


def _record_failure(ip: str, what: str) -> None:
    now = time.time()
    recent = [t for t in _failures[ip] if now - t < WINDOW_S] + [now]
    _failures[ip] = recent
    if len(recent) >= MAX_FAILURES:
        _locked_until[ip] = now + LOCKOUT_S
        _failures.pop(ip, None)
        audit.record({"capability": what, "summary": f"Locked out {ip} after {MAX_FAILURES} "
                      "wrong attempts", "status": "blocked"})


def check_key(request: Request, x_jarvis_key: str = Header(default=""),
              jarvis_key: str = Cookie(default="")) -> None:
    if not PASSWORD:
        if config.ON_VERCEL:  # a public URL with no password would let anyone in
            raise HTTPException(503, "Set JARVIS_UI_PASSWORD in the Vercel project's environment variables.")
        return
    ip = client_ip(request)
    _check_lockout(ip)
    # Header for API calls; cookie so links opened in a new tab (reports) also work.
    supplied = x_jarvis_key or jarvis_key
    if secrets.compare_digest(supplied.encode(), PASSWORD.encode()):
        _failures.pop(ip, None)
        return
    if supplied:  # an actual wrong guess (not just a first visit without a password)
        _record_failure(ip, "dashboard.login")
    raise HTTPException(status_code=401, detail="Wrong or missing dashboard password")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"            # no embedding in other sites
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(self), display-capture=(self)"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Always revalidate the dashboard files so an update is picked up on a normal reload.
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/status")
def status() -> dict:
    return {"password_required": bool(PASSWORD), "agent_name": config.AGENT_NAME}


def device_of(request: Request) -> str:
    """'host' = browser on the PC that runs JARVIS; 'remote' = any other device (e.g. via the tunnel)."""
    peer = request.client.host if request.client else ""
    # Anything arriving through a tunnel (Cloudflare, ngrok) is another device, even though
    # the tunnel itself connects from localhost.
    local = peer in ("127.0.0.1", "::1") and not _via_tunnel(request)
    return "host" if local else "remote"


class TokenOptions(BaseModel):
    wake: str = ""  # what Boss said right after "Hey JARVIS" (wake word): answered instead of a greeting


@app.post("/api/token", dependencies=[Depends(check_key)])
def token(request: Request, body: TokenOptions | None = None) -> dict:
    url, key, secret = (config.env(n) for n in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    if not (url and key and secret):
        raise HTTPException(500, "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET missing in .env")
    room = f"jarvis-{uuid.uuid4().hex[:8]}"
    identity = f"owner-{uuid.uuid4().hex[:6]}"
    jwt = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(get_policy().owner_name)
        .with_attributes({OWNER_ROLE_ATTR: "owner", DEVICE_ATTR: device_of(request),
                          **({WAKE_ATTR: body.wake.strip()[:300]} if body and body.wake.strip() else {})})
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True,
                                     can_subscribe=True, can_publish_data=True))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=config.AGENT_NAME)]))
        .to_jwt()
    )
    return {"url": url, "token": jwt, "room": room, "identity": identity}


@app.post("/api/device/token")
def device_token(request: Request, x_device_key: str = Header(default="")) -> dict:
    """LiveKit token for the JARVIS Device app. Every connection gets its own private room, so the
    agent session it starts can only reach this one device."""
    ip = client_ip(request)
    _check_lockout(ip)
    device = devices.verify(x_device_key)
    if device is None:
        _record_failure(ip, "device.login")
        raise HTTPException(401, "Unknown, disabled or removed device key")
    _failures.pop(ip, None)
    url, key, secret = (config.env(n) for n in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    if not (url and key and secret):
        raise HTTPException(500, "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET missing in .env")
    room = f"dev-{device['id']}-{uuid.uuid4().hex[:6]}"
    identity = f"device-{device['id']}-{uuid.uuid4().hex[:4]}"
    jwt = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(device["name"])
        # Role and device come from the registry, inside the signed token. The device can't
        # change its own attributes (no can_update_own_metadata grant).
        .with_attributes({OWNER_ROLE_ATTR: device["role"], DEVICE_ATTR: "app",
                          DEVICE_ID_ATTR: device["id"], DEVICE_NAME_ATTR: device["name"]})
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True,
                                     can_subscribe=True, can_publish_data=True))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=config.AGENT_NAME)]))
        .to_jwt()
    )
    audit.record({"capability": "device.login", "summary": f"{device['name']} ({device['role']}) from {ip}",
                  "status": "ok"})
    return {"url": url, "token": jwt, "room": room, "identity": identity,
            "device": {"id": device["id"], "name": device["name"], "role": device["role"]}}


# ------------------------------------------------- devices & permissions --
class NewDevice(BaseModel):
    name: str
    role: str = "guest"


class PermissionChange(BaseModel):
    capability: str
    level: str  # allow | confirm | deny | default


class Decision(BaseModel):
    decision: str  # allow | confirm | deny


class Enabled(BaseModel):
    enabled: bool


def _device_or_404(device_id: str) -> dict:
    device = devices.get(device_id)
    if device is None:
        raise HTTPException(404, "No such device")
    return device


@app.get("/api/devices", dependencies=[Depends(check_key)])
async def devices_overview() -> dict:
    policy = get_policy()
    online = await devices.online_ids()
    rows = []
    for d in devices.all_devices():
        rows.append({**d, "online": d["id"] in online, "permissions_table": devices.permission_table(d, policy)})
    return {"devices": rows, "requests": devices.requests("pending"),
            "history": devices.requests(None)[:30]}


@app.post("/api/devices", dependencies=[Depends(check_key)])
def devices_add(body: NewDevice, request: Request) -> dict:
    try:
        device, key = devices.add(body.name, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # The device reaches the server at the same address this dashboard is using.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8000"
    server_url = f"{proto}://{host}"
    audit.record({"capability": "devices.manage", "summary": f"Registered {device['name']} ({device['role']})",
                  "status": "done", "approved_by": "dashboard"})
    yaml_text = (f"# JARVIS Device app config for: {device['name']}  (role: {device['role']})\n"
                 f"server_url: \"{server_url}\"\ndevice_key: \"{key}\"\n")
    return {"device": device, "device_yaml": yaml_text}


@app.delete("/api/devices/{device_id}", dependencies=[Depends(check_key)])
def devices_remove(device_id: str) -> dict:
    device = _device_or_404(device_id)
    devices.remove(device_id)
    audit.record({"capability": "devices.manage", "summary": f"Removed {device['name']}", "status": "done",
                  "approved_by": "dashboard"})
    return {"ok": True}


@app.post("/api/devices/{device_id}/enabled", dependencies=[Depends(check_key)])
def devices_enable(device_id: str, body: Enabled) -> dict:
    device = _device_or_404(device_id)
    devices.set_enabled(device_id, body.enabled)
    audit.record({"capability": "devices.manage", "status": "done", "approved_by": "dashboard",
                  "summary": f"{'Enabled' if body.enabled else 'Disabled'} {device['name']}"})
    return {"ok": True}


@app.post("/api/devices/{device_id}/permissions", dependencies=[Depends(check_key)])
def devices_permission(device_id: str, body: PermissionChange) -> dict:
    device = _device_or_404(device_id)
    try:
        updated = devices.set_permission(device_id, body.capability, body.level)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.record({"capability": "devices.manage", "status": "done", "approved_by": "dashboard",
                  "summary": f"{device['name']}: {body.capability} = {body.level}"})
    return {"device": updated, "permissions_table": devices.permission_table(updated, get_policy())}


@app.post("/api/permission-requests/{request_id}", dependencies=[Depends(check_key)])
def permission_decide(request_id: str, body: Decision) -> dict:
    try:
        req = devices.resolve_request(request_id, body.decision)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.record({"capability": "devices.manage", "status": "done", "approved_by": "dashboard",
                  "summary": f"{req['status'].capitalize()} {req['device_name']}: {req['capability']}"
                             + (f" ({body.decision})" if body.decision != "deny" else "")})
    return {"request": req}


@app.get("/api/access", dependencies=[Depends(check_key)])
def access() -> dict:
    policy = get_policy()
    return {"owner": policy.owner_name, "file": str(config.ACCESS_FILE), "capabilities": policy.summary()}


@app.get("/api/audit", dependencies=[Depends(check_key)])
def audit_log(limit: int = 100) -> list[dict]:
    return list(reversed(audit.tail(max(1, min(limit, 500)))))


class CustomerCall(BaseModel):
    who: str  # customer name / id from data/customers.json, or +91... number
    message: str  # what the AI must tell them


@app.post("/api/customers/call", dependencies=[Depends(check_key)])
async def customers_call(body: CustomerCall) -> dict:
    """Start an outbound "inform" call. Clicking this on the dashboard counts as your approval."""
    try:
        return await business.dispatch_customer_call(body.who, body.message)
    except business.CallError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/customers/calls", dependencies=[Depends(check_key)])
def customers_calls(limit: int = 20, transcript: bool = False) -> list[dict]:
    if get_policy().level("customer.read_calls") == DENY:
        raise HTTPException(403, "customer.read_calls is denied in access.yaml")
    return business.recent_calls(limit, with_transcript=transcript)


@app.get("/api/websites", dependencies=[Depends(check_key)])
def websites_status() -> dict:
    allowed = get_policy().level("websites.report") != DENY
    return {
        "allowed": allowed,
        "sites": websites.latest_status() if allowed else [],
        "reports": websites.list_reports()[:30] if allowed else [],
    }


@app.post("/api/websites/report", dependencies=[Depends(check_key)])
async def websites_generate() -> dict:
    if get_policy().level("websites.report") == DENY:
        raise HTTPException(403, "websites.report is denied in access.yaml")
    report = await websites.build_daily_report()
    return {"date": report["date"], "headline": report["headline"]}


@app.get("/reports/{day}", dependencies=[Depends(check_key)])
def report_page(day: str) -> FileResponse:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(404)
    path = websites.REPORTS_DIR / f"{day}.html"
    if not path.exists():
        raise HTTPException(404, "No report for that day")
    return FileResponse(path)


# ---- Control Center: insights, history & memory, settings ---------------------------------
@app.get("/api/insights", dependencies=[Depends(check_key)])
def insights_usage(days: int = 14) -> dict:
    try:
        return insights.usage(days)
    except Exception as e:  # noqa: BLE001 — show the reason on the dashboard
        return {"available": False, "reason": f"Could not read the database: {str(e).splitlines()[0]}"}


@app.get("/api/history", dependencies=[Depends(check_key)])
def history_list(q: str = "", limit: int = 40) -> dict:
    return {"items": memory.recent_conversations(limit=max(1, min(limit, 200)), search_text=q[:200]),
            "search": bool(q.strip())}


@app.get("/api/history/{session_id}", dependencies=[Depends(check_key)])
def history_session(session_id: str) -> list[dict]:
    if not re.fullmatch(r"[0-9a-f]{8,32}", session_id):
        raise HTTPException(404)
    return memory.session_lines(session_id)


@app.get("/api/memories", dependencies=[Depends(check_key)])
async def memories_list(q: str = "") -> list[dict]:
    if q.strip():
        return await memory.search(q[:200], limit=30)
    return insights.memories()


@app.delete("/api/memories/{memory_id}", dependencies=[Depends(check_key)])
def memories_delete(memory_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{6,32}", memory_id):
        raise HTTPException(404)
    if not insights.delete_memory(memory_id):
        raise HTTPException(404, "No such memory")
    audit.record({"capability": "memory.write", "summary": f"Deleted memory {memory_id} from the dashboard",
                  "status": "done", "mode": "owner"})
    return {"ok": True}


@app.get("/api/tasks", dependencies=[Depends(check_key)])
def tasks_list() -> list[dict]:
    return insights.task_history()


def _local_only(request: Request) -> None:
    """Keys and models can only be changed from the PC running JARVIS, never through a tunnel."""
    if _via_tunnel(request) or client_ip(request) not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Settings can only be changed on the PC running JARVIS.")


@app.get("/api/settings", dependencies=[Depends(check_key)])
def settings_get(request: Request) -> dict:
    return {**insights.settings(), "editable": not _via_tunnel(request)
            and client_ip(request) in ("127.0.0.1", "::1")}


class SettingsUpdate(BaseModel):
    env: dict[str, str] = {}
    proactive: dict[str, str | bool | int] = {}


@app.post("/api/settings", dependencies=[Depends(check_key)])
def settings_post(body: SettingsUpdate, request: Request) -> dict:
    _local_only(request)
    try:
        changed = insights.update_env(body.env) + [f"proactive.{k}" for k in insights.update_proactive(body.proactive)]
    except insights.SettingsError as e:
        raise HTTPException(400, str(e)) from e
    if changed:
        audit.record({"capability": "dashboard.settings", "summary": "Settings changed: " + ", ".join(changed),
                      "status": "done", "mode": "owner"})
    restart = any(not k.startswith("proactive.") for k in changed)
    return {"changed": changed, "restart_agent": restart}


@app.post("/api/settings/test-claude", dependencies=[Depends(check_key)])
async def settings_test_claude(request: Request) -> dict:
    _local_only(request)
    return await insights.test_claude()


# ------------------------------------------------------------ automations --
class NewAutomation(BaseModel):
    title: str
    job: str
    when: str
    notify: str = "auto"


def _automation_or_404(automation_id: str) -> dict:
    a = next((x for x in automations.load() if x["id"] == automation_id), None)
    if a is None:
        raise HTTPException(404, "No such automation")
    return a


@app.get("/api/automations", dependencies=[Depends(check_key)])
def automations_list() -> dict:
    return {"items": automations.load(), "notify_modes": automations.NOTIFY_MODES,
            "push_ready": notify.configured(), "calendar_ready": calendar_feed.configured(),
            "email_ready": bool(config.env("EMAIL_ADDRESS"))}


@app.post("/api/automations", dependencies=[Depends(check_key)])
def automations_create(body: NewAutomation) -> dict:
    if get_policy().level("automation.manage") == DENY:
        raise HTTPException(403, "automation.manage is denied in access.yaml")
    try:
        a = automations.create(body.title, body.job, body.when, body.notify)
    except automations.ScheduleError as e:
        raise HTTPException(400, str(e)) from e
    audit.record({"capability": "automation.manage", "summary": f"Automation created: {a['title']} ({a['schedule']})",
                  "status": "done", "mode": "owner"})
    return a


@app.post("/api/automations/{automation_id}/enabled", dependencies=[Depends(check_key)])
def automations_enable(automation_id: str, body: Enabled) -> dict:
    _automation_or_404(automation_id)
    return automations.set_enabled(automation_id, body.enabled) or {}


@app.post("/api/automations/{automation_id}/run", dependencies=[Depends(check_key)])
def automations_run(automation_id: str) -> dict:
    _automation_or_404(automation_id)
    automations.run_soon(automation_id)
    return {"ok": True}


@app.delete("/api/automations/{automation_id}", dependencies=[Depends(check_key)])
def automations_delete(automation_id: str) -> dict:
    a = _automation_or_404(automation_id)
    automations.delete(automation_id)
    audit.record({"capability": "automation.manage", "summary": f"Automation deleted: {a['title']}",
                  "status": "done", "mode": "owner"})
    return {"ok": True}


@app.post("/api/notify/test", dependencies=[Depends(check_key)])
async def notify_test() -> dict:
    if not notify.configured():
        raise HTTPException(400, "Set NTFY_TOPIC in .env first (and subscribe to it in the ntfy app).")
    ok = await notify.push("JARVIS test", "Push notifications are working.", "info")
    return {"ok": ok}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


if __name__ == "__main__":
    host = config.env("JARVIS_UI_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost") and not PASSWORD:
        sys.exit("Refusing to listen on a network address without JARVIS_UI_PASSWORD set.")
    uvicorn.run(app, host=host, port=int(config.env("JARVIS_UI_PORT", "8000")))
