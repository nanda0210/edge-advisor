#!/usr/bin/env python3
"""
Auto-ingest WhatsApp group exports from a local download folder.

WhatsApp does not expose a safe personal-chat API for direct background
downloads. This watcher handles the reliable local part: once WhatsApp exports
or saves the group zip into Downloads, it automatically feeds the export into
scripts/whatsapp_stock_feed.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


DEFAULT_GROUP = "BuyAlertsContrbutingAndPaidMembers"
DEFAULT_WATCH = Path.home() / "Downloads"
DEFAULT_STATE = Path.home() / "myprojects" / "edge-advisor-local" / "whatsapp-feeds" / ".auto_state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Downloads and auto-ingest WhatsApp group exports.")
    parser.add_argument("--watch", default=str(DEFAULT_WATCH), help="Folder to watch for WhatsApp export zip files.")
    parser.add_argument("--group", default=DEFAULT_GROUP, help="WhatsApp group name to match.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")
    parser.add_argument("--once", action="store_true", help="Scan once, ingest the newest matching export, then exit.")
    parser.add_argument("--retention-days", type=int, default=7, help="Retention passed to whatsapp_stock_feed.py.")
    parser.add_argument("--out", default="", help="Optional output folder passed to whatsapp_stock_feed.py.")
    return parser.parse_args()


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"processed": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def stable_file(path: Path, settle_seconds: int = 5) -> bool:
    try:
        first = path.stat().st_size
        time.sleep(settle_seconds)
        second = path.stat().st_size
        return first > 0 and first == second
    except OSError:
        return False


def candidate_exports(watch: Path, group: str) -> list[Path]:
    tokens = [t.lower() for t in group.replace("_", " ").split() if t]
    files = []
    for path in watch.glob("*.zip"):
        name = path.name.lower()
        if "whatsapp" not in name:
            continue
        if tokens and not all(token in name for token in tokens[:2]):
            # iOS/Android export names can truncate long groups; require at
            # least the first token pair to avoid ingesting unrelated chats.
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def run_ingest(export_zip: Path, args: argparse.Namespace) -> int:
    script = Path(__file__).with_name("whatsapp_stock_feed.py")
    cmd = [
        sys.executable,
        str(script),
        "--input",
        str(export_zip),
        "--group",
        args.group,
        "--retention-days",
        str(args.retention_days),
    ]
    if args.out:
        cmd.extend(["--out", args.out])
    print(f"[{datetime.now().isoformat(timespec='seconds')}] ingesting {export_zip}")
    return subprocess.call(cmd)


def scan_once(args: argparse.Namespace, state: dict) -> bool:
    watch = Path(args.watch).expanduser()
    watch.mkdir(parents=True, exist_ok=True)
    processed = state.setdefault("processed", {})
    for export_zip in candidate_exports(watch, args.group):
        key = str(export_zip.resolve())
        mtime = export_zip.stat().st_mtime
        if processed.get(key) == mtime:
            continue
        if not stable_file(export_zip):
            print(f"[{datetime.now().isoformat(timespec='seconds')}] waiting for download to finish: {export_zip.name}")
            continue
        rc = run_ingest(export_zip, args)
        if rc == 0:
            processed[key] = mtime
            state["lastProcessed"] = {
                "path": key,
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            save_state(DEFAULT_STATE, state)
            return True
        print(f"[{datetime.now().isoformat(timespec='seconds')}] ingest failed rc={rc}: {export_zip}")
    return False


def main() -> int:
    args = parse_args()
    state = load_state(DEFAULT_STATE)
    if args.once:
        processed = scan_once(args, state)
        print("processed" if processed else "no new matching WhatsApp export found")
        return 0
    print(f"Watching {Path(args.watch).expanduser()} for WhatsApp exports for {args.group}")
    print("Leave this running while you export the chat to Downloads.")
    while True:
        scan_once(args, state)
        time.sleep(max(args.interval, 15))


if __name__ == "__main__":
    raise SystemExit(main())
