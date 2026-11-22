"""AE web view backend.

The OpenComputers program pushes two kinds of data:
  * POST /api/oc/status  small JSON: AE power and crafting CPUs, every few seconds
  * POST /api/oc/items   inventory snapshot as tab-separated text, sent in
                         numbered chunks and swapped in once the last chunk arrives
The browser reads it back through /api/state and /api/items after logging in.

Item names arrive in the server's language (English). Chinese names come from
NEI item panel dumps placed in <data_dir>/i18n (see Translations).
"""

import asyncio
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import tomllib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from pydantic import BaseModel, Field

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
I18N_DIR = CFG["data_dir"] / "i18n"


class Translations:
    """Chinese item names from NEI item panel dumps (CSV mode).

    itempanel_zh.csv (dumped with the client in Chinese) maps "registry:meta" to
    a Chinese name. The optional itempanel_en.csv, dumped with the client in
    English, pairs up with it row by row to give an English-name -> Chinese-name
    table; that is how fluids (shown in NEI as GT fluid display items) and items
    whose meta is shared by several NBT variants get translated.

    Items NEI hides or lists only once get two fallbacks: an item registered
    under a single meta (tools, whose meta is damage) uses that one name, and
    GT ore-processing products (hidden in GTNH's NEI) are named from their
    material using the patterns GT++/BartWorks items show in the same dumps.
    """

    ZH_FILE = "itempanel_zh.csv"
    EN_FILE = "itempanel_en.csv"
    # (English pattern, Chinese template, use the bare material name)
    DERIVED = [
        (re.compile(r"Tiny Pile of (.+)"), "小撮{}", False),
        (re.compile(r"Small Pile of (.+)"), "小堆{}", False),
        (re.compile(r"Impure Pile of (.+)"), "含杂{}", False),
        (re.compile(r"Purified Pile of (.+)"), "纯净{}", False),
        (re.compile(r"Purified Crushed (.+) Ore"), "洗净的{}矿石", True),
        (re.compile(r"Centrifuged Crushed (.+) Ore"), "离心{}矿石", True),
        (re.compile(r"Crushed (.+) Ore"), "粉碎的{}矿石", True),
    ]

    def __init__(self) -> None:
        self.by_key: dict[str, str] = {}     # unambiguous registry:meta -> zh
        self.any_key: dict[str, str] = {}    # first zh seen for registry:meta
        self.single: dict[str, str] = {}     # registry name with one meta -> zh
        self.by_en: dict[str, str] = {}      # unambiguous English name -> zh
        self.stamp: tuple | None = None
        self.version = 0
        self.info = {"zh": 0, "en": 0, "pairs": 0, "error": None}

    @staticmethod
    def _read(path: Path) -> dict[str, list[str]]:
        """Return registry:meta -> display names in dump order (None if absent)."""
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        groups: dict[str, list[str]] = {}
        reader = csv.DictReader(text.splitlines())
        missing = {"Item Name", "Item meta", "Display Name"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
        for row in reader:
            name = (row["Item Name"] or "").strip()
            display = (row["Display Name"] or "").strip()
            if name and display:
                groups.setdefault(f"{name}:{row['Item meta'].strip()}", []).append(display)
        return groups

    def refresh(self) -> None:
        zh_path, en_path = I18N_DIR / self.ZH_FILE, I18N_DIR / self.EN_FILE
        stamp = tuple(p.stat().st_mtime if p.exists() else None for p in (zh_path, en_path))
        if stamp == self.stamp:
            return
        self.stamp = stamp
        by_key, any_key, by_en = {}, {}, {}
        info = {"zh": 0, "en": 0, "pairs": 0, "error": None}
        try:
            zh = self._read(zh_path) if zh_path.exists() else {}
            en = self._read(en_path) if en_path.exists() else {}
        except (OSError, ValueError) as exc:
            info["error"] = str(exc)
            zh, en = {}, {}
        metas_per_name: dict[str, list[str]] = {}
        for key, names in zh.items():
            info["zh"] += len(names)
            any_key[key] = names[0]
            if len(set(names)) == 1:
                by_key[key] = names[0]
            metas_per_name.setdefault(key.rsplit(":", 1)[0], []).append(key)
        single = {name: by_key[keys[0]] for name, keys in metas_per_name.items()
                  if len(keys) == 1 and keys[0] in by_key}
        ambiguous = set()
        for key, en_names in en.items():
            info["en"] += len(en_names)
            zh_names = zh.get(key)
            if not zh_names or len(zh_names) != len(en_names):
                continue
            for en_name, zh_name in zip(en_names, zh_names):
                if en_name == zh_name or en_name in ambiguous:
                    continue
                if by_en.setdefault(en_name, zh_name) != zh_name:
                    ambiguous.add(en_name)
        for en_name in ambiguous:
            del by_en[en_name]
        info["pairs"] = len(by_en)
        self.by_key, self.any_key, self.single, self.by_en = by_key, any_key, single, by_en
        self.info = info
        self.version += 1
        print(f"Translations loaded: {info}")

    def lookup(self, kind: str, name: str, damage: int, label: str) -> str | None:
        key = f"{name}:{damage}"
        if kind == "i" and key in self.by_key:
            return self.by_key[key]
        if label in self.by_en:
            return self.by_en[label]
        if kind != "i":
            return None
        if key in self.any_key:
            return self.any_key[key]
        if name in self.single:
            return self.single[name]
        return self.derive(label)

    def derive(self, label: str) -> str | None:
        for pattern, template, bare in self.DERIVED:
            match = pattern.fullmatch(label)
            if not match:
                continue
            base = match.group(1)
            if bare:  # "Gold" -> "金" via "Gold Dust" -> "金粉"
                dust = self.by_en.get(base + " Dust")
                zh = dust[:-1] if dust and dust.endswith("粉") else self.by_en.get(base)
            else:
                zh = self.by_en.get(base)
            return template.format(zh) if zh else None
        return None


TR = Translations()
ICON_DIR = CFG["data_dir"] / "icons"


class Icons:
    """Item icons imported from an NEI PNG dump by import_icons.py.

    The index lists every dumped item panel entry (registry name, meta, Chinese
    display name, icon id). Items are found by registry:meta, NBT variants by
    their Chinese name, fluids through NEI's fluid display items by name.
    GT dust piles hidden from NEI borrow the matching dust's icon.
    """

    FLUID_DISPLAYS = ("gregtech:gt.GregTech_FluidDisplay", "NotEnoughItems:neiFluidDisplay")
    PILE = re.compile(r"(?:Tiny|Small|Impure|Purified) Pile of (.+)")

    def __init__(self) -> None:
        self.version: str | None = None
        self.by_key: dict[str, list[tuple[str, int]]] = {}
        self.by_zh: dict[str, int] = {}
        self.fluid_by_zh: dict[str, int] = {}
        self.by_name_zh: dict[tuple[str, str], int] = {}  # same item, any meta (damage/charge)
        self.single: dict[str, int] = {}
        self.stamp = None

    def refresh(self) -> None:
        current = ICON_DIR / "current.txt"
        stamp = current.stat().st_mtime if current.exists() else None
        if stamp == self.stamp:
            return
        self.stamp = stamp
        by_key, by_zh, fluid_by_zh, keys_per_name, by_name_zh = {}, {}, {}, {}, {}
        version = None
        try:
            version = current.read_text(encoding="utf-8").strip()
            rows = json.loads((ICON_DIR / version / "index.json").read_text(encoding="utf-8"))["rows"]
        except (OSError, ValueError, KeyError) as exc:
            if stamp is not None:
                print(f"Icons not loaded: {exc}")
            rows, version = [], None
        ambiguous = set()
        for name, meta, display, icon in rows:
            if icon is None:
                continue
            key = f"{name}:{meta}"
            by_key.setdefault(key, []).append((display, icon))
            keys_per_name.setdefault(name, set()).add(key)
            by_name_zh.setdefault((name, display), icon)
            if name in self.FLUID_DISPLAYS:
                fluid_by_zh.setdefault(display, icon)
            if by_zh.setdefault(display, icon) != icon:
                ambiguous.add(display)
        for display in ambiguous:
            del by_zh[display]
        self.single = {name: by_key[next(iter(keys))][0][1]
                       for name, keys in keys_per_name.items() if len(keys) == 1}
        self.by_key, self.by_zh, self.fluid_by_zh, self.version = by_key, by_zh, fluid_by_zh, version
        self.by_name_zh = by_name_zh
        print(f"Icons loaded: version {version}, {len(rows)} entries")

    def lookup(self, kind: str, name: str, damage: int, label: str, zh: str | None) -> str | None:
        icon = self._find(kind, name, damage, label, zh) if self.version else None
        return f"{self.version}/{icon}" if icon is not None else None

    def _find(self, kind, name, damage, label, zh):
        if kind == "f":
            return self.fluid_by_zh.get(zh) if zh else None
        candidates = self.by_key.get(f"{name}:{damage}")
        if candidates:
            if len(candidates) > 1 and zh:
                for display, icon in candidates:
                    if display == zh:
                        return icon
            return candidates[0][1]
        if zh and (name, zh) in self.by_name_zh:
            return self.by_name_zh[(name, zh)]
        if zh and zh in self.by_zh:
            return self.by_zh[zh]
        if name in self.single:
            return self.single[name]
        pile = self.PILE.fullmatch(label)
        if pile:
            dust = TR.by_en.get(pile.group(1))
            return self.by_name_zh.get((name, dust)) or self.by_zh.get(dust) if dust else None
        return None


ICONS = Icons()


def index_rows(rows: list[dict]) -> None:
    """Attach the Chinese name, icon and lower-cased search text to each row."""
    for row in rows:
        zh = TR.lookup(row["k"], row["n"], row["d"], row["l"])
        row["_zh"] = zh
        row["_ic"] = ICONS.lookup(row["k"], row["n"], row["d"], row["l"], zh)
        row["_key"] = "\0".join((row["n"], row["l"], zh or "")).lower()


class State:
    def __init__(self) -> None:
        self.status: dict | None = None
        self.items: list[dict] = []
        self.items_meta: dict | None = None
        self.pending: dict | None = None
        self.indexed_version = None

    def ensure_indexed(self) -> None:
        TR.refresh()
        ICONS.refresh()
        version = (TR.version, ICONS.version)
        if self.indexed_version != version:
            index_rows(self.items)
            self.indexed_version = version

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

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        items = [{k: v for k, v in row.items() if not k.startswith("_")} for row in self.items]
        payload = {"status": self.status, "items": items, "items_meta": self.items_meta}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)


