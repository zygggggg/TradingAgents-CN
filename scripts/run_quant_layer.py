#!/usr/bin/env python3
"""Run the deterministic quant layer without invoking any LLM.

Example:
    .venv/bin/python scripts/run_quant_layer.py --symbol 603588 --stock-name 高能环境 --date 2026-04-29
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(_path):
        return False

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

from report_paths import results_stock_dir, safe_name  # noqa: E402
from tradingagents.quant import generate_quant_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate baseline quant score report without LLM calls.")
    parser.add_argument("--symbol", required=True, help="A-share symbol, e.g. 603588")
    parser.add_argument("--stock-name", default="", help="Stock name for output folder, e.g. 高能环境")
    parser.add_argument("--date", default="", help="Analysis date YYYY-MM-DD. Default: today")
    parser.add_argument("--market-type", default="A股", help="Market type. Current quant baseline supports A股")
    parser.add_argument("--fundamentals-file", default="", help="Optional existing fundamentals report markdown")
    parser.add_argument("--out-dir", default="", help="Optional output directory. Default: results/<stock>/<date>/reports")
    parser.add_argument("--json", action="store_true", help="Also write quant_analysis.json")
    return parser.parse_args()


def read_optional_text(path_text: str) -> Optional[str]:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"fundamentals file not found: {path}")
    return path.read_text(encoding="utf-8", errors="ignore")


def default_fundamentals_path(symbol: str, stock_name: str, date: str) -> Optional[Path]:
    candidates = []
    if stock_name:
        candidates.append(results_stock_dir(symbol, stock_name, create=False) / date / "reports" / "fundamentals_report.md")
    candidates.append(ROOT / "results" / symbol / date / "reports" / "fundamentals_report.md")
    for path in candidates:
        if path.exists():
            return path
    return None


def main() -> int:
    args = parse_args()
    date = args.date or __import__("datetime").date.today().strftime("%Y-%m-%d")

    fundamentals_text = read_optional_text(args.fundamentals_file)
    if fundamentals_text is None:
        path = default_fundamentals_path(args.symbol, args.stock_name, date)
        if path:
            fundamentals_text = path.read_text(encoding="utf-8", errors="ignore")

    quant, report = generate_quant_report(
        stock_symbol=args.symbol,
        analysis_date=date,
        market_type=args.market_type,
        fundamentals_report=fundamentals_text,
    )

    if args.out_dir:
        out_dir = Path(args.out_dir)
        if not out_dir.is_absolute():
            out_dir = ROOT / out_dir
    else:
        stock_dir = results_stock_dir(args.symbol, args.stock_name, create=True)
        out_dir = stock_dir / date / "reports"

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "quant_report.md"
    report_path.write_text(report.rstrip() + "\n", encoding="utf-8")

    json_path = None
    if args.json:
        json_path = out_dir / "quant_analysis.json"
        json_path.write_text(json.dumps(quant or {}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"quant_report={report_path}")
    if json_path:
        print(f"quant_json={json_path}")
    if quant:
        print(f"score={quant.get('score')} signal={quant.get('signal')} risk={quant.get('risk_level')}")
    else:
        print("score=N/A")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
