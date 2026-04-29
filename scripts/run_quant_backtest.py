#!/usr/bin/env python3
"""Run rolling backtest for the local quant layer without LLM calls.

Example:
    .venv/bin/python scripts/run_quant_backtest.py --symbol 603588 --stock-name 高能环境 --start-date 2026-01-01 --end-date 2026-04-29
"""

from __future__ import annotations

import argparse
import json
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

from report_paths import results_stock_dir  # noqa: E402
from tradingagents.quant.backtest import BacktestConfig, run_rolling_backtest, save_backtest_outputs  # noqa: E402
from tradingagents.quant.training import TrainingConfig, build_training_frame, train_model  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run rolling backtest for baseline quant factor model.")
    parser.add_argument("--symbol", required=True, help="A-share symbol, e.g. 603588")
    parser.add_argument("--stock-name", default="", help="Stock name for output folder, e.g. 高能环境")
    parser.add_argument("--start-date", required=True, help="Backtest start date YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="Backtest end date YYYY-MM-DD")
    parser.add_argument("--horizons", default="5,20,60", help="Forward horizons in trading days, comma separated")
    parser.add_argument("--rebalance-step", type=int, default=5, help="Score every N trading days")
    parser.add_argument("--min-history", type=int, default=80, help="Minimum past trading days used for a score")
    parser.add_argument("--score-threshold", type=float, default=52.0, help="Selected signal score threshold")
    parser.add_argument("--fundamentals-file", default="", help="Optional existing fundamentals report markdown")
    parser.add_argument("--out-dir", default="", help="Optional output dir. Default: results/<stock>/<end-date>/reports")
    parser.add_argument("--training-frame", action="store_true", help="Also write supervised training frame and readiness metadata")
    parser.add_argument("--train-model", action="store_true", help="Train optional LightGBM/CatBoost model when enough data and dependencies exist")
    parser.add_argument("--model-type", default="lightgbm", choices=["lightgbm", "catboost"], help="Training model type")
    return parser.parse_args()


def parse_horizons(raw: str) -> tuple[int, ...]:
    horizons = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    if not horizons:
        raise ValueError("horizons不能为空")
    return horizons


def read_fundamentals(path_text: str, symbol: str, stock_name: str, end_date: str) -> str:
    candidates = []
    if path_text:
        path = Path(path_text)
        candidates.append(path if path.is_absolute() else ROOT / path)
    if stock_name:
        candidates.append(results_stock_dir(symbol, stock_name, create=False) / end_date / "reports" / "fundamentals_report.md")
    candidates.append(ROOT / "results" / symbol / end_date / "reports" / "fundamentals_report.md")
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    return ""


def main() -> int:
    args = parse_args()
    horizons = parse_horizons(args.horizons)
    fundamentals_text = read_fundamentals(args.fundamentals_file, args.symbol, args.stock_name, args.end_date)
    config = BacktestConfig(
        symbol=args.symbol,
        start_date=args.start_date,
        end_date=args.end_date,
        horizons=horizons,
        rebalance_step=args.rebalance_step,
        min_history=args.min_history,
        score_threshold=args.score_threshold,
        fundamentals_report=fundamentals_text,
    )
    result = run_rolling_backtest(config)

    if args.out_dir:
        out_dir = Path(args.out_dir)
        if not out_dir.is_absolute():
            out_dir = ROOT / out_dir
    else:
        out_dir = results_stock_dir(args.symbol, args.stock_name, create=True) / args.end_date / "reports"

    paths = save_backtest_outputs(result, out_dir)
    print(f"backtest_report={paths['report']}")
    print(f"backtest_rows={paths['rows_csv']}")
    print(f"backtest_summary={paths['summary_json']}")
    print(f"sample_count={result.summary.get('sample_count')} selected_count={result.summary.get('selected_count')}")

    if args.training_frame or args.train_model:
        train_config = TrainingConfig(
            horizon=horizons[1] if len(horizons) > 1 else horizons[0],
            model_type=args.model_type,
        )
        train_config.label_column = f"return_{train_config.horizon}d"
        frame = build_training_frame(result.rows, train_config)
        frame_path = out_dir / "quant_training_frame.csv"
        meta_path = out_dir / "quant_training_readiness.json"
        model_path = out_dir / f"quant_{args.model_type}_model.txt"
        frame.to_csv(frame_path, index=False)
        train_meta = train_model(frame, train_config, model_path=model_path if args.train_model else None)
        meta_path.write_text(json.dumps(train_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"training_frame={frame_path}")
        print(f"training_readiness={meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