STATE = State()
STATE.load()


class Commands:
    """Orders queued from the web page and carried out by the OC program.

    A command is handed to OC in the response to its status upload ("sent"),
    re-sent if OC has not reported on it within RESEND_AFTER seconds, and
    dropped ("expired") if OC never picked it up within EXPIRE_AFTER seconds so
    an old order cannot fire long after it was placed. OC records each id
    before acting on it, so a re-sent command never runs twice.
    """

    FILE = CFG["data_dir"] / "commands.json"
    RESEND_AFTER = 30
    EXPIRE_AFTER = 300
    LOST_AFTER = 60
    KEEP = 200
    TERMINAL = {"done", "failed", "canceled", "expired", "lost", "error"}

    def __init__(self) -> None:
        self.items: list[dict] = []
        if self.FILE.exists():
            try:
                self.items = json.loads(self.FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"Ignoring unreadable {self.FILE}: {exc}")

    def save(self) -> None:
        self.items = self.items[-self.KEEP:]
        self.FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.FILE)

    def get(self, cmd_id: str) -> dict | None:
        return next((c for c in self.items if c["id"] == cmd_id), None)

    def add(self, **fields) -> dict:
        now = time.time()
        cmd = {"id": secrets.token_hex(8), "state": "queued", "msg": "", "created": now,
               "updated": now, "sent": None, **fields}
        self.items.append(cmd)
        self.save()
        return cmd

    def set_state(self, cmd: dict, state: str, msg: str = "") -> None:
        cmd["state"], cmd["msg"], cmd["updated"] = state, msg, time.time()

    def apply_report(self, updates: list, tracking: list) -> None:
        """Apply state updates from OC and notice jobs OC is no longer tracking."""
        now, changed = time.time(), False
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            cmd = self.get(str(update.get("id", "")))
            state = str(update.get("state", ""))
            if cmd and cmd["state"] not in self.TERMINAL and state:
                self.set_state(cmd, state, str(update.get("msg", ""))[:200])
                changed = True
        tracked = {str(t) for t in tracking} if isinstance(tracking, list) else set()
        for cmd in self.items:
            if (cmd["state"] in ("computing", "crafting") and cmd["id"] not in tracked
                    and now - cmd["updated"] > self.LOST_AFTER):
                self.set_state(cmd, "lost", "OC stopped tracking this job")
                changed = True
        if changed:
            self.save()

    def to_deliver(self) -> list[dict]:
        now, out, changed = time.time(), [], False
        for cmd in self.items:
            if cmd["state"] == "queued" and now - cmd["created"] > self.EXPIRE_AFTER:
                self.set_state(cmd, "expired")
                changed = True
            elif cmd["state"] == "queued" or (
                    cmd["state"] == "sent" and now - cmd["sent"] > self.RESEND_AFTER):
                if cmd["state"] == "queued":
                    self.set_state(cmd, "sent")
                cmd["sent"] = now
                out.append(cmd)
                changed = True
        if changed:
            self.save()
        return out

    @staticmethod
    def wire(cmd: dict) -> str:
        """One tab-separated line for OC; the label goes last as it may hold anything."""
        clean = lambda s: re.sub(r"[\t\r\n]", " ", str(s))
        if cmd["type"] == "craft" and cmd.get("k") == "f":  # amount in L (mB)
            fields = ["craft_fluid", cmd["id"], cmd["amount"], cmd["n"], cmd["cpu"], cmd["l"]]
        elif cmd["type"] == "craft":
            fields = ["craft", cmd["id"], cmd["amount"], cmd["d"], cmd["n"], cmd["cpu"], cmd["l"]]
        else:
            fields = ["cancel_cpu", cmd["id"], cmd["index"], cmd["cpu"]]
        return "\t".join(clean(f) for f in fields)


