"""Website monitoring engine — shared by the MCP server, the background monitor and the dashboard.

Checks per site: up/down, HTTP status, response time, redirects, SSL certificate expiry,
required keywords, basic SEO (title, meta description, H1, images without alt),
page size and (full check) broken internal links.

History : data/site_checks.jsonl   (one line per check)
Reports : reports/YYYY-MM-DD.html + .md
"""

from __future__ import annotations

import asyncio
import html
import json
import socket
import ssl
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import certifi
import httpx

from . import audit, config
from .access import get_policy

HISTORY_FILE = config.DATA_DIR / "site_checks.jsonl"
REPORTS_DIR = config.BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
USER_AGENT = "JARVIS-SiteMonitor/1.0 (+self-hosted website monitor)"


# ------------------------------------------------------------------ config --
def settings() -> dict[str, Any]:
    return get_policy().section("websites")


def sites() -> list[dict[str, Any]]:
    out = []
    for s in settings().get("sites") or []:
        if isinstance(s, dict) and s.get("url"):
            url = s["url"].strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            out.append({"name": s.get("name") or urlparse(url).netloc, "url": url,
                        "keywords": s.get("keywords") or [],
                        "seo": s.get("seo", True) is not False,
                        "login_check": s.get("login_check") if isinstance(s.get("login_check"), dict) else None})
    return out


def find_site(name_or_url: str) -> dict[str, Any] | None:
    key = name_or_url.strip().lower().rstrip("/")
    for s in sites():
        if key in (s["name"].lower(), s["url"].lower().rstrip("/"), urlparse(s["url"]).netloc.lower()):
            return s
    for s in sites():  # loose match: "portfolio" -> "My Portfolio"
        if key in s["name"].lower() or key in s["url"].lower():
            return s
    return None


def _thresholds() -> tuple[int, int]:
    r = settings().get("report") or {}
    return int(r.get("slow_ms", 2000)), int(r.get("ssl_warn_days", 14))


