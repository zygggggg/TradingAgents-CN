#!/usr/bin/env python3
"""Run 东方财富 Skills tools and optionally save markdown output."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_env_file(path: Path, override: bool = False) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


def cn_date() -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def default_output_path(kind: str, symbol: str | None = None, name: str | None = None) -> Path:
    date_text = cn_date()
    if symbol:
        folder = ROOT / "analysis_outputs" / (name or symbol)
        return folder / f"{name or symbol}-{date_text}-eastmoney-{kind}.md"
    folder = ROOT / "analysis_outputs" / "东方财富Skills"
    return folder / f"东方财富Skills-{date_text}-{kind}.md"


def write_output(text: str, path: Path | None) -> None:
    if path is None:
        print(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"✅ 已写出: {path}")


def load_positions(value: str) -> List[Dict[str, Any]]:
    path = Path(value).expanduser()
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = json.loads(value)
    if isinstance(payload, dict) and isinstance(payload.get("positions"), list):
        return payload["positions"]
    if isinstance(payload, list):
        return payload
    raise ValueError("positions 必须是 JSON 数组，或包含 positions 数组的 JSON 文件")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="东方财富 Skills / OpenClaw 金融工具")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="环境变量文件，默认项目 .env")
    parser.add_argument("--output", help="写出 markdown 文件路径；不传则使用默认 analysis_outputs 路径")
    parser.add_argument("--stdout", action="store_true", help="直接打印到终端，不写文件")

    sub = parser.add_subparsers(dest="command", required=True)

    fundamentals = sub.add_parser("fundamentals", help="金融数据查询/财务透视")
    fundamentals.add_argument("symbol")
    fundamentals.add_argument("--name")
    fundamentals.add_argument("--report-count", type=int, default=5)

    news = sub.add_parser("news", help="金融资讯搜索")
    news.add_argument("symbol")
    news.add_argument("--name")
    news.add_argument("--limit", type=int, default=10)

    screen = sub.add_parser("screen", help="智能选股")
    screen.add_argument("--keyword", default="A股近60日涨幅小于25%，股价距离MA60小于15%，一季报净利润或扣非净利润改善，经营现金流没有明显恶化，成交量不是高位爆量滞涨，技术面低位修复，目标价与止损空间风险收益比大于2比1，排除连续加速缩量新高")
    screen.add_argument("--page-size", type=int, default=30)

    hotspot = sub.add_parser("hotspot", help="热点发现")
    hotspot.add_argument("--focus", default="A股市场热点、AI、算力、机器人、军工")

    diagnose = sub.add_parser("diagnose", help="综合诊股")
    diagnose.add_argument("symbol")
    diagnose.add_argument("--name")

    watchlist = sub.add_parser("watchlist", help="自选股监控")
    watchlist.add_argument("symbols", nargs="+")

    portfolio = sub.add_parser("portfolio", help="模拟组合风控复盘")
    portfolio.add_argument("positions", help="持仓 JSON 字符串或 JSON 文件路径")

    return parser


def load_skills_client():
    try:
        from tradingagents.dataflows.providers.china.eastmoney_skills import get_eastmoney_skills_client

        return get_eastmoney_skills_client()
    except ModuleNotFoundError:
        import importlib.util

        module_path = ROOT / "tradingagents" / "dataflows" / "providers" / "china" / "eastmoney_skills.py"
        spec = importlib.util.spec_from_file_location("eastmoney_skills_standalone", module_path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.get_eastmoney_skills_client()


def main() -> int:
    args = build_parser().parse_args()
    load_env_file(Path(args.env), override=False)

    try:
        client = load_skills_client()
        if args.command == "fundamentals":
            text = client.fundamentals_report(args.symbol, stock_name=args.name, report_count=args.report_count)
            default_path = default_output_path("fundamentals", args.symbol, args.name)
        elif args.command == "news":
            text = client.news_report(args.symbol, stock_name=args.name, limit_hint=args.limit)
            default_path = default_output_path("news", args.symbol, args.name)
        elif args.command == "screen":
            call = client.screen_stocks(args.keyword, page_size=args.page_size)
            text = client.format_call("东方财富 Skills 智能选股", call, args.keyword)
            default_path = default_output_path("screen")
        elif args.command == "hotspot":
            text = client.hotspot_report(args.focus)
            default_path = default_output_path("hotspot")
        elif args.command == "diagnose":
            text = client.stock_diagnosis_report(args.symbol, stock_name=args.name)
            default_path = default_output_path("diagnose", args.symbol, args.name)
        elif args.command == "watchlist":
            text = client.watchlist_report(args.symbols)
            default_path = default_output_path("watchlist")
        elif args.command == "portfolio":
            text = client.portfolio_risk_report(load_positions(args.positions))
            default_path = default_output_path("portfolio")
        else:  # pragma: no cover
            raise RuntimeError(f"未知命令: {args.command}")
    except Exception as exc:
        print(f"❌ 东方财富 Skills 调用失败: {exc}", file=sys.stderr)
        return 2

    output_path = None if args.stdout else Path(args.output).expanduser() if args.output else default_path
    write_output(text, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