COMMANDS = Commands()
MAX_ORDER_AMOUNT = int(CFG.get("max_order_amount", 1_000_000))
MAX_ORDER_FLUID = int(CFG.get("max_order_fluid_amount", 10_000_000))  # L
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
    COMMANDS.apply_report(data.pop("updates", []), data.pop("tracking", []))
    data["received"] = time.time()
    STATE.status = data
    # Pending commands ride back on the response, one per line.
    lines = [Commands.wire(c) for c in COMMANDS.to_deliver()]
    return PlainTextResponse("".join(line + "\n" for line in lines))


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
        STATE.indexed_version = -1
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


def translated_status(status: dict | None) -> dict | None:
    """Copy of the status with a Chinese name "z" added to every CPU item."""
    if not status:
        return status

    def tr(item):
        if not isinstance(item, dict):
            return item
        n, d, l = item.get("n", ""), item.get("d", 0), item.get("l", "")
        zh = TR.lookup("i", n, d, l)
        return {**item, "z": zh, "ic": ICONS.lookup("i", n, d, l, zh)}

    cpus = []
    for cpu in status.get("cpus") or []:
        cpu = dict(cpu)
        cpu["final"] = tr(cpu.get("final"))
        cpu["active"] = [tr(i) for i in cpu.get("active") or []]
        cpu["pending"] = [tr(i) for i in cpu.get("pending") or []]
        cpus.append(cpu)
    return {**status, "cpus": cpus}


