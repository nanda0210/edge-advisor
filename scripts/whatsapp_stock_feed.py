#!/usr/bin/env python3
"""
Local WhatsApp export ingester for NandaEdge Advisor.

This script does not log in to WhatsApp, scrape WhatsApp Web, trade, or touch
broker/account data. It only reads a WhatsApp "Export Chat" zip/folder that you
provide, copies recent text/media into a local one-week cache, and writes a
read-only stock-signal JSON file for Advisor review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


DEFAULT_GROUP = "BuyAlertsContrbutingAndPaidMembers"
DEFAULT_CACHE = Path.home() / "myprojects" / "edge-advisor-local" / "whatsapp-feeds"
SYMBOL_RE = re.compile(r"(?<![A-Z0-9])\$?([A-Z]{1,5})(?![A-Z0-9])")
PRICE_RE = re.compile(r"(?i)\b(?:entry|buy|above|breakout|target|exit|sell|stop|sl)\D{0,18}(\d+(?:\.\d+)?)")
MEDIA_EXTS = {
    "images": {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"},
    "videos": {".mp4", ".mov", ".m4v", ".avi", ".webm"},
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".ppt", ".pptx"},
}
STOP_WORDS = {
    "A", "AM", "AN", "AND", "ARE", "AS", "AT", "BE", "BUY", "CALL", "CAN", "CEO",
    "CFO", "CPI", "DAY", "DD", "DTE", "EPS", "ETF", "EXIT", "FOR", "GDP", "HAS",
    "HIGH", "HOD", "IN", "IPO", "IS", "IT", "LOW", "MACD", "NEW", "NO", "NOW",
    "OF", "ON", "OR", "PUT", "RSI", "SEC", "SEE", "SELL", "SL", "THE", "TO",
    "TP", "USD", "VWAP", "WAS", "WE", "YOU",
}


@dataclass
class Signal:
    symbol: str
    bias: str
    confidence: int
    mentions: int
    latestMessageTime: str | None
    levels: list[float]
    reasons: list[str]
    sampleMessages: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest one exported WhatsApp group into a local stock feed.")
    parser.add_argument("--input", required=True, help="Path to WhatsApp export .zip or extracted folder.")
    parser.add_argument("--group", default=DEFAULT_GROUP, help="Expected WhatsApp group name.")
    parser.add_argument("--out", default=str(DEFAULT_CACHE), help="Local output/cache folder.")
    parser.add_argument("--retention-days", type=int, default=7, help="Delete copied cache files older than this many days.")
    parser.add_argument("--max-samples", type=int, default=4, help="Message samples retained per symbol.")
    parser.add_argument("--advisor-json", default="", help="Optional explicit output JSON path.")
    return parser.parse_args()


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "whatsapp_group"


def extract_input(src: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    if src.is_dir():
        return src, None
    if not src.exists():
        raise FileNotFoundError(f"Input not found: {src}")
    if src.suffix.lower() != ".zip":
        raise ValueError("Input must be a WhatsApp export .zip or extracted folder.")
    tmp = tempfile.TemporaryDirectory(prefix="nandaedge-wa-")
    with zipfile.ZipFile(src) as zf:
        zf.extractall(tmp.name)
    return Path(tmp.name), tmp


def find_chat_file(root: Path) -> Path:
    candidates = list(root.rglob("_chat.txt")) + list(root.rglob("WhatsApp Chat*.txt")) + list(root.rglob("*.txt"))
    if not candidates:
        raise FileNotFoundError("No WhatsApp chat .txt file found in the export.")
    return max(candidates, key=lambda p: p.stat().st_size)


def parse_message_time(line: str) -> datetime | None:
    # iOS: [5/23/26, 9:41:08 AM] Name: message
    m = re.match(r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)\]", line)
    if m:
        for fmt in ("%m/%d/%y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2).upper()}", fmt)
            except ValueError:
                pass
    # Android: 5/23/26, 9:41 AM - Name: message
    m = re.match(r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)\s+-", line)
    if m:
        for fmt in ("%m/%d/%y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2).upper()}", fmt)
            except ValueError:
                pass
    return None


def read_messages(chat_file: Path) -> list[dict]:
    messages: list[dict] = []
    current: dict | None = None
    for raw in chat_file.read_text(encoding="utf-8", errors="replace").splitlines():
        ts = parse_message_time(raw)
        if ts:
            if current:
                messages.append(current)
            current = {"time": ts, "text": raw}
        elif current:
            current["text"] += "\n" + raw
    if current:
        messages.append(current)
    return messages


def classify_media(path: Path) -> str | None:
    ext = path.suffix.lower()
    for kind, exts in MEDIA_EXTS.items():
        if ext in exts:
            return kind
    return None


def copy_media(root: Path, output_root: Path, retention_days: int) -> dict:
    media_root = output_root / "media"
    counts = Counter()
    bytes_by_kind = Counter()
    seen_hashes = set()
    for kind in MEDIA_EXTS:
        (media_root / kind).mkdir(parents=True, exist_ok=True)
        for existing in (media_root / kind).glob("*"):
            if existing.is_file():
                try:
                    seen_hashes.add(sha1(existing))
                except OSError:
                    pass

    for src in root.rglob("*"):
        if not src.is_file() or src.name.startswith("."):
            continue
        kind = classify_media(src)
        if not kind:
            continue
        try:
            digest = sha1(src)
        except OSError:
            continue
        if digest in seen_hashes:
            continue
        stamp = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
        dest = media_root / kind / f"{stamp}_{digest[:10]}_{safe_name(src.name)}"
        shutil.copy2(src, dest)
        seen_hashes.add(digest)
        counts[kind] += 1
        bytes_by_kind[kind] += dest.stat().st_size

    prune_old_files(output_root, retention_days)
    return {
        "copied": dict(counts),
        "bytes": dict(bytes_by_kind),
        "mediaRoot": str(media_root),
    }


def prune_old_files(root: Path, retention_days: int) -> int:
    cutoff = datetime.now().timestamp() - (retention_days * 86400)
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "whatsapp_stock_feed.json":
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def extract_symbols(text: str) -> set[str]:
    symbols = set()
    for match in SYMBOL_RE.findall(text.upper()):
        if match not in STOP_WORDS and not match.isdigit():
            symbols.add(match)
    return symbols


def signal_bias(text: str) -> str:
    t = text.lower()
    bullish = sum(word in t for word in ("buy", "long", "breakout", "above", "calls", "call", "target", "momentum"))
    bearish = sum(word in t for word in ("sell", "short", "puts", "put", "breakdown", "below", "stop", "exit"))
    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    return "watch"


def build_signals(messages: list[dict], max_samples: int) -> list[Signal]:
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        symbols = extract_symbols(msg["text"])
        for symbol in symbols:
            by_symbol[symbol].append(msg)

    signals: list[Signal] = []
    for symbol, hits in by_symbol.items():
        texts = "\n".join(h["text"] for h in hits)
        bias_counts = Counter(signal_bias(h["text"]) for h in hits)
        bias = bias_counts.most_common(1)[0][0]
        levels = sorted({float(x) for x in PRICE_RE.findall(texts)})[:8]
        mention_score = min(35, len(hits) * 7)
        level_score = 15 if levels else 0
        urgency_score = 15 if re.search(r"(?i)\b(now|alert|entry|breakout|above|below)\b", texts) else 0
        media_score = 10 if re.search(r"(?i)<attached:|image omitted|video omitted|\.jpg|\.png|\.mp4|\.pdf", texts) else 0
        confidence = min(90, 35 + mention_score + level_score + urgency_score + media_score)
        reasons = [f"{len(hits)} group mention(s)", f"{bias} language majority"]
        if levels:
            reasons.append("price/entry/exit levels detected")
        if media_score:
            reasons.append("message references attached media")
        latest = max(h["time"] for h in hits if h.get("time"))
        sample_messages = [clean_sample(h["text"]) for h in hits[-max_samples:]]
        signals.append(Signal(symbol, bias, confidence, len(hits), latest.isoformat(), levels, reasons, sample_messages))

    return sorted(signals, key=lambda s: (s.confidence, s.mentions), reverse=True)[:25]


def clean_sample(text: str) -> str:
    text = re.sub(r"^\[?\d{1,2}/\d{1,2}/\d{2,4}.*?(?:\]\s*|-)\s*", "", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def write_text_archive(messages: list[dict], output_root: Path, retention_days: int) -> Path:
    text_dir = output_root / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    start = datetime.now() - timedelta(days=retention_days)
    recent = [m for m in messages if not m.get("time") or m["time"] >= start]
    archive = text_dir / f"messages_last_{retention_days}_days.txt"
    with archive.open("w", encoding="utf-8") as f:
        for msg in recent:
            ts = msg["time"].isoformat() if msg.get("time") else "unknown"
            f.write(f"[{ts}] {msg['text']}\n\n")
    return archive


def main() -> int:
    args = parse_args()
    src = Path(args.input).expanduser()
    output_root = Path(args.out).expanduser() / safe_name(args.group)
    output_root.mkdir(parents=True, exist_ok=True)

    extracted_root, tmp = extract_input(src)
    try:
        chat_file = find_chat_file(extracted_root)
        messages = read_messages(chat_file)
        text_archive = write_text_archive(messages, output_root, args.retention_days)
        media = copy_media(extracted_root, output_root, args.retention_days)
        signals = build_signals(messages, args.max_samples)
        result = {
            "group": args.group,
            "generatedAt": datetime.now().isoformat(),
            "retentionDays": args.retention_days,
            "source": str(src),
            "messageCount": len(messages),
            "textArchive": str(text_archive),
            "media": media,
            "advisorUse": "Read-only stock-signal intelligence. Validate against NandaEdge Advisor market data before decisions.",
            "signals": [asdict(s) for s in signals],
        }
        advisor_json = Path(args.advisor_json).expanduser() if args.advisor_json else output_root / "whatsapp_stock_feed.json"
        advisor_json.parent.mkdir(parents=True, exist_ok=True)
        advisor_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({
            "ok": True,
            "group": args.group,
            "messageCount": len(messages),
            "signals": len(signals),
            "advisorJson": str(advisor_json),
            "mediaRoot": media["mediaRoot"],
        }, indent=2))
        return 0
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
