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

from jarvis import audit, config, devices, websites  # noqa: E402
from jarvis.access import DENY, get_policy  # noqa: E402
from jarvis.guard import DEVICE_ATTR, DEVICE_ID_ATTR, DEVICE_NAME_ATTR, OWNER_ROLE_ATTR  # noqa: E402

app = FastAPI(title="JARVIS PRO", docs_url=None, redoc_url=None)
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


@app.post("/api/token", dependencies=[Depends(check_key)])
def token(request: Request) -> dict:
    url, key, secret = (config.env(n) for n in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    if not (url and key and secret):
        raise HTTPException(500, "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET missing in .env")
    room = f"jarvis-{uuid.uuid4().hex[:8]}"
    identity = f"owner-{uuid.uuid4().hex[:6]}"
    jwt = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(get_policy().owner_name)
        .with_attributes({OWNER_ROLE_ATTR: "owner", DEVICE_ATTR: device_of(request)})
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


if __name__ == "__main__":
    host = config.env("JARVIS_UI_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost") and not PASSWORD:
        sys.exit("Refusing to listen on a network address without JARVIS_UI_PASSWORD set.")
    uvicorn.run(app, host=host, port=int(config.env("JARVIS_UI_PORT", "8000")))
