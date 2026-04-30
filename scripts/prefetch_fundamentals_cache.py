#!/usr/bin/env python3
"""Prefetch validated A-share fundamentals snapshots into optional Mongo cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.dataflows.fundamentals_cache import write_cached_fundamentals
from tradingagents.dataflows.fundamentals_quality import fetch_supplemental_fundamentals, validate_fundamentals_quality


def load_symbols(raw: str, file_path: str | None) -> list[str]:
    symbols: list[str] = []
    if raw:
        symbols.extend(item.strip() for item in raw.split(",") if item.strip())
    if file_path:
        for line in Path(file_path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                symbols.append(line)
    return sorted(set(symbols))


def main() -> None:
    parser = argparse.ArgumentParser(description="Prefetch validated A-share fundamentals into Mongo cache.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols, e.g. 000977,002335")
    parser.add_argument("--file", help="One symbol per line")
    parser.add_argument("--strict", action="store_true", help="Fail process if any symbol fails")
    args = parser.parse_args()

    results = []
    for symbol in load_symbols(args.symbols, args.file):
        try:
            report, source = fetch_supplemental_fundamentals(symbol)
            quality = validate_fundamentals_quality(report, strict=True)
            if not quality["ok"]:
                raise RuntimeError(f"quality gate failed: {quality}")
            cached = write_cached_fundamentals(symbol, report, source, quality)
            item = {"symbol": symbol, "status": "ok", "source": source, "cached": cached, "quality": quality}
        except Exception as exc:
            item = {"symbol": symbol, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            if args.strict:
                print(json.dumps(item, ensure_ascii=False, indent=2))
                raise
        results.append(item)
        print(json.dumps(item, ensure_ascii=False, indent=2))

    if not results:
        raise SystemExit("请用 --symbols 或 --file 指定股票代码")


if __name__ == "__main__":
    main()
