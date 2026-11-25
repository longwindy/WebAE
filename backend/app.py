"""AE web view backend.

Several AE networks can report here, each through its own OC computer and token.
The OpenComputers program pushes two kinds of data:
  * POST /api/oc/status  small JSON: AE power and crafting CPUs, every few seconds
  * POST /api/oc/items   inventory snapshot as tab-separated text, sent in
                         numbered chunks and swapped in once the last chunk arrives
The browser reads it back through /api/state and /api/items (with ?net=<name>) after
logging in with a per-user key. Users and networks live in accounts.py / manage.py.

Item names arrive in the server's language (English). Chinese names come from
NEI item panel dumps placed in <data_dir>/i18n (see Translations).
"""

import asyncio
import base64
import csv
import hashlib
import json
import re
import secrets
import struct
import time
import zlib
from pathlib import Path

import pypinyin
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from pydantic import BaseModel, Field

from accounts import BASE_DIR, Accounts, digest, load_config, role_at_least

MAX_BODY = 4 * 1024 * 1024
PENDING_TTL = 600
COOKIE = "aeweb_session"

CFG = load_config()
I18N_DIR = CFG["data_dir"] / "i18n"
NETWORKS_DIR = CFG["data_dir"] / "networks"


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

# ---------------------------------------------------------------- pinyin search
# JECH-style: a query matches a run of consecutive characters in the Chinese
# name when each character is matched by itself or by a non-empty prefix of
# any of its readings, so "tieding", "td", "tied" and "铁d" all find 铁锭.

_READINGS: dict[str, tuple[str, ...]] = {}


def char_readings(ch: str) -> tuple[str, ...]:
    readings = _READINGS.get(ch)
    if readings is None:
        readings = ()
        if "㐀" <= ch <= "鿿":
            options = pypinyin.pinyin(ch, style=pypinyin.Style.NORMAL, heteronym=True)[0]
            readings = tuple(sorted({p for p in options if p.isascii() and p.isalpha()}))
        _READINGS[ch] = readings
    return readings


def pinyin_index(text: str) -> tuple[list[tuple[str, tuple[str, ...]]], frozenset[str]]:
    """Per-character readings plus every letter/character a match can start with."""
    chars = [(ch, char_readings(ch)) for ch in text.lower()]
    starts = frozenset(ch for ch, _ in chars) | frozenset(r[0] for _, rs in chars for r in rs)
    return chars, starts


def pinyin_match(term: str, index) -> bool:
    chars, starts = index
    if term[0] not in starts:
        return False
    m = len(term)
    for start in range(len(chars)):
        positions = {0}
        for ch, readings in chars[start:]:
            reached = set()
            for pos in positions:
                if term[pos] == ch:
                    reached.add(pos + 1)
                for reading in readings:
                    k = 0
                    while k < len(reading) and pos + k < m and term[pos + k] == reading[k]:
                        k += 1
                        reached.add(pos + k)
            if m in reached:
                return True
            positions = reached
            if not positions:
                break
    return False


def term_matches(term: str, row: dict) -> bool:
    if term in row["_key"]:
        return True
    return (row["_py"] is not None and any("a" <= c <= "z" for c in term)
            and pinyin_match(term, row["_py"]))


def index_rows(rows: list[dict]) -> None:
    """Attach the Chinese name, icon and search data to each row."""
    for row in rows:
        zh = TR.lookup(row["k"], row["n"], row["d"], row["l"])
        row["_zh"] = zh
        row["_ic"] = ICONS.lookup(row["k"], row["n"], row["d"], row["l"], zh)
        row["_key"] = "\0".join((row["n"], row["l"], zh or "")).lower()
        # A renamed item can also be found by the pinyin of its new name. "\0" has no
        # reading, so a match never runs across the two names.
        spoken = "\0".join(filter(None, (zh, row["l"] if row.get("rn") else None)))
        row["_py"] = pinyin_index(spoken) if spoken else None


class State:
    def __init__(self, file: Path) -> None:
        self.file = file
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
        if not self.file.exists():
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Ignoring unreadable {self.file}: {exc}")
            return
        self.status = data.get("status")
        self.items = data.get("items", [])
        self.items_meta = data.get("items_meta")

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        items = [{k: v for k, v in row.items() if not k.startswith("_")} for row in self.items]
        payload = {"status": self.status, "items": items, "items_meta": self.items_meta}
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.file)


