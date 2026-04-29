"""Rolling backtest utilities for the local quant layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from tradingagents.quant.engine import _load_history_frame, _normalize_history, _score_stock, format_quant_report
from tradingagents.utils.logging_manager import get_logger

logger = get_logger("quant")


@dataclass
class BacktestConfig:
    symbol: str
    start_date: str
    end_date: str
    horizons: Sequence[int] = (5, 20, 60)
    rebalance_step: int = 5
    min_history: int = 80
    score_threshold: float = 52.0
    fundamentals_report: str = ""


@dataclass
class BacktestResult:
    config: BacktestConfig
    rows: pd.DataFrame
    summary: Dict[str, Any]
    data_source: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": {
                "symbol": self.config.symbol,
                "start_date": self.config.start_date,
                "end_date": self.config.end_date,
                "horizons": list(self.config.horizons),
                "rebalance_step": self.config.rebalance_step,
                "min_history": self.config.min_history,
                "score_threshold": self.config.score_threshold,
            },
            "data_source": self.data_source,
            "summary": self.summary,
            "rows": self.rows.to_dict(orient="records"),
        }


def run_rolling_backtest(config: BacktestConfig) -> BacktestResult:
    """Backtest baseline quant scores on rolling historical windows.

    It scores each rebalance date using only data available up to that date, then
    measures forward returns over configured trading-day horizons.
    """

    from tradingagents.dataflows.providers.china.integrated import get_integrated_china_provider

    provider = get_integrated_china_provider()
    fetch_start = _calendar_lookback(config.start_date, max(420, config.min_history * 3))
    history_df, data_source = _load_history_frame(provider, config.symbol, fetch_start, config.end_date)
    history_df = _normalize_history(history_df)
    if history_df.empty:
        raise RuntimeError(f"未获取到{config.symbol}历史行情")

    start_ts = pd.to_datetime(config.start_date)
    rows: List[Dict[str, Any]] = []
    max_horizon = max(config.horizons)

    for index in range(config.min_history, len(history_df) - max_horizon, config.rebalance_step):
        current_date = history_df.iloc[index]["date"]
        if current_date < start_ts:
            continue
        window = history_df.iloc[: index + 1].copy()
        result = _score_stock(
            symbol=config.symbol,
            analysis_date=current_date.strftime("%Y-%m-%d"),
            history_df=window,
            fundamentals_text=config.fundamentals_report,
            data_source=data_source,
        )
        latest_close = float(history_df.iloc[index]["close"])
        row = {
            "date": current_date.strftime("%Y-%m-%d"),
            "close": latest_close,
            "score": round(result.score, 2),
            "signal": result.signal,
            "risk_level": result.risk_level,
            "factor_momentum": result.factors["momentum"]["score"],
            "factor_trend": result.factors["trend"]["score"],
            "factor_risk": result.factors["risk"]["score"],
            "factor_valuation": result.factors["valuation"]["score"],
            "factor_quality": result.factors["quality"]["score"],
            "factor_liquidity": result.factors["liquidity"]["score"],
        }
        for horizon in config.horizons:
            future_close = float(history_df.iloc[index + horizon]["close"])
            row[f"return_{horizon}d"] = (future_close / latest_close - 1) * 100
        rows.append(row)

    rows_df = pd.DataFrame(rows)
    summary = summarize_backtest(rows_df, config.horizons, config.score_threshold)
    return BacktestResult(config=config, rows=rows_df, summary=summary, data_source=data_source)


def summarize_backtest(rows: pd.DataFrame, horizons: Sequence[int], score_threshold: float) -> Dict[str, Any]:
    if rows.empty:
        return {"sample_count": 0, "message": "样本不足，无法回测"}

    summary: Dict[str, Any] = {
        "sample_count": int(len(rows)),
        "score_mean": round(float(rows["score"].mean()), 2),
        "score_median": round(float(rows["score"].median()), 2),
        "score_threshold": score_threshold,
    }
    selected = rows[rows["score"] >= score_threshold]
    summary["selected_count"] = int(len(selected))
    summary["selected_ratio"] = round(float(len(selected) / len(rows)), 4) if len(rows) else 0.0

    for horizon in horizons:
        column = f"return_{horizon}d"
        all_returns = rows[column].dropna()
        selected_returns = selected[column].dropna() if not selected.empty else pd.Series(dtype=float)
        summary[f"h{horizon}"] = {
            "all_avg_return": _round_or_none(all_returns.mean()),
            "all_win_rate": _round_or_none((all_returns > 0).mean()),
            "selected_avg_return": _round_or_none(selected_returns.mean()),
            "selected_win_rate": _round_or_none((selected_returns > 0).mean()),
            "selected_median_return": _round_or_none(selected_returns.median()),
            "selected_max_drawdown_proxy": _round_or_none(selected_returns.min()),
        }
    return summary


def format_backtest_report(result: BacktestResult) -> str:
    config = result.config
    summary = result.summary
    lines = [
        "## 📈 量化滚动回测报告",
        "",
        f"- 股票代码: {config.symbol}",
        f"- 回测区间: {config.start_date} 至 {config.end_date}",
        f"- 调仓步长: 每 {config.rebalance_step} 个交易日评分一次",
        f"- 最小历史窗口: {config.min_history} 个交易日",
        f"- 入选阈值: 量化评分 >= {config.score_threshold}",
        f"- 数据来源: {result.data_source}",
        f"- 样本数: {summary.get('sample_count', 0)}，入选次数: {summary.get('selected_count', 0)}",
        "",
    ]

    if not summary.get("sample_count"):
        lines.append(summary.get("message", "样本不足"))
        return "\n".join(lines)

    lines.extend(
        [
            "### 分周期表现",
            "周期 | 全样本平均收益 | 全样本胜率 | 入选平均收益 | 入选胜率 | 入选中位收益 | 入选最差单期",
            "--- | ---: | ---: | ---: | ---: | ---: | ---:",
        ]
    )
    for horizon in config.horizons:
        item = summary.get(f"h{horizon}", {})
        lines.append(
            f"{horizon}日 | {_fmt_pct(item.get('all_avg_return'))} | {_fmt_rate(item.get('all_win_rate'))} | "
            f"{_fmt_pct(item.get('selected_avg_return'))} | {_fmt_rate(item.get('selected_win_rate'))} | "
            f"{_fmt_pct(item.get('selected_median_return'))} | {_fmt_pct(item.get('selected_max_drawdown_proxy'))}"
        )

    lines.extend(
        [
            "",
            "### 最近评分样本",
            "日期 | 收盘价 | 评分 | 信号 | 5日收益 | 20日收益 | 60日收益",
            "--- | ---: | ---: | --- | ---: | ---: | ---:",
        ]
    )
    for _, row in result.rows.tail(12).iterrows():
        lines.append(
            f"{row['date']} | {row['close']:.2f} | {row['score']:.1f} | {row['signal']} | "
            f"{_fmt_pct(row.get('return_5d'))} | {_fmt_pct(row.get('return_20d'))} | {_fmt_pct(row.get('return_60d'))}"
        )

    lines.extend(
        [
            "",
            "### 解释",
            "- 该回测只检验本地基线因子评分的历史表现，不代表未来收益。",
            "- 单只股票样本量有限，更可靠的结论需要扩展到股票池横截面回测。",
            "- 下一步训练版模型会用这些滚动样本生成标签，训练LightGBM/CatBoost排序器。",
        ]
    )
    return "\n".join(lines)


def save_backtest_outputs(result: BacktestResult, out_dir: Path) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "quant_backtest_report.md"
    csv_path = out_dir / "quant_backtest_rows.csv"
    json_path = out_dir / "quant_backtest_summary.json"
    report_path.write_text(format_backtest_report(result).rstrip() + "\n", encoding="utf-8")
    result.rows.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report": str(report_path), "rows_csv": str(csv_path), "summary_json": str(json_path)}


def _calendar_lookback(start_date: str, days: int) -> str:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    return (start - timedelta(days=days)).strftime("%Y-%m-%d")


def _round_or_none(value: Any, digits: int = 4) -> Optional[float]:
    try:
        if pd.isna(value):
            return None
        return round(float(value), digits)
    except Exception:
        return None


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        if pd.isna(value):
            return "N/A"
        return f"{float(value):+.2f}%"
    except Exception:
        return "N/A"


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        if pd.isna(value):
            return "N/A"
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "N/A"