# ------------------------------------------------------------------ checks --
class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.meta_description = ""
        self.h1 = 0
        self.links: list[str] = []
        self.images_without_alt = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.meta_description = (a.get("content") or "").strip()
        elif tag == "h1":
            self.h1 += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img" and not (a.get("alt") or "").strip():
            self.images_without_alt += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _ssl_days_left(host: str, port: int = 443) -> tuple[int | None, str | None]:
    try:
        # certifi = same CA bundle httpx uses; Windows' Python store misses some intermediates/roots.
        ctx = ssl.create_default_context(cafile=certifi.where())
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        expires = ssl.cert_time_to_seconds(cert["notAfter"])
        return int((expires - time.time()) // 86400), None
    except ssl.SSLCertVerificationError as e:
        return None, f"SSL certificate invalid: {e.verify_message}"
    except Exception as e:  # noqa: BLE001
        return None, f"SSL check failed: {type(e).__name__}"


async def _broken_links(client: httpx.AsyncClient, base_url: str, links: list[str], limit: int = 25) -> list[dict]:
    host = urlparse(base_url).netloc
    targets: list[str] = []
    for href in links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if urlparse(url).netloc == host and url not in targets and url.rstrip("/") != base_url.rstrip("/"):
            targets.append(url)
    targets = targets[:limit]
    sem = asyncio.Semaphore(5)
    broken: list[dict] = []

    async def probe(url: str) -> None:
        async with sem:
            try:
                r = await client.head(url)
                if r.status_code in (405, 403, 501):  # some servers refuse HEAD
                    r = await client.get(url)
                if r.status_code >= 400:
                    broken.append({"url": url, "status": r.status_code})
            except httpx.HTTPError as e:
                broken.append({"url": url, "status": type(e).__name__})

    await asyncio.gather(*(probe(u) for u in targets))
    return broken


async def _login_check(client: httpx.AsyncClient, lc: dict[str, Any]) -> dict[str, Any]:
    """Log in through the site's API and confirm a token comes back. Only success and timing
    are kept — the response (tokens, user profile) is never stored or logged."""
    user, pw = config.env(lc.get("username_env", "")), config.env(lc.get("password_env", ""))
    if not user or not pw:
        return {"ok": False, "ms": 0,
                "error": f"credentials missing in .env ({lc.get('username_env')}, {lc.get('password_env')})"}
    body = {lc.get("username_field", "email"): user, lc.get("password_field", "password"): pw}
    try:
        t0 = time.perf_counter()
        r = await client.post(lc["url"], json=body)
        ms = int((time.perf_counter() - t0) * 1000)
    except httpx.HTTPError as e:
        return {"ok": False, "ms": 0, "error": f"login API unreachable ({type(e).__name__})"}
    if r.status_code >= 400:
        return {"ok": False, "ms": ms, "error": f"login API returned HTTP {r.status_code}"}
    try:
        node: Any = r.json()
        for part in str(lc.get("expect", "")).split("."):
            if part:
                node = node.get(part) if isinstance(node, dict) else None
        ok = bool(node)
    except ValueError:
        ok = False
    return {"ok": ok, "ms": ms, "error": None if ok else f"login response missing '{lc.get('expect')}'"}


async def check_site(site: dict[str, Any], full: bool = False) -> dict[str, Any]:
    """Run all checks for one site. `full` also crawls internal links for broken ones."""
    slow_ms, ssl_warn = _thresholds()
    result: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": site["name"], "url": site["url"], "full": full,
        "up": False, "status": None, "response_ms": None, "issues": [],
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as client:
        try:
            t0 = time.perf_counter()
            r = await client.get(site["url"])
            result["response_ms"] = int((time.perf_counter() - t0) * 1000)
            result["status"] = r.status_code
            result["final_url"] = str(r.url)
            result["redirects"] = len(r.history)
            result["page_kb"] = round(len(r.content) / 1024, 1)
            result["up"] = r.status_code < 400
        except httpx.HTTPError as e:
            result["error"] = f"{type(e).__name__}: {e}"[:200]
            result["issues"].append(f"DOWN — {type(e).__name__}")

        if result["status"] and result["status"] >= 400:
            result["issues"].append(f"DOWN — HTTP {result['status']}")
        if result["up"]:
            if result["response_ms"] > slow_ms:
                result["issues"].append(f"Slow: {result['response_ms']} ms (limit {slow_ms} ms)")
            text = r.text
            page = _PageParser()
            try:
                page.feed(text)
            except Exception:  # noqa: BLE001 — broken HTML shouldn't break the check
                pass
            result["title"] = " ".join(page.title.split())[:120]
            result["meta_description"] = page.meta_description[:200]
            result["h1_count"] = page.h1
            result["images_without_alt"] = page.images_without_alt
            result["links"] = len(page.links)
            missing = [k for k in site.get("keywords") or [] if k.lower() not in text.lower()]
            if missing:
                result["issues"].append(f"Missing expected text: {', '.join(missing)}")
            if site.get("seo", True):
                if not result["title"]:
                    result["issues"].append("SEO: page has no <title>")
                if not result["meta_description"]:
                    result["issues"].append("SEO: no meta description")
                if page.h1 != 1:
                    result["issues"].append(f"SEO: {page.h1} H1 headings (should be 1)")
            if full:
                result["broken_links"] = await _broken_links(client, str(r.url), page.links)
                if result["broken_links"]:
                    result["issues"].append(f"{len(result['broken_links'])} broken internal link(s)")

        lc = site.get("login_check")
        if lc and (full or lc.get("every_check")):
            result["login"] = await _login_check(client, lc)
            if not result["login"]["ok"]:
                result["issues"].append(f"LOGIN FAILED — {result['login']['error']}")
            elif result["login"]["ms"] > slow_ms:
                result["issues"].append(f"Slow login: {result['login']['ms']} ms")

    parsed = urlparse(site["url"])
    if parsed.scheme == "https":
        days, err = await asyncio.to_thread(_ssl_days_left, parsed.hostname or "", parsed.port or 443)
        result["ssl_days_left"] = days
        if err:
            result["issues"].append(err)
        elif days is not None and days <= ssl_warn:
            result["issues"].append(f"SSL certificate expires in {days} day(s)")
    else:
        result["issues"].append("Not using HTTPS")

    _append_history(result)
    return result


async def check_all(full: bool = False) -> list[dict[str, Any]]:
    return list(await asyncio.gather(*(check_site(s, full) for s in sites())))


# ----------------------------------------------------------------- history --
def _append_history(result: dict[str, Any]) -> None:
    slim = {k: result.get(k) for k in ("ts", "name", "url", "up", "status", "response_ms",
                                       "ssl_days_left", "issues", "full", "login")}
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")


def history(hours: int = 24, name: str | None = None) -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    with HISTORY_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if datetime.fromisoformat(row["ts"]) >= since and (name is None or row["name"] == name):
                out.append(row)
    return out


def latest_status() -> list[dict[str, Any]]:
    last: dict[str, dict] = {}
    for row in history(hours=72):
        last[row["name"]] = row
    return [last.get(s["name"], {"name": s["name"], "url": s["url"], "up": None}) for s in sites()]


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"checks": 0}
    up = [r for r in rows if r.get("up")]
    times = [r["response_ms"] for r in up if r.get("response_ms") is not None]
    return {
        "checks": len(rows),
        "uptime_pct": round(100 * len(up) / len(rows), 2),
        "avg_ms": int(sum(times) / len(times)) if times else None,
        "max_ms": max(times) if times else None,
        "down_events": [r["ts"] for r in rows if not r.get("up")],
    }