class Commands:
    """Orders queued from the web page and carried out by the OC program.

    A command is handed to OC in the response to its status upload ("sent"),
    re-sent if OC has not reported on it within RESEND_AFTER seconds, and
    dropped ("expired") if OC never picked it up within EXPIRE_AFTER seconds so
    an old order cannot fire long after it was placed. OC records each id
    before acting on it, so a re-sent command never runs twice.
    """

    RESEND_AFTER = 30
    EXPIRE_AFTER = 300
    LOST_AFTER = 60
    KEEP_ORDERS = 10  # finished craft orders kept for the history panel
    KEEP_OTHER = 20   # finished CPU-cancel commands, kept only for bookkeeping
    TERMINAL = {"done", "failed", "canceled", "expired", "lost", "error"}

    def __init__(self, file: Path) -> None:
        self.FILE = file
        self.items: list[dict] = []
        if self.FILE.exists():
            try:
                self.items = json.loads(self.FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"Ignoring unreadable {self.FILE}: {exc}")
        self.prune()

    def prune(self) -> None:
        """Drop finished commands beyond the keep limits; unfinished ones always stay."""
        budget = {"craft": self.KEEP_ORDERS}
        keep = []
        for cmd in reversed(self.items):
            if cmd["state"] in self.TERMINAL:
                kind = "craft" if cmd.get("type") == "craft" else "other"
                left = budget.get(kind, self.KEEP_OTHER)
                if left <= 0:
                    continue
                budget[kind] = left - 1
            keep.append(cmd)
        self.items = keep[::-1]

    def save(self) -> None:
        self.prune()
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
        if state in self.TERMINAL and not cmd.get("finished"):
            cmd["finished"] = cmd["updated"]

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



class CpuTracker:
    """Per busy CPU: when the job started, its target and the latest details.

    OC re-reads only some busy CPUs per report ("detail"), and the target only
    now and then (a "finalError" key marks that it was read), so both are kept
    here between reports. The start time is exact to one report interval when
    the CPU was seen idle in the previous report; otherwise (first report after
    a restart, or a long gap) it is only a lower bound.
    """

    GAP = 60  # seconds without reports after which idle/busy history is unknown

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.prev_busy: set[str] | None = None
        self.prev_time = 0.0

    @staticmethod
    def key(cpu: dict) -> str:
        return f"{cpu.get('index')}|{cpu.get('rawName', '')}|{cpu.get('storage')}"

    def update(self, cpus: list, now: float) -> None:
        known_history = self.prev_busy is not None and now - self.prev_time <= self.GAP
        busy_now = set()
        for cpu in cpus if isinstance(cpus, list) else []:
            if not isinstance(cpu, dict):
                continue
            detail = cpu.pop("detail", None)
            final_read = "finalError" in cpu
            final, final_error = cpu.pop("final", None), cpu.pop("finalError", None)
            for old in ("active", "pending", "truncated"):  # older OC scripts
                cpu.pop(old, None)
            if not cpu.get("busy"):
                continue
            key = self.key(cpu)
            busy_now.add(key)
            job = self.jobs.get(key)
            if job is None:
                job = self.jobs[key] = {"since": now, "exact": known_history and key not in self.prev_busy,
                                        "final": None, "finalError": None, "detail": None, "detailAt": None}
            if final_read:
                same = lambda a, b: (a or {}).get("n") == (b or {}).get("n") and (a or {}).get("d") == (b or {}).get("d")
                if job["final"] and final and not same(job["final"], final):
                    # one job ended and another began between two reports
                    job.update(since=now, exact=False, detail=None, detailAt=None)
                job["final"], job["finalError"] = final, final_error or None
            if isinstance(detail, dict):
                job["detail"], job["detailAt"] = detail, now
        for key in list(self.jobs):
            if key not in busy_now:
                del self.jobs[key]
        self.prev_busy, self.prev_time = busy_now, now

    def job(self, cpu: dict) -> dict | None:
        return self.jobs.get(self.key(cpu)) if cpu.get("busy") else None


