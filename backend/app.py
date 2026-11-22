"""AE web view backend.

The OpenComputers program pushes two kinds of data:
  * POST /api/oc/status  small JSON: AE power and crafting CPUs, every few seconds
  * POST /api/oc/items   inventory snapshot as tab-separated text, sent in
                         numbered chunks and swapped in once the last chunk arrives
The browser reads it back through /api/state and /api/items after logging in.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AEWEB_CONFIG", BASE_DIR / "config.toml"))
MAX_BODY = 4 * 1024 * 1024
PENDING_TTL = 600
COOKIE = "aeweb_session"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Config not found: {CONFIG_PATH} (copy config.example.toml to config.toml)")
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    for key in ("oc_token", "web_password"):
        value = str(cfg.get(key, ""))
        if not value or value.startswith("CHANGE-ME"):
            sys.exit(f"Set '{key}' in {CONFIG_PATH} before starting")
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8765)
    data_dir = Path(cfg.get("data_dir", "data"))
    cfg["data_dir"] = data_dir if data_dir.is_absolute() else BASE_DIR / data_dir
    return cfg


CFG = load_config()
SESSION_VALUE = hmac.new(
    CFG["web_password"].encode(), b"aeweb-session-v1", hashlib.sha256
).hexdigest()
STATE_FILE = CFG["data_dir"] / "state.json"


class State:
    def __init__(self) -> None:
        self.status: dict | None = None
        self.items: list[dict] = []
        self.items_meta: dict | None = None
        self.pending: dict | None = None

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Ignoring unreadable {STATE_FILE}: {exc}")
            return
        self.status = data.get("status")
        self.items = data.get("items", [])
        self.items_meta = data.get("items_meta")
        for row in self.items:
            row["_key"] = (row["n"] + "\0" + row["l"]).lower()

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        items = [{k: v for k, v in row.items() if k != "_key"} for row in self.items]
        payload = {"status": self.status, "items": items, "items_meta": self.items_meta}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)


STATE = State()
STATE.load()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- auth

def require_oc(request: Request) -> None:
    header = request.headers.get("authorization", "")
    expected = "Bearer " + CFG["oc_token"]
    if not hmac.compare_digest(header.encode(), expected.encode()):
        raise HTTPException(401, "bad token")


def is_logged_in(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), SESSION_VALUE)


def require_web(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(401, "login required")


async def read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY:
        raise HTTPException(413, "body too large")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "body too large")
    return body


# ---------------------------------------------------------------- OC endpoints

@app.post("/api/oc/status")
async def oc_status(request: Request):
    require_oc(request)
    try:
        data = json.loads(await read_body(request))
    except ValueError:
        raise HTTPException(400, "invalid json")
    if not isinstance(data, dict):
        raise HTTPException(400, "expected object")
    data["received"] = time.time()
    STATE.status = data
    return {"ok": True}


def parse_rows(text: str) -> list[dict]:
    rows = []
    for line in text.split("\n"):
        if not line:
            continue
        parts = line.split("\t", 5)
        if len(parts) != 6 or parts[0] not in ("i", "f"):
            raise HTTPException(400, f"bad line: {line[:80]!r}")
        kind, name, damage, size, craftable, label = parts
        try:
            row = {
                "k": kind,
                "n": name,
                "d": int(float(damage)),
                "s": int(float(size)),
                "c": craftable == "1",
                "l": label or name,
            }
        except ValueError:
            raise HTTPException(400, f"bad number: {line[:80]!r}")
        row["_key"] = (row["n"] + "\0" + row["l"]).lower()
        rows.append(row)
    return rows


@app.post("/api/oc/items")
async def oc_items(request: Request, snap: str, seq: int, final: int = 0,
                   count: int = -1, scan: float = 0.0):
    require_oc(request)
    body = await read_body(request)
    digest = hashlib.sha1(body).hexdigest()
    now = time.time()
    pending = STATE.pending
    if pending and now - pending["started"] > PENDING_TTL:
        pending = STATE.pending = None

    if seq == 0:
        pending = STATE.pending = {"snap": snap, "rows": [], "next": 0,
                                   "started": now, "last": None}
    if not pending or pending["snap"] != snap:
        return JSONResponse({"ok": False, "error": "unknown_snapshot"}, status_code=409)
    if seq == pending["next"] - 1 and pending["last"] == digest:
        return {"ok": True, "duplicate": True}
    if seq != pending["next"]:
        return JSONResponse({"ok": False, "error": "out_of_order",
                             "expected": pending["next"]}, status_code=409)

    pending["rows"].extend(parse_rows(body.decode("utf-8", errors="replace")))
    pending["next"] += 1
    pending["last"] = digest

    if final:
        rows = pending["rows"]
        STATE.pending = None
        if count >= 0 and count != len(rows):
            return JSONResponse({"ok": False, "error": "count_mismatch",
                                 "got": len(rows)}, status_code=409)
        STATE.items = rows
        STATE.items_meta = {
            "updated": now,
            "items": sum(1 for r in rows if r["k"] == "i"),
            "fluids": sum(1 for r in rows if r["k"] == "f"),
            "scan_seconds": scan,
        }
        await asyncio.to_thread(STATE.save)
    return {"ok": True}


# ---------------------------------------------------------------- web endpoints

LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AE 网络 · 登录</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--muted:#6b7280;--accent:#2f6fde;--err:#c2412d;--line:#d9dde4}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e6e8eb;--muted:#9aa1ad;--accent:#6b9cf0;--err:#ef7d68;--line:#30353f}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Microsoft YaHei",sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:28px;width:min(320px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:18px;margin:0 0 16px}input,button{width:100%;box-sizing:border-box;font:inherit;padding:9px 11px;border-radius:7px}
input{border:1px solid var(--line);background:transparent;color:inherit;margin-bottom:12px}
button{border:0;background:var(--accent);color:#fff;cursor:pointer}.err{color:var(--err);font-size:13px;margin:-4px 0 10px}
</style></head><body><form method="post" action="login"><h1>AE 网络</h1>{error}
<input type="password" name="password" placeholder="密码" autofocus required><button>登录</button></form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_PAGE.replace("{error}", "")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password.encode(), CFG["web_password"].encode()):
        await asyncio.sleep(1.0)
        return HTMLResponse(LOGIN_PAGE.replace("{error}", '<p class="err">密码错误</p>'), 401)
    response = RedirectResponse("./", status_code=303)
    secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    response.set_cookie(COOKIE, SESSION_VALUE, max_age=30 * 86400, httponly=True,
                        samesite="lax", secure=secure)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.get("/")
async def index(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("login", status_code=303)
    return FileResponse(BASE_DIR / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def api_state(request: Request):
    require_web(request)
    return {"now": time.time(), "status": STATE.status, "items_meta": STATE.items_meta}


@app.get("/api/items")
async def api_items(request: Request, q: str = "", kind: str = "all", sort: str = "size",
                    order: str = "desc", offset: int = 0, limit: int = 100):
    require_web(request)
    rows = STATE.items
    if kind in ("i", "f"):
        rows = [r for r in rows if r["k"] == kind]
    if kind == "c":
        rows = [r for r in rows if r["c"]]
    terms = q.lower().split()
    if terms:
        rows = [r for r in rows if all(t in r["_key"] for t in terms)]
    if sort == "name":
        rows = sorted(rows, key=lambda r: r["l"].lower(), reverse=(order == "desc"))
    else:
        rows = sorted(rows, key=lambda r: r["s"], reverse=(order != "asc"))
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    page = [{k: v for k, v in r.items() if k != "_key"} for r in rows[offset:offset + limit]]
    return {"total": len(rows), "rows": page}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/oc/aeweb.lua")
async def oc_script():
    # Lets the OC computer fetch the uploader with `wget`; it holds no secrets.
    return FileResponse(BASE_DIR.parent / "oc" / "aeweb.lua", media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    uvicorn.run(app, host=CFG["host"], port=int(CFG["port"]), proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