# ----------------------------------------------------------------- reports --
def summarise(result: dict[str, Any]) -> str:
    """One short line per site — good for voice."""
    if not result.get("up"):
        return f"{result['name']}: DOWN ({result.get('error') or 'HTTP ' + str(result.get('status'))})"
    parts = [f"{result['name']}: up, {result['response_ms']} ms"]
    if result.get("login"):
        parts.append(f"login OK ({result['login']['ms']} ms)" if result["login"]["ok"] else "LOGIN FAILED")
    if result.get("ssl_days_left") is not None:
        parts.append(f"SSL {result['ssl_days_left']} days left")
    parts.append("no issues" if not result["issues"] else "issues: " + "; ".join(result["issues"]))
    return ", ".join(parts)


async def build_daily_report(day: date | None = None) -> dict[str, Any]:
    """Run a full check of every site, combine with the last 24 h of history, save the report."""
    day = day or date.today()
    current = await check_all(full=True)
    owner = get_policy().owner_name
    rows_by_site: dict[str, list] = defaultdict(list)
    for row in history(hours=24):
        rows_by_site[row["name"]].append(row)

    sections_md, sections_html = [], []
    total_issues = 0
    for res in current:
        st = stats(rows_by_site[res["name"]])
        total_issues += len(res["issues"])
        badge = "🟢 UP" if res["up"] else "🔴 DOWN"
        md = [f"## {res['name']} — {badge}", f"{res['url']}", ""]
        md.append(f"- Uptime (24 h): **{st.get('uptime_pct', 'n/a')}%** over {st['checks']} checks")
        if st.get("avg_ms") is not None:
            md.append(f"- Response time: avg {st['avg_ms']} ms, max {st['max_ms']} ms, now {res.get('response_ms')} ms")
        if st.get("down_events"):
            md.append(f"- Down at: {', '.join(t[11:16] + ' UTC' for t in st['down_events'][:10])}")
        if res.get("ssl_days_left") is not None:
            md.append(f"- SSL certificate: {res['ssl_days_left']} days left")
        if res.get("login"):
            md.append(f"- Admin login: OK in {res['login']['ms']} ms" if res["login"]["ok"]
                      else f"- Admin login: **FAILED** — {res['login']['error']}")
        if res.get("title"):
            md.append(f"- Title: {res['title']}")
        md.append(f"- Page size: {res.get('page_kb', 'n/a')} KB, links: {res.get('links', 'n/a')}, "
                  f"images without alt: {res.get('images_without_alt', 'n/a')}")
        if res["issues"]:
            md += ["", "**Issues:**"] + [f"- ⚠️ {i}" for i in res["issues"]]
        for b in (res.get("broken_links") or [])[:15]:
            md.append(f"  - broken: {b['url']} ({b['status']})")
        sections_md.append("\n".join(md))
        sections_html.append(_section_html(res, st))

    headline = (f"All {len(current)} site(s) healthy." if total_issues == 0
                else f"{total_issues} issue(s) across {len(current)} site(s).")
    title = f"Website report — {day.strftime('%A %d %B %Y')}"
    md_doc = f"# {title}\n\nFor {owner}. {headline}\n\n" + "\n\n".join(sections_md) + "\n"
    html_doc = _page_html(title, owner, headline, sections_html)

    (REPORTS_DIR / f"{day.isoformat()}.md").write_text(md_doc, encoding="utf-8")
    (REPORTS_DIR / f"{day.isoformat()}.html").write_text(html_doc, encoding="utf-8")
    audit.record({"capability": "websites.report", "summary": f"Daily website report: {headline}",
                  "status": "done", "mode": "worker"})
    return {"date": day.isoformat(), "headline": headline, "markdown": md_doc, "html": html_doc,
            "summary": [summarise(r) for r in current]}