class Network:
    """Everything the backend keeps for one AE network, under <data_dir>/networks/<name>/."""

    def __init__(self, name: str) -> None:
        self.name = name
        folder = NETWORKS_DIR / name
        self.state = State(folder / "state.json")
        self.state.load()
        self.commands = Commands(folder / "commands.json")
        self.cpus = CpuTracker()


ACCOUNTS = Accounts(CFG["data_dir"])
_networks: dict[str, Network] = {}


def migrate_single_network() -> None:
    """First start after the multi-user update: the old single network becomes "main".

    Its OC keeps working because the network takes over oc_token from config.toml,
    and the old state.json / commands.json move into the network's folder.
    """
    if ACCOUNTS.exists():
        return
    token = str(CFG.get("oc_token", ""))
    if not token or token.startswith("CHANGE-ME"):
        return
    ACCOUNTS.data = Accounts.empty()
    ACCOUNTS.networks["main"] = {"token_hash": digest(token), "created": time.time()}
    folder = NETWORKS_DIR / "main"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("state.json", "commands.json"):
        old = CFG["data_dir"] / name
        if old.exists() and not (folder / name).exists():
            old.replace(folder / name)
    ACCOUNTS.save()
    print("Moved the existing AE network to network 'main' (it keeps the oc_token from config.toml).")
    print("Rename it with:     uv run python manage.py net rename main <new-name>")
    print("Add yourself with:  uv run python manage.py user add <your-name> --admin")


migrate_single_network()
ACCOUNTS.refresh()
if not ACCOUNTS.users:
    print("No users yet. Add one with:  uv run python manage.py user add <your-name> --admin")


def network(name: str) -> Network | None:
    """The live Network for a name in accounts.json (created on first use)."""
    ACCOUNTS.refresh()
    for stale in [n for n in _networks if n not in ACCOUNTS.networks]:  # renamed or removed
        del _networks[stale]
    if name not in ACCOUNTS.networks:
        return None
    if name not in _networks:
        _networks[name] = Network(name)
    return _networks[name]


# Defaults match the old AE amount box limit (int max, about 2.1g); OC passes it on as a long.
MAX_ORDER_AMOUNT = int(CFG.get("max_order_amount", 2_147_483_647))
MAX_ORDER_FLUID = int(CFG.get("max_order_fluid_amount", 2_147_483_647))  # L
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- auth

def require_oc(request: Request) -> Network:
    """The network whose OC token the request carries."""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    ACCOUNTS.refresh()
    name = ACCOUNTS.network_for_token(token)
    net = network(name) if name else None
    if not net:
        raise HTTPException(401, "bad token")
    return net


def current_user(request: Request) -> str | None:
    ACCOUNTS.refresh()
    return ACCOUNTS.user_for_session(request.cookies.get(COOKIE, ""))


class Access:
    def __init__(self, user: str, net: Network, role: str) -> None:
        self.user, self.net, self.role = user, net, role