@app.get("/api/state")
async def api_state(request: Request):
    require_web(request)
    STATE.ensure_indexed()
    orders = []
    for c in reversed(COMMANDS.items[-30:]):
        kind = c.get("k", "i")
        zh = TR.lookup(kind, c["n"], c["d"], c["l"]) if c["type"] == "craft" else None
        icon = ICONS.lookup(kind, c["n"], c["d"], c["l"], zh) if c["type"] == "craft" else None
        orders.append({**c, "z": zh, "ic": icon})
    return {"now": time.time(), "status": translated_status(STATE.status),
            "items_meta": STATE.items_meta, "i18n": {**TR.info, "version": TR.version},
            "orders": orders, "max_order": MAX_ORDER_AMOUNT, "max_order_fluid": MAX_ORDER_FLUID}


class OrderRequest(BaseModel):
    k: str = Field("i", pattern="^[if]$")  # item or fluid
    n: str
    d: int
    l: str
    amount: int = Field(ge=1)  # items, or L (mB) for fluids
    cpu: str = ""


class CpuCancelRequest(BaseModel):
    index: int = Field(ge=1)
    name: str = ""


def require_oc_online() -> None:
    received = (STATE.status or {}).get("received", 0)
    if time.time() - received > 60:
        raise HTTPException(409, "OC 程序超过 1 分钟没有上报，指令不会被执行，请先检查游戏内的 OC 电脑")


