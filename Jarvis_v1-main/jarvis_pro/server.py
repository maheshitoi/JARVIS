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

from jarvis import audit, config, websites  # noqa: E402
from jarvis.access import DENY, get_policy  # noqa: E402
from jarvis.guard import DEVICE_ATTR, OWNER_ROLE_ATTR  # noqa: E402

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
    if peer in ("127.0.0.1", "::1") and request.headers.get("cf-connecting-ip"):
        return request.headers["cf-connecting-ip"]
    return peer


def check_key(request: Request, x_jarvis_key: str = Header(default=""),
              jarvis_key: str = Cookie(default="")) -> None:
    if not PASSWORD:
        return
    ip, now = client_ip(request), time.time()
    if _locked_until.get(ip, 0) > now:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {int(_locked_until[ip] - now) // 60 + 1} min.")
    # Header for API calls; cookie so links opened in a new tab (reports) also work.
    supplied = x_jarvis_key or jarvis_key
    if secrets.compare_digest(supplied.encode(), PASSWORD.encode()):
        _failures.pop(ip, None)
        return
    if supplied:  # an actual wrong guess (not just a first visit without a password)
        recent = [t for t in _failures[ip] if now - t < WINDOW_S] + [now]
        _failures[ip] = recent
        if len(recent) >= MAX_FAILURES:
            _locked_until[ip] = now + LOCKOUT_S
            _failures.pop(ip, None)
            audit.record({"capability": "dashboard.login", "summary": f"Locked out {ip} after {MAX_FAILURES} "
                          "wrong passwords", "status": "blocked"})
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
    local = peer in ("127.0.0.1", "::1") and not request.headers.get("cf-connecting-ip")
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