def require_web(request: Request, net: str, need: str = "viewer") -> Access:
    """The logged-in user and the network they asked for, if their role there is at least `need`."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "login required")
    role = ACCOUNTS.role(user, net)
    live = network(net) if role else None
    if not live:
        raise HTTPException(403, "没有这个网络的访问权限")
    if not role_at_least(role, need):
        raise HTTPException(403, "你在这个网络里没有这个操作的权限")
    return Access(user, live, role)


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
    net = require_oc(request)
    try:
        data = json.loads(await read_body(request))
    except ValueError:
        raise HTTPException(400, "invalid json")
    if not isinstance(data, dict):
        raise HTTPException(400, "expected object")
    net.commands.apply_report(data.pop("updates", []), data.pop("tracking", []))
    data["received"] = time.time()
    net.cpus.update(data.get("cpus"), data["received"])  # moves CPU details into the tracker
    net.state.status = data
    # Pending commands ride back on the response, one per line.
    lines = [Commands.wire(c) for c in net.commands.to_deliver()]
    return PlainTextResponse("".join(line + "\n" for line in lines))


NBT_MAX_BYTES = 64 * 1024  # decompressed; the OC side only sends small tags anyway


def nbt_has_custom_name(tag_b64: str) -> bool:
    """True when the GZIP'ed item NBT (from OC's `tag` field) has display.Name,
    which is what an anvil rename writes."""
    try:
        packed = base64.b64decode(tag_b64, validate=True)
        inflater = zlib.decompressobj(wbits=31)  # gzip container
        data = inflater.decompress(packed, NBT_MAX_BYTES)
        if inflater.unconsumed_tail:
            return False
        return _nbt_display_name(data)
    except (ValueError, zlib.error, IndexError, struct.error, RecursionError):
        return False


def _nbt_display_name(data: bytes) -> bool:
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if n < 0 or pos + n > len(data):
            raise ValueError("truncated NBT")
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    def name() -> str:
        return take(struct.unpack(">H", take(2))[0]).decode("utf-8", errors="replace")

    sizes = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}

    def skip(kind: int) -> None:
        if kind in sizes:
            take(sizes[kind])
        elif kind == 7:  # byte array
            take(struct.unpack(">i", take(4))[0])
        elif kind == 8:  # string
            take(struct.unpack(">H", take(2))[0])
        elif kind == 9:  # list
            inner, count = take(1)[0], struct.unpack(">i", take(4))[0]
            for _ in range(count):
                skip(inner)
        elif kind == 10:  # compound
            while (child := take(1)[0]) != 0:
                name()
                skip(child)
        elif kind == 11:  # int array
            take(4 * struct.unpack(">i", take(4))[0])
        else:
            raise ValueError(f"unknown NBT tag {kind}")

    def compound_has(path: list[str]) -> bool:
        """Reads a compound's payload; True when it holds path (compounds, then a string)."""
        found = False
        while (kind := take(1)[0]) != 0:
            key = name()
            if key == path[0] and len(path) == 1 and kind == 8:
                skip(kind)
                found = True
            elif key == path[0] and len(path) > 1 and kind == 10:
                found = compound_has(path[1:]) or found
            else:
                skip(kind)
        return found

    if take(1)[0] != 10:
        return False
    name()
    return compound_has(["display", "Name"])


def parse_rows(text: str) -> list[dict]:
    rows = []
    for line in text.split("\n"):
        if not line:
            continue
        # The 7th field (newer OC scripts) is the item's base64 GZIP'ed NBT, or empty.
        parts = line.split("\t", 6)
        if len(parts) not in (6, 7) or parts[0] not in ("i", "f"):
            raise HTTPException(400, f"bad line: {line[:80]!r}")
        kind, name, damage, size, craftable, label = parts[:6]
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
        if len(parts) == 7 and parts[6] and nbt_has_custom_name(parts[6]):
            row["rn"] = True  # renamed (anvil): show the label instead of the translation
        rows.append(row)
    return rows


@app.post("/api/oc/items")
async def oc_items(request: Request, snap: str, seq: int, final: int = 0,
                   count: int = -1, scan: float = 0.0):
    state = require_oc(request).state
    body = await read_body(request)
    chunk_hash = hashlib.sha1(body).hexdigest()
    now = time.time()
    pending = state.pending
    if pending and now - pending["started"] > PENDING_TTL:
        pending = state.pending = None

    if seq == 0:
        pending = state.pending = {"snap": snap, "rows": [], "next": 0,
                                   "started": now, "last": None}
    if not pending or pending["snap"] != snap:
        return JSONResponse({"ok": False, "error": "unknown_snapshot"}, status_code=409)
    if seq == pending["next"] - 1 and pending["last"] == chunk_hash:
        return {"ok": True, "duplicate": True}
    if seq != pending["next"]:
        return JSONResponse({"ok": False, "error": "out_of_order",
                             "expected": pending["next"]}, status_code=409)

    pending["rows"].extend(parse_rows(body.decode("utf-8", errors="replace")))
    pending["next"] += 1
    pending["last"] = chunk_hash

    if final:
        rows = pending["rows"]
        state.pending = None
        if count >= 0 and count != len(rows):
            return JSONResponse({"ok": False, "error": "count_mismatch",
                                 "got": len(rows)}, status_code=409)
        state.items = rows
        state.indexed_version = -1
        state.items_meta = {
            "updated": now,
            "items": sum(1 for r in rows if r["k"] == "i"),
            "fluids": sum(1 for r in rows if r["k"] == "f"),
            "scan_seconds": scan,
        }
        await asyncio.to_thread(state.save)
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
.hint{color:var(--muted);font-size:12px;margin:12px 0 0}
</style></head><body><form method="post" action="login"><h1>AE 网络</h1>{error}
<input type="password" name="key" placeholder="登录密钥" autocomplete="current-password" autofocus required>
<button>登录</button><p class="hint">密钥由管理员发放，登录后 30 天内不用再输入。</p></form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_PAGE.replace("{error}", "")