@app.post("/api/orders")
async def create_order(request: Request, order: OrderRequest):
    require_web(request)
    require_oc_online()
    limit = MAX_ORDER_FLUID if order.k == "f" else MAX_ORDER_AMOUNT
    if order.amount > limit:
        raise HTTPException(400, f"数量超过上限 {limit}")
    item = next((r for r in STATE.items if r["k"] == order.k and r["c"] and r["n"] == order.n
                 and r["d"] == order.d and r["l"] == order.l), None)
    if not item:
        raise HTTPException(400, "这个物品没有可用的合成样板")
    cpus = (STATE.status or {}).get("cpus") or []
    if order.cpu and not any(c.get("rawName") == order.cpu for c in cpus):
        raise HTTPException(400, "找不到这个合成 CPU")
    cmd = COMMANDS.add(type="craft", k=order.k, n=order.n, d=order.d, l=order.l,
                       amount=order.amount, cpu=order.cpu)
    return {"ok": True, "id": cmd["id"]}


@app.post("/api/orders/{cmd_id}/cancel")
async def cancel_order(request: Request, cmd_id: str):
    require_web(request)
    cmd = COMMANDS.get(cmd_id)
    if not cmd or cmd["state"] != "queued":
        raise HTTPException(409, "只能撤销还没被 OC 领取的指令")
    COMMANDS.set_state(cmd, "canceled", "withdrawn before delivery")
    COMMANDS.save()
    return {"ok": True}


@app.post("/api/cpus/cancel")
async def cancel_cpu(request: Request, req: CpuCancelRequest):
    require_web(request)
    require_oc_online()
    cpus = (STATE.status or {}).get("cpus") or []
    cpu = next((c for c in cpus if c.get("index") == req.index), None)
    if not cpu or (cpu.get("rawName") or "") != req.name or not cpu.get("busy"):
        raise HTTPException(409, "CPU 状态已变化，请刷新后再试")
    cmd = COMMANDS.add(type="cancel_cpu", index=req.index, cpu=req.name, n="", d=0,
                       l=cpu.get("name", ""), amount=0)
    return {"ok": True, "id": cmd["id"]}


@app.get("/api/items")
async def api_items(request: Request, q: str = "", kind: str = "all", sort: str = "size",
                    order: str = "desc", offset: int = 0, limit: int = 100):
    require_web(request)
    STATE.ensure_indexed()
    rows = STATE.items
    if kind in ("i", "f"):
        rows = [r for r in rows if r["k"] == kind]
    if kind == "c":
        rows = [r for r in rows if r["c"]]
    terms = q.lower().split()
    if terms:
        rows = [r for r in rows if all(t in r["_key"] for t in terms)]
    if sort == "name":
        rows = sorted(rows, key=lambda r: (r["_zh"] or r["l"]).lower(), reverse=(order == "desc"))
    else:
        rows = sorted(rows, key=lambda r: r["s"], reverse=(order != "asc"))
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    page = [{**{k: v for k, v in r.items() if not k.startswith("_")}, "z": r["_zh"], "ic": r["_ic"]}
            for r in rows[offset:offset + limit]]
    return {"total": len(rows), "rows": page}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/icons/{version}/{icon}.png")
async def icon_file(version: str, icon: int):
    # Icons are not secret and their URLs are versioned, so they skip login and
    # can be cached for good (also by Cloudflare).
    if not re.fullmatch(r"\d{14}", version):
        raise HTTPException(404)
    path = ICON_DIR / version / f"{icon}.png"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/oc/aeweb.lua")
async def oc_script():
    # Lets the OC computer fetch the uploader with `wget`; it holds no secrets.
    return FileResponse(BASE_DIR.parent / "oc" / "aeweb.lua", media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    uvicorn.run(app, host=CFG["host"], port=int(CFG["port"]), proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
