"""Import item icons from an NEI item panel dump.

Usage:  uv run python import_icons.py "<game dir>/dumps"

The dump directory must hold itempanel.csv (CSV mode) and itempanel_icons/
(PNG mode), both dumped one after the other without touching the item panel.
NEI names each PNG after the item's display name, so the icons are matched to
the CSV rows by replaying NEI's naming: illegal characters become "_", and a
name already taken (case-insensitively, as on Windows) gets "_2", "_3", ...
A few items change their name between the two dumps; those leftovers are
paired with the remaining files by text similarity.

Icons are copied to <data_dir>/icons/<version>/<row>.png with an index.json.
"""

import csv
import difflib
import json
import shutil
import sys
import time
import tomllib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ILLEGAL = {34, 60, 62, 124, 58, 42, 63, 92, 47, *range(32)}  # NEI GuiItemIconDumper


def clean_file_name(name: str) -> str:
    return "".join("_" if ord(c) in ILLEGAL else c for c in name)


def data_dir() -> Path:
    cfg_path = BASE_DIR / "config.toml"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    path = Path(cfg.get("data_dir", "data"))
    return path if path.is_absolute() else BASE_DIR / path


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    dump = Path(sys.argv[1])
    icon_dir = dump / "itempanel_icons"
    with (dump / "itempanel.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    files = {p.name.lower(): p for p in icon_dir.iterdir() if p.suffix.lower() == ".png"}
    print(f"{len(rows)} CSV rows, {len(files)} icon files")

    used: set[str] = set()
    matched: dict[int, Path] = {}
    unmatched_rows: list[int] = []
    for i, row in enumerate(rows):
        base = clean_file_name(row["Display Name"])
        name, n = f"{base}.png", 2
        while name.lower() in used:
            name, n = f"{base}_{n}.png", n + 1
        used.add(name.lower())
        if name.lower() in files:
            matched[i] = files[name.lower()]
        else:
            unmatched_rows.append(i)

    leftovers = [files[k] for k in files if k not in used]
    for i in unmatched_rows:
        if not leftovers:
            break
        target = clean_file_name(rows[i]["Display Name"])
        best = max(leftovers, key=lambda p: difflib.SequenceMatcher(None, target, p.stem).ratio())
        matched[i] = best
        leftovers.remove(best)
        print(f"  paired by similarity: {rows[i]['Item Name']}:{rows[i]['Item meta']} -> {best.name}")
    print(f"matched {len(matched)} / {len(rows)}, {len(leftovers)} icon files left unused")

    root = data_dir() / "icons"
    version = time.strftime("%Y%m%d%H%M%S")
    target_dir = root / version
    target_dir.mkdir(parents=True)
    index = []
    for i, row in enumerate(rows):
        if i in matched:
            shutil.copyfile(matched[i], target_dir / f"{i}.png")
        index.append([row["Item Name"], row["Item meta"], row["Display Name"], i if i in matched else None])
    (target_dir / "index.json").write_text(json.dumps({"version": version, "rows": index},
                                                      ensure_ascii=False), encoding="utf-8")
    (root / "current.txt").write_text(version, encoding="utf-8")
    for old in root.iterdir():  # drop earlier imports
        if old.is_dir() and old.name != version:
            shutil.rmtree(old)
    print(f"icons imported to {target_dir}")


if __name__ == "__main__":
    main()