@app.post("/login")
async def login(request: Request, key: str = Form(...)):
    ACCOUNTS.refresh()
    user = ACCOUNTS.user_for_key(key)
    if not user:
        await asyncio.sleep(1.0)
        return HTMLResponse(LOGIN_PAGE.replace("{error}", '<p class="err">密钥无效</p>'), 401)
    response = RedirectResponse("./", status_code=303)
    secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    response.set_cookie(COOKIE, ACCOUNTS.session_for(user), max_age=30 * 86400, httponly=True,
                        samesite="lax", secure=secure)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.get("/")
async def index(request: Request):
    if not current_user(request):
        return RedirectResponse("login", status_code=303)
    return FileResponse(BASE_DIR / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/me")
async def api_me(request: Request):
    """Who is logged in and which networks they can open."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "login required")
    return {"user": user, "admin": bool(ACCOUNTS.users[user].get("admin")),
            "networks": ACCOUNTS.visible_networks(user)}


def translate_item(item):
    if not isinstance(item, dict):
        return item
    n, d, l = item.get("n", ""), item.get("d", 0), item.get("l", "")
    zh = TR.lookup("i", n, d, l)
    return {**item, "z": zh, "ic": ICONS.lookup("i", n, d, l, zh)}


def cpu_summary(net: Network, cpu: dict) -> dict:
    """CPU list entry: static info plus, when busy, target, start time and detail age."""
    job = net.cpus.job(cpu)
    out = dict(cpu)
    if job:
        detail = job["detail"] or {}
        out.update(final=translate_item(job["final"]), finalError=job["finalError"],
                   since=job["since"], exact=job["exact"], detailAt=job["detailAt"],
                   counts={k: len(detail.get(k) or []) for k in ("stored", "active", "pending")})
    return out


def cpu_rows(detail: dict) -> list[dict]:
    """Merge the stored/active/pending lists into one row per item, like the monitor GUI."""
    rows: dict[tuple, dict] = {}
    for field, short in (("stored", "s"), ("active", "a"), ("pending", "p")):
        for it in detail.get(field) or []:
            key = (it.get("n", ""), it.get("d", 0), it.get("l", ""))
            row = rows.setdefault(key, {"n": key[0], "d": key[1], "l": key[2], "s": 0, "a": 0, "p": 0})
            row[short] += it.get("s", 0)
    out = []
    for row in rows.values():
        row = translate_item(row)
        row["_key"] = "\0".join((row["n"], row["l"], row["z"] or "")).lower()
        row["_py"] = pinyin_index(row["z"]) if row["z"] else None
        out.append(row)
    out.sort(key=lambda r: (-(r["a"] > 0), -(r["p"] > 0), (r["z"] or r["l"]).lower()))
    return out


def translated_status(net: Network) -> dict | None:
    status = net.state.status
    if not status:
        return status
    return {**status, "cpus": [cpu_summary(net, c) for c in status.get("cpus") or []]}


@app.get("/api/state")
async def api_state(request: Request, net: str):
    acc = require_web(request, net)
    state = acc.net.state
    state.ensure_indexed()
    orders = []
    for c in reversed(acc.net.commands.items):
        if c["type"] != "craft":
            continue
        kind = c.get("k", "i")
        zh = TR.lookup(kind, c["n"], c["d"], c["l"])
        icon = ICONS.lookup(kind, c["n"], c["d"], c["l"], zh)
        orders.append({**c, "z": zh, "ic": icon})
    return {"now": time.time(), "user": acc.user, "role": acc.role,
            "status": translated_status(acc.net),
            "items_meta": state.items_meta, "i18n": {**TR.info, "version": TR.version},
            "orders": orders, "max_order": MAX_ORDER_AMOUNT, "max_order_fluid": MAX_ORDER_FLUID}


@app.get("/api/cpus/{index}")
async def api_cpu(request: Request, net: str, index: int, q: str = ""):
    """One CPU with its merged stored/active/pending rows, optionally searched."""
    acc = require_web(request, net)
    acc.net.state.ensure_indexed()
    cpu = next((c for c in (acc.net.state.status or {}).get("cpus") or [] if c.get("index") == index), None)
    if not cpu:
        raise HTTPException(404, "没有这个 CPU")
    job = acc.net.cpus.job(cpu)
    rows = cpu_rows(job["detail"]) if job and job["detail"] else []
    total = len(rows)
    terms = q.lower().split()
    if terms:
        rows = [r for r in rows if all(term_matches(t, r) for t in terms)]
    return {"now": time.time(), "cpu": cpu_summary(acc.net, cpu), "total": total,
            "truncated": bool(job and (job["detail"] or {}).get("truncated")),
            "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]}


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


def require_oc_online(net: Network) -> None:
    received = (net.state.status or {}).get("received", 0)
    if time.time() - received > 60:
        raise HTTPException(409, "OC 程序超过 1 分钟没有上报，指令不会被执行，请先检查游戏内的 OC 电脑")


@app.post("/api/orders")
async def create_order(request: Request, net: str, order: OrderRequest):
    acc = require_web(request, net, "member")
    require_oc_online(acc.net)
    limit = MAX_ORDER_FLUID if order.k == "f" else MAX_ORDER_AMOUNT
    if order.amount > limit:
        raise HTTPException(400, f"数量超过上限 {limit}")
    item = next((r for r in acc.net.state.items if r["k"] == order.k and r["c"] and r["n"] == order.n
                 and r["d"] == order.d and r["l"] == order.l), None)
    if not item:
        raise HTTPException(400, "这个物品没有可用的合成样板")
    cpus = (acc.net.state.status or {}).get("cpus") or []
    if order.cpu and not any(c.get("rawName") == order.cpu for c in cpus):
        raise HTTPException(400, "找不到这个合成 CPU")
    cmd = acc.net.commands.add(type="craft", k=order.k, n=order.n, d=order.d, l=order.l,
                               amount=order.amount, cpu=order.cpu, user=acc.user)
    return {"ok": True, "id": cmd["id"]}


@app.post("/api/orders/{cmd_id}/cancel")
async def cancel_order(request: Request, net: str, cmd_id: str):
    acc = require_web(request, net, "member")
    commands = acc.net.commands
    cmd = commands.get(cmd_id)
    if not cmd or cmd["state"] != "queued":
        raise HTTPException(409, "只能撤销还没被 OC 领取的指令")
    if acc.role != "owner" and cmd.get("user") != acc.user:
        raise HTTPException(403, "只能撤回自己下的订单")
    commands.set_state(cmd, "canceled", "withdrawn before delivery")
    commands.save()
    return {"ok": True}


@app.post("/api/cpus/cancel")
async def cancel_cpu(request: Request, net: str, req: CpuCancelRequest):
    # Owners only: a CPU job may have been started in game by someone else.
    acc = require_web(request, net, "owner")
    require_oc_online(acc.net)
    cpus = (acc.net.state.status or {}).get("cpus") or []
    cpu = next((c for c in cpus if c.get("index") == req.index), None)
    if not cpu or (cpu.get("rawName") or "") != req.name or not cpu.get("busy"):
        raise HTTPException(409, "CPU 状态已变化，请刷新后再试")
    cmd = acc.net.commands.add(type="cancel_cpu", index=req.index, cpu=req.name, n="", d=0,
                               l=cpu.get("name", ""), amount=0, user=acc.user)
    return {"ok": True, "id": cmd["id"]}


@app.get("/api/items")
async def api_items(request: Request, net: str, q: str = "", kind: str = "all", sort: str = "size",
                    order: str = "desc", offset: int = 0, limit: int = 100):
    state = require_web(request, net).net.state
    state.ensure_indexed()
    rows = state.items
    if kind in ("i", "f"):
        rows = [r for r in rows if r["k"] == kind]
    if kind == "c":
        rows = [r for r in rows if r["c"]]
    terms = q.lower().split()
    if terms:
        rows = [r for r in rows if all(term_matches(t, r) for t in terms)]
    if sort == "name":
        rows = sorted(rows, key=lambda r: (r["l"] if r.get("rn") else r["_zh"] or r["l"]).lower(), reverse=(order == "desc"))
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