def read_report(day: str | None = None) -> str | None:
    if day:
        path = REPORTS_DIR / f"{day}.md"
    else:
        files = sorted(REPORTS_DIR.glob("*.md"))
        path = files[-1] if files else None
    return path.read_text(encoding="utf-8") if path and path.exists() else None


def list_reports() -> list[str]:
    return sorted((p.stem for p in REPORTS_DIR.glob("*.html")), reverse=True)


def email_report(report: dict[str, Any]) -> str:
    """Email the report to the owner's own address (needs email credentials)."""
    from .tools.email_tools import creds, smtp_send

    c = creds()
    msg = EmailMessage()
    msg["From"] = c["EMAIL_ADDRESS"]
    msg["To"] = c["EMAIL_ADDRESS"]
    msg["Subject"] = f"🌐 Website report {report['date']} — {report['headline']}"
    msg.set_content(report["markdown"])
    msg.add_alternative(report["html"], subtype="html")
    smtp_send(msg)
    return c["EMAIL_ADDRESS"]


def _section_html(res: dict[str, Any], st: dict[str, Any]) -> str:
    e = html.escape
    color = "#16a34a" if res["up"] else "#dc2626"
    rows = [
        ("Uptime (24 h)", f"{st.get('uptime_pct', 'n/a')}% ({st['checks']} checks)"),
        ("Response", f"now {res.get('response_ms')} ms · avg {st.get('avg_ms')} ms · max {st.get('max_ms')} ms"),
        ("SSL", f"{res.get('ssl_days_left')} days left" if res.get("ssl_days_left") is not None else "n/a"),
        *([("Admin login", f"OK in {res['login']['ms']} ms" if res["login"]["ok"] else f"FAILED — {res['login']['error']}")]
          if res.get("login") else []),
        ("Title", res.get("title") or "—"),
        ("Page", f"{res.get('page_kb', 'n/a')} KB · {res.get('links', 'n/a')} links · "
                 f"{res.get('images_without_alt', 'n/a')} images without alt"),
    ]
    issues = "".join(f"<li>{e(i)}</li>" for i in res["issues"]) or "<li style='color:#16a34a'>No issues</li>"
    broken = "".join(f"<li><code>{e(b['url'])}</code> ({e(str(b['status']))})</li>"
                     for b in (res.get("broken_links") or [])[:15])
    table = "".join(f"<tr><td style='color:#64748b;padding:4px 12px 4px 0'>{e(k)}</td><td>{e(str(v))}</td></tr>"
                    for k, v in rows)
    return (f"<div style='border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin:14px 0'>"
            f"<h2 style='margin:0 0 4px;font-size:18px'>{e(res['name'])} "
            f"<span style='color:{color};font-size:13px'>● {'UP' if res['up'] else 'DOWN'}</span></h2>"
            f"<a href='{e(res['url'])}' style='color:#0284c7;font-size:13px'>{e(res['url'])}</a>"
            f"<table style='margin:10px 0;font-size:14px'>{table}</table>"
            f"<b style='font-size:14px'>Issues</b><ul style='font-size:14px'>{issues}</ul>"
            + (f"<b style='font-size:14px'>Broken links</b><ul style='font-size:13px'>{broken}</ul>" if broken else "")
            + "</div>")


def _page_html(title: str, owner: str, headline: str, sections: list[str]) -> str:
    e = html.escape
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{e(title)}</title></head>"
            f"<body style='font-family:Segoe UI,Arial,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#0f172a'>"
            f"<h1 style='font-size:22px;margin-bottom:4px'>{e(title)}</h1>"
            f"<p style='color:#475569;margin-top:0'>For {e(owner)} · {e(headline)}</p>"
            + "".join(sections)
            + "<p style='color:#94a3b8;font-size:12px'>Generated by JARVIS PRO website monitor.</p></body></html>")
