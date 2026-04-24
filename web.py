"""
web.py - Skland Sign-In Web Management UI

Environment variables:
  WEB_PASSWORD         Admin password (full access to config / logs)
  WEB_VIEWER_PASSWORD  Viewer password (dashboard only, no config / logs)
  WEB_PORT             Port to listen on (default: 8080)
  WEB_SECRET           HMAC secret for cookie signing (auto-generated if not set)
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("config.yaml")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
WEB_VIEWER_PASSWORD = os.environ.get("WEB_VIEWER_PASSWORD", "")
_SECRET = os.environ.get("WEB_SECRET", secrets.token_hex(32))
_COOKIE_ADMIN = "skland_auth"
_COOKIE_VIEWER = "skland_viewer"

# ── Logging setup ─────────────────────────────────────────────────────────────
# Set up root logger before anything else so that main.py's basicConfig() is
# a no-op (it skips setup when handlers already exist).

_log_buf: deque = deque(maxlen=500)


class _BufHandler(logging.Handler):
    """Captures log records into an in-memory ring buffer for the web UI."""

    def emit(self, record: logging.LogRecord) -> None:
        _log_buf.append(
            {
                "t": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "lvl": record.levelname,
                "msg": record.getMessage(),
            }
        )


_root = logging.getLogger()
_root.setLevel(logging.INFO)
# Console handler (so cron-triggered runs also appear in docker logs)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
_root.addHandler(_sh)
# Web buffer handler
_root.addHandler(_BufHandler())

# ── Sign-in state ─────────────────────────────────────────────────────────────

_st: dict = {"running": False, "last_time": "-", "last_result": "-", "accounts": []}

# ── FastAPI app ───────────────────────────────────────────────────────────────

VERSION = "2.1.0"
REPO = "https://github.com/quicksilver2000/Skland-Sign-In"

app = FastAPI(docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")
templates.env.globals["has_password"] = bool(WEB_PASSWORD)
templates.env.globals["is_admin"] = False
templates.env.globals["version"] = VERSION
templates.env.globals["repo_url"] = REPO

# Disable Jinja2 template cache to avoid unhashable weakref.ref in LRU cache key on Python 3.12
templates.env.cache = None

# ── Auth helpers ──────────────────────────────────────────────────────────────


def _make_token(pw: str) -> str:
    return hmac.new(_SECRET.encode(), pw.encode(), hashlib.sha256).hexdigest()


def _auth_role(req: Request) -> str | None:
    if not WEB_PASSWORD:
        return "admin"
    c = req.cookies.get(_COOKIE_ADMIN, "")
    if c and hmac.compare_digest(c, _make_token(WEB_PASSWORD)):
        return "admin"
    if WEB_VIEWER_PASSWORD:
        c = req.cookies.get(_COOKIE_VIEWER, "")
        if c and hmac.compare_digest(c, _make_token(WEB_VIEWER_PASSWORD)):
            return "viewer"
    return None


# ── Config/crontab helpers ────────────────────────────────────────────────────


def _load_cfg() -> dict:
    try:
        return yaml.safe_load(CONFIG_PATH.read_text("utf-8")) or {}
    except Exception:
        return {}


def _write_crontab(expr: str) -> None:
    """Update the Alpine crontab so the new schedule takes effect immediately."""
    p = Path("/etc/crontabs/root")
    if p.exists():
        p.write_text(f"{expr} curl -sf -X POST http://localhost:{os.environ.get('WEB_PORT', '8080')}/api/internal/run > /dev/null 2>&1\n")


# ── Sign-in runner ────────────────────────────────────────────────────────────


async def _do_sign_in() -> None:
    _st["running"] = True
    logger = logging.getLogger("web")
    logger.info("签到任务开始执行")
    try:
        from main import run_sign_in  # noqa: PLC0415

        accounts = await run_sign_in()
        _st["last_result"] = "success"
        _st["accounts"] = accounts or []
        logger.info("签到任务完成")
    except Exception as exc:
        _st["last_result"] = f"error: {exc}"
        _st["accounts"] = []
        logger.error(f"手动触发签到失败: {exc}")
    finally:
        _st["running"] = False
        _st["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_delayed_first_run())


async def _delayed_first_run():
    await asyncio.sleep(2)
    _st["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await _do_sign_in()


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    role = _auth_role(request)
    if role is None:
        return RedirectResponse("/login", 302)
    cfg = _load_cfg()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "st": _st,
            "cron": cfg.get("cron", "0 1 * * *"),
            "user_count": len(cfg.get("users", [])),
            "is_admin": role == "admin",
        },
    )


@app.get("/config", response_class=HTMLResponse)
async def config_get(request: Request, saved: str = ""):
    if _auth_role(request) != "admin":
        return RedirectResponse("/login", 302)
    text = CONFIG_PATH.read_text("utf-8") if CONFIG_PATH.exists() else ""
    return templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "config_text": text,
            "saved": saved == "1",
            "error": None,
            "is_admin": True,
        },
    )


@app.post("/config", response_class=HTMLResponse)
async def config_post(request: Request, config_text: str = Form(...)):
    if _auth_role(request) != "admin":
        return RedirectResponse("/login", 302)
    try:
        parsed = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        return templates.TemplateResponse(
            "config.html",
            {
                "request": request,
                "config_text": config_text,
                "saved": False,
                "error": str(exc),
                "is_admin": True,
            },
        )
    CONFIG_PATH.write_text(config_text, "utf-8")
    if isinstance(parsed, dict):
        _write_crontab(parsed.get("cron", "0 1 * * *"))
    logging.getLogger("web").info("配置已通过 Web UI 更新")
    return RedirectResponse("/config?saved=1", 303)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if _auth_role(request) != "admin":
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse("logs.html", {"request": request, "is_admin": True})


@app.post("/api/run")
async def api_run(request: Request):
    if _auth_role(request) != "admin":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _trigger_run()


@app.post("/api/internal/run")
async def api_internal_run(request: Request):
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await _trigger_run()


async def _trigger_run():
    if _st["running"]:
        return JSONResponse({"status": "already_running"})
    asyncio.create_task(_do_sign_in())
    return JSONResponse({"status": "started"})


@app.get("/api/status")
async def api_status(request: Request):
    role = _auth_role(request)
    if role is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cfg = _load_cfg()
    data = {**_st, "user_count": len(cfg.get("users", [])), "is_admin": role == "admin"}
    return JSONResponse(data)


@app.get("/api/logs")
async def api_logs(request: Request):
    if _auth_role(request) != "admin":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(list(_log_buf))


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if not WEB_PASSWORD:
        return RedirectResponse("/", 302)
    if _auth_role(request):
        return RedirectResponse("/", 302)
    return templates.TemplateResponse("login.html", {"request": request, "error": False})


@app.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    if password == WEB_PASSWORD:
        resp = RedirectResponse("/", 303)
        resp.set_cookie(_COOKIE_ADMIN, _make_token(password), httponly=True, samesite="lax")
        return resp
    if WEB_VIEWER_PASSWORD and password == WEB_VIEWER_PASSWORD:
        resp = RedirectResponse("/", 303)
        resp.set_cookie(_COOKIE_VIEWER, _make_token(password), httponly=True, samesite="lax")
        return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": True})


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 302)
    resp.delete_cookie(_COOKIE_ADMIN)
    resp.delete_cookie(_COOKIE_VIEWER)
    return resp


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("WEB_PORT", "8080")))
