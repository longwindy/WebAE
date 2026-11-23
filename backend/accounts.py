"""Config loading and the account store, shared by app.py and manage.py.

<data_dir>/accounts.json holds:
  * networks: one per AE network, each with the hash of the token its OC computer uses
  * users:    one per person, with the hash of their login key, an admin flag and a
              role in each network they may use ("owner", "member" or "viewer")
  * secret:   signs login cookies

Only manage.py writes the file; the backend reloads it whenever it changes, so
adding or removing users and networks needs no restart. Keys and tokens are random,
so a plain SHA-256 is enough to store them; the originals are shown once and never kept.
"""

import base64
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

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AEWEB_CONFIG", BASE_DIR / "config.toml"))

ROLES = ("viewer", "member", "owner")  # each role can do everything the ones before it can
ROLE_NAMES = {"viewer": "只读", "member": "成员", "owner": "所有者"}
# Users and networks are both known only by their name. A network's name is also its
# data folder name, so it must be a valid file name on Windows and Linux too.
NAME = re.compile(r'[^\s\x00-\x1f/\\:*?"<>|]{1,32}')
NAME_RULE = '最长 32 个字符，不能有空格和 / \\ : * ? " < > |，也不能以 . 结尾'


def valid_name(name: str) -> bool:
    return bool(NAME.fullmatch(name)) and not name.endswith(".")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Config not found: {CONFIG_PATH} (copy config.example.toml to config.toml)")
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8765)
    data_dir = Path(cfg.get("data_dir", "data"))
    cfg["data_dir"] = data_dir if data_dir.is_absolute() else BASE_DIR / data_dir
    return cfg


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_key() -> str:
    return "aew_" + secrets.token_urlsafe(24)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def role_at_least(role: str | None, need: str) -> bool:
    return role in ROLES and ROLES.index(role) >= ROLES.index(need)


class Accounts:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "accounts.json"
        self.data = self.empty()
        self.mtime = None
        self.by_token: dict[str, str] = {}
        self.by_key: dict[str, str] = {}

    @staticmethod
    def empty() -> dict:
        return {"secret": secrets.token_hex(32), "networks": {}, "users": {}}

    @property
    def networks(self) -> dict:
        return self.data["networks"]

    @property
    def users(self) -> dict:
        return self.data["users"]

    def exists(self) -> bool:
        return self.file.exists()

    def refresh(self) -> None:
        """Reload when the file changed on disk (cheap enough to call on every request)."""
        try:
            mtime = self.file.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime == self.mtime:
            return
        if mtime is not None:
            try:
                self.data = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"Keeping previous accounts, {self.file} is unreadable: {exc}")
                return
        self.mtime = mtime
        self.by_token = {n["token_hash"]: nid for nid, n in self.networks.items()}
        self.by_key = {u["key_hash"]: name for name, u in self.users.items()}

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)
        self.mtime = None
        self.refresh()

    # ------------------------------------------------------------ lookups

    def network_for_token(self, token: str) -> str | None:
        return self.by_token.get(digest(token)) if token else None

    def user_for_key(self, key: str) -> str | None:
        return self.by_key.get(digest(key.strip())) if key else None

    def role(self, user: str, net: str) -> str | None:
        u = self.users.get(user)
        if not u or net not in self.networks:
            return None
        return "owner" if u.get("admin") else u.get("networks", {}).get(net)

    def visible_networks(self, user: str) -> list[dict]:
        out = []
        for net in self.networks:
            role = self.role(user, net)
            if role:
                out.append({"name": net, "role": role})
        return out

    # ------------------------------------------------------------ login cookies
    # "<base64 name>.<signature>": the signature covers the user's current key hash,
    # so resetting the key or removing the user ends every session at once.

    def _sign(self, user: str, key_hash: str) -> str:
        msg = f"aeweb-session-v2\0{user}\0{key_hash}".encode()
        return hmac.new(self.data["secret"].encode(), msg, hashlib.sha256).hexdigest()

    def session_for(self, user: str) -> str:
        name = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
        return f"{name}.{self._sign(user, self.users[user]['key_hash'])}"

    def user_for_session(self, cookie: str) -> str | None:
        name, _, sig = (cookie or "").partition(".")
        try:
            user = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode()
        except ValueError:
            return None
        u = self.users.get(user)
        if not u or not hmac.compare_digest(sig, self._sign(user, u["key_hash"])):
            return None
        return user

    # ------------------------------------------------------------ changes (manage.py)

    def network_dir(self, net: str) -> Path:
        return self.file.parent / "networks" / net

    def add_network(self, net: str) -> str:
        token = new_token()
        self.networks[net] = {"token_hash": digest(token), "created": time.time()}
        return token

    def rename_network(self, old: str, new: str) -> None:
        """Rename a network together with its data folder and everyone's grants."""
        old_dir, new_dir = self.network_dir(old), self.network_dir(new)
        if old_dir.exists():
            old_dir.rename(new_dir)
        self.networks[new] = self.networks.pop(old)
        for info in self.users.values():
            grants = info.get("networks", {})
            if old in grants:
                grants[new] = grants.pop(old)

    def add_user(self, name: str, admin: bool = False) -> str:
        key = new_key()
        self.users[name] = {"key_hash": digest(key), "admin": admin, "networks": {}, "created": time.time()}
        return key
