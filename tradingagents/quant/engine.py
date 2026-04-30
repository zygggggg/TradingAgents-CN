"""Lightweight quantitative scoring for A-share analysis reports.

This module is intentionally deterministic and dependency-light.  It provides a
baseline factor score that can later be replaced by a trained LightGBM/CatBoost
ranking model while keeping the same public interface.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from tradingagents.utils.logging_manager import get_logger

logger = get_logger("quant")


@dataclass
class QuantAnalysisResult:
    """Structured quantitative result used by reports and future model swaps."""

    symbol: str
    analysis_date: str
    score: float
    rating: str
    signal: str
    suggested_position: str
    risk_level: str
    factors: Dict[str, Dict[str, Any]]
    metrics: Dict[str, Any]
    warnings: List[str]
    data_source: str
    model_name: str = "baseline_factor_v1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "analysis_date": self.analysis_date,
            "score": round(self.score, 1),
            "rating": self.rating,
            "signal": self.signal,
            "suggested_position": self.suggested_position,
            "risk_level": self.risk_level,
            "factors": self.factors,
            "metrics": self.metrics,
            "warnings": self.warnings,
            "data_source": self.data_source,
            "model_name": self.model_name,
        }


def generate_quant_report(
    stock_symbol: str,
    analysis_date: str,
    market_type: str = "A股",
    fundamentals_report: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Generate a baseline quantitative report for the stock.

    The current implementation is a deterministic factor model using public
    market/fundamental data.  It is designed as a safe offline score, not an
    auto-trading instruction.
    """

    if market_type != "A股":
        report = (
            "## 📊 量化评分\n\n"
            f"当前基线量化模块仅支持A股，收到市场类型: {market_type}。\n\n"
            "后续可扩展到美股/港股，或接入训练好的多市场模型。"
        )
        return None, report

    try:
        from tradingagents.dataflows.providers.china.integrated import get_integrated_china_provider

        provider = get_integrated_china_provider()
        start_date = _history_start_date(analysis_date, calendar_days=260)
        history_df, data_source = _load_history_frame(provider, stock_symbol, start_date, analysis_date)
        if history_df.empty:
            raise RuntimeError("未获取到可用历史行情")

        fundamentals_text = fundamentals_report
        if not fundamentals_text:
            fundamentals_text = provider.get_fundamentals_data(stock_symbol, report_count=5)

        info = provider.get_stock_info(stock_symbol)
        result = _score_stock(
            symbol=stock_symbol,
            analysis_date=analysis_date,
            history_df=history_df,
            fundamentals_text=fundamentals_text or "",
            data_source=data_source,
            stock_name=info.get("name") if isinstance(info, dict) else None,
        )
        return result.to_dict(), format_quant_report(result)
    except Exception as exc:
        logger.warning("量化评分生成失败: %s %s", stock_symbol, exc)
        fallback_report = (
            "## 📊 量化评分\n\n"
            f"量化评分暂不可用：{exc}\n\n"
            "报告主体仍可参考LLM投研结论；建议补齐行情/财务数据后重跑。"
        )
        return None, fallback_report


def _history_start_date(analysis_date: str, calendar_days: int = 260) -> str:
    try:
        end = datetime.strptime(analysis_date, "%Y-%m-%d")
    except Exception:
        end = datetime.now()
    return (end - timedelta(days=calendar_days)).strftime("%Y-%m-%d")


def _load_history_frame(provider: Any, symbol: str, start_date: str, end_date: str) -> Tuple[pd.DataFrame, str]:
    errors: List[str] = []
    for source in provider.source_order():
        try:
            result = provider._try_history_source(source, symbol, start_date, end_date, "daily")
            if result.ok and isinstance(result.data, pd.DataFrame) and not result.data.empty:
                df = result.data.copy()
                return _normalize_history(df), result.source
            if result.error:
                errors.append(f"{source}: {result.error}")
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    raise RuntimeError("历史行情数据源全部失败: " + "; ".join(errors[-4:]))


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "close", "high", "low", "volume", "amount", "pct_change", "turnover"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low"])
    if "pct_change" not in df.columns or df["pct_change"].isna().all():
        df["pct_change"] = df["close"].pct_change() * 100
    return df.sort_values("date").reset_index(drop=True)


def _score_stock(
    symbol: str,
    analysis_date: str,
    history_df: pd.DataFrame,
    fundamentals_text: str,
    data_source: str,
    stock_name: Optional[str] = None,
) -> QuantAnalysisResult:
    metrics = _calculate_market_metrics(history_df)
    fundamental_metrics = _extract_fundamental_metrics(fundamentals_text)
    metrics.update(fundamental_metrics)

    factors: Dict[str, Dict[str, Any]] = {
        "momentum": _score_momentum(metrics),
        "trend": _score_trend(metrics),
        "risk": _score_risk(metrics),
        "valuation": _score_valuation(metrics),
        "quality": _score_quality(metrics),
        "liquidity": _score_liquidity(metrics),
    }

    weights = {
        "momentum": 0.22,
        "trend": 0.18,
        "risk": 0.16,
        "valuation": 0.16,
        "quality": 0.20,
        "liquidity": 0.08,
    }
    weighted_score = sum(factors[name]["score"] * weight for name, weight in weights.items())
    score = max(0.0, min(100.0, weighted_score))

    warnings = _build_warnings(metrics, factors)
    rating, signal, suggested_position = _rating_from_score(score, metrics)
    risk_level = _risk_level(metrics)

    if stock_name:
        metrics["stock_name"] = stock_name

    return QuantAnalysisResult(
        symbol=symbol,
        analysis_date=analysis_date,
        score=score,
        rating=rating,
        signal=signal,
        suggested_position=suggested_position,
        risk_level=risk_level,
        factors=factors,
        metrics=metrics,
        warnings=warnings,
        data_source=data_source,
    )


def _calculate_market_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    close = df["close"].astype(float)
    pct = close.pct_change()
    latest = df.iloc[-1]
    latest_close = float(latest["close"])

    def ma(days: int) -> Optional[float]:
        if len(close) < days:
            return None
        return float(close.tail(days).mean())

    def ret(days: int) -> Optional[float]:
        if len(close) <= days:
            return None
        base = float(close.iloc[-days - 1])
        if base == 0:
            return None
        return (latest_close / base - 1) * 100

    ma5 = ma(5)
    ma20 = ma(20)
    ma60 = ma(60)
    volatility20 = float(pct.tail(20).std() * math.sqrt(252) * 100) if len(pct.dropna()) >= 20 else None
    max_drawdown60 = _max_drawdown(close.tail(60)) if len(close) >= 20 else None
    avg_amount20 = _tail_mean(df, "amount", 20)
    avg_turnover20 = _tail_mean(df, "turnover", 20)
    volume_ratio = _volume_ratio(df)
    rsi14 = _rsi(close, 14)

    return {
        "latest_date": latest.get("date").strftime("%Y-%m-%d") if hasattr(latest.get("date"), "strftime") else str(latest.get("date")),
        "latest_close": latest_close,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "return_5d": ret(5),
        "return_20d": ret(20),
        "return_60d": ret(60),
        "volatility_20d_annualized": volatility20,
        "max_drawdown_60d": max_drawdown60,
        "avg_amount_20d": avg_amount20,
        "avg_turnover_20d": avg_turnover20,
        "volume_ratio_5d_vs_20d": volume_ratio,
        "rsi14": rsi14,
        "history_days": len(df),
    }


def _tail_mean(df: pd.DataFrame, column: str, days: int) -> Optional[float]:
    if column not in df.columns or len(df) == 0:
        return None
    values = pd.to_numeric(df[column], errors="coerce").tail(days).dropna()
    if values.empty:
        return None
    return float(values.mean())


def _volume_ratio(df: pd.DataFrame) -> Optional[float]:
    if "volume" not in df.columns or len(df) < 20:
        return None
    short = pd.to_numeric(df["volume"], errors="coerce").tail(5).mean()
    medium = pd.to_numeric(df["volume"], errors="coerce").tail(20).mean()
    if pd.isna(short) or pd.isna(medium) or medium == 0:
        return None
    return float(short / medium)


def _max_drawdown(series: pd.Series) -> Optional[float]:
    if series.empty:
        return None
    running_max = series.cummax()
    drawdown = series / running_max - 1
    return float(drawdown.min() * 100)


def _rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    latest_loss = loss.iloc[-1]
    if pd.isna(latest_loss):
        return None
    if latest_loss == 0:
        return 100.0
    rs = gain.iloc[-1] / latest_loss
    return float(100 - 100 / (1 + rs))


def _extract_fundamental_metrics(text: str) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    if not text:
        return metrics

    patterns = {
        "revenue_yoy": r"营业总收入:.*?同比:\s*([+-]?\d+(?:\.\d+)?)%",
        "profit_yoy": r"归母净利润:.*?同比:\s*([+-]?\d+(?:\.\d+)?)%",
        "deducted_profit_yoy": r"扣非归母净利润:.*?同比:\s*([+-]?\d+(?:\.\d+)?)%",
        "roe": r"(?:ROE|净资产收益率)[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)%",
        "gross_margin": r"毛利率[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)%",
        "net_margin": r"净利率[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)%",
        "debt_ratio": r"资产负债率[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)%",
        "current_ratio": r"流动比率:\s*([+-]?\d+(?:\.\d+)?)x",
        "quick_ratio": r"速动比率:\s*([+-]?\d+(?:\.\d+)?)x",
        "pe_simple": r"(?:简单PE|PE\s*/?市盈率|市盈率\s*PE|PE)[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:x|倍)?",
        "pb_simple": r"(?:简单PB|PB\s*/?市净率|市净率\s*PB|PB)[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:x|倍)?",
        "ps_simple": r"(?:简单PS|PS\s*/?市销率|市销率\s*PS|PS)[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:x|倍)?",
    }
    for key, pattern in patterns.items():
        value = _regex_float(pattern, text)
        if value is not None:
            metrics[key] = value

    operating_cash_flow = _extract_latest_table_value(text, "现金流量表摘要", "经营现金流净额")
    if operating_cash_flow is not None:
        metrics["operating_cash_flow"] = operating_cash_flow
    return metrics


def _regex_float(pattern: str, text: str) -> Optional[float]:
    match = re.search(pattern, text, flags=re.S)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _extract_latest_table_value(text: str, section_title: str, column_name: str) -> Optional[float]:
    section_match = re.search(rf"##\s*{re.escape(section_title)}\s*\n(.+?)(?:\n##\s|\Z)", text, flags=re.S)
    if not section_match:
        return None
    lines = [line.strip() for line in section_match.group(1).splitlines() if "|" in line]
    if len(lines) < 3:
        return None
    header = [item.strip() for item in lines[0].split("|")]
    try:
        column_index = header.index(column_name)
    except ValueError:
        return None
    row = [item.strip() for item in lines[2].split("|")]
    if column_index >= len(row):
        return None
    return _parse_chinese_money(row[column_index])


def _parse_chinese_money(value: str) -> Optional[float]:
    value = value.strip().replace(",", "")
    if not value or value.upper() == "N/A":
        return None
    match = re.search(r"([+-]?\d+(?:\.\d+)?)(亿|万)?", value)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit == "亿":
        return number * 100_000_000
    if unit == "万":
        return number * 10_000
    return number


def _score_momentum(metrics: Dict[str, Any]) -> Dict[str, Any]:
    score = 50.0
    score += _bounded(metrics.get("return_20d"), -20, 20) * 18
    score += _bounded(metrics.get("return_60d"), -35, 35) * 12
    rsi = metrics.get("rsi14")
    if rsi is not None:
        if 45 <= rsi <= 65:
            score += 8
        elif 30 <= rsi < 45:
            score += 3
        elif rsi > 75:
            score -= 10
        elif rsi < 25:
            score -= 6
    return _factor(score, "动量", "近20/60日收益与RSI位置")


def _score_trend(metrics: Dict[str, Any]) -> Dict[str, Any]:
    latest = metrics.get("latest_close")
    ma5 = metrics.get("ma5")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    score = 50.0
    if latest and ma20:
        score += 14 if latest >= ma20 else -14
    if latest and ma60:
        score += 12 if latest >= ma60 else -12
    if ma5 and ma20:
        score += 8 if ma5 >= ma20 else -8
    if ma20 and ma60:
        score += 8 if ma20 >= ma60 else -8
    volume_ratio = metrics.get("volume_ratio_5d_vs_20d")
    if volume_ratio is not None:
        score += max(-6, min(6, (volume_ratio - 1) * 10))
    return _factor(score, "趋势", "均线结构与近期量能")


def _score_risk(metrics: Dict[str, Any]) -> Dict[str, Any]:
    score = 72.0
    volatility = metrics.get("volatility_20d_annualized")
    drawdown = metrics.get("max_drawdown_60d")
    debt_ratio = metrics.get("debt_ratio")
    if volatility is not None:
        score -= max(0, volatility - 28) * 0.7
        score += max(0, 22 - volatility) * 0.4
    if drawdown is not None:
        score -= max(0, abs(drawdown) - 12) * 1.2
    if debt_ratio is not None:
        score -= max(0, debt_ratio - 65) * 0.6
        score += max(0, 45 - debt_ratio) * 0.2
    return _factor(score, "风险", "波动率、回撤和资产负债率，分数越高风险越可控")


def _score_valuation(metrics: Dict[str, Any]) -> Dict[str, Any]:
    pe = metrics.get("pe_simple")
    pb = metrics.get("pb_simple")
    ps = metrics.get("ps_simple")
    profit_yoy = metrics.get("profit_yoy")
    score = 50.0
    if pe is not None:
        if pe <= 0:
            score -= 18
        elif pe < 15:
            score += 18
        elif pe < 30:
            score += 10
        elif pe < 50:
            score += 0
        elif pe < 80:
            score -= 10
        else:
            score -= 20
    if pb is not None:
        if pb < 1.5:
            score += 10
        elif pb < 3:
            score += 4
        elif pb > 8:
            score -= 14
        elif pb > 5:
            score -= 8
    if ps is not None:
        if ps < 2:
            score += 8
        elif ps > 12:
            score -= 12
        elif ps > 6:
            score -= 6
    if profit_yoy is not None and profit_yoy > 30:
        score += 6
    return _factor(score, "估值", "简单PE/PB/PS与利润增速匹配度")


def _score_quality(metrics: Dict[str, Any]) -> Dict[str, Any]:
    score = 50.0
    roe = metrics.get("roe")
    net_margin = metrics.get("net_margin")
    revenue_yoy = metrics.get("revenue_yoy")
    profit_yoy = metrics.get("profit_yoy")
    operating_cash_flow = metrics.get("operating_cash_flow")
    current_ratio = metrics.get("current_ratio")

    if roe is not None:
        score += max(-16, min(18, (roe - 8) * 1.2))
    if net_margin is not None:
        score += max(-10, min(12, (net_margin - 5) * 0.8))
    if revenue_yoy is not None:
        score += max(-12, min(12, revenue_yoy * 0.25))
    if profit_yoy is not None:
        score += max(-14, min(16, profit_yoy * 0.22))
    if operating_cash_flow is not None:
        score += 8 if operating_cash_flow > 0 else -10
    if current_ratio is not None:
        score += 5 if current_ratio >= 1.2 else -5
    return _factor(score, "质量", "ROE、利润率、成长性、现金流和偿债能力")


def _score_liquidity(metrics: Dict[str, Any]) -> Dict[str, Any]:
    avg_amount = metrics.get("avg_amount_20d")
    avg_turnover = metrics.get("avg_turnover_20d")
    score = 50.0
    if avg_amount is not None:
        if avg_amount >= 500_000_000:
            score += 22
        elif avg_amount >= 100_000_000:
            score += 12
        elif avg_amount < 30_000_000:
            score -= 18
    if avg_turnover is not None:
        if 0.5 <= avg_turnover <= 8:
            score += 8
        elif avg_turnover > 15:
            score -= 8
    return _factor(score, "流动性", "20日成交额与换手率")


def _bounded(value: Optional[float], low: float, high: float) -> float:
    if value is None:
        return 0.0
    clipped = max(low, min(high, float(value)))
    midpoint = (high + low) / 2
    half_range = (high - low) / 2
    return (clipped - midpoint) / half_range if half_range else 0.0


def _factor(score: float, label: str, explanation: str) -> Dict[str, Any]:
    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "label": label,
        "explanation": explanation,
        "grade": _grade(score),
    }


def _grade(score: float) -> str:
    if score >= 75:
        return "强"
    if score >= 60:
        return "偏强"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "偏弱"
    return "弱"


def _rating_from_score(score: float, metrics: Dict[str, Any]) -> Tuple[str, str, str]:
    if score >= 75:
        return "强势", "量化偏多", "50%-70%目标仓位，仍需人工确认"
    if score >= 65:
        return "偏强", "谨慎看多", "30%-50%目标仓位，回踩或突破确认后执行"
    if score >= 52:
        return "中性偏强", "观察/小仓位", "0%-30%目标仓位，等待更明确确认"
    if score >= 42:
        return "中性偏弱", "观望", "原则上不超过20%试探仓位"
    return "弱势", "规避", "空仓或仅保留观察仓位"


def _risk_level(metrics: Dict[str, Any]) -> str:
    volatility = metrics.get("volatility_20d_annualized")
    drawdown = metrics.get("max_drawdown_60d")
    debt_ratio = metrics.get("debt_ratio")
    risk_points = 0
    if volatility is not None and volatility > 45:
        risk_points += 1
    if drawdown is not None and drawdown < -25:
        risk_points += 1
    if debt_ratio is not None and debt_ratio > 70:
        risk_points += 1
    if risk_points >= 2:
        return "高"
    if risk_points == 1:
        return "中"
    return "低-中"


def _build_warnings(metrics: Dict[str, Any], factors: Dict[str, Dict[str, Any]]) -> List[str]:
    warnings: List[str] = []
    if metrics.get("history_days", 0) < 80:
        warnings.append("历史行情样本不足80个交易日，量化评分稳定性较低。")
    if metrics.get("pe_simple") is None:
        warnings.append("缺少估值指标，估值因子置信度下降。")
    if metrics.get("operating_cash_flow") is not None and metrics["operating_cash_flow"] < 0:
        warnings.append("最新经营现金流为负，需重点核查回款和利润质量。")
    if metrics.get("debt_ratio") is not None and metrics["debt_ratio"] > 70:
        warnings.append("资产负债率偏高，仓位需要打折。")
    if factors["trend"]["score"] < 40:
        warnings.append("趋势因子偏弱，左侧买入需等待技术确认。")
    return warnings


def format_quant_report(result: QuantAnalysisResult) -> str:
    metrics = result.metrics
    factors = result.factors
    lines = [
        "## 📊 量化评分报告",
        "",
        f"- 股票代码: {result.symbol}" + (f"（{metrics.get('stock_name')}）" if metrics.get("stock_name") else ""),
        f"- 分析日期: {result.analysis_date}",
        f"- 模型版本: {result.model_name}",
        f"- 数据来源: {result.data_source}",
        f"- 综合评分: **{result.score:.1f}/100**（{result.rating}）",
        f"- 量化信号: **{result.signal}**",
        f"- 风险等级: **{result.risk_level}**",
        f"- 建议仓位约束: {result.suggested_position}",
        "",
        "### 因子拆解",
        "因子 | 分数 | 等级 | 说明",
        "--- | ---: | --- | ---",
    ]
    for key in ["momentum", "trend", "risk", "valuation", "quality", "liquidity"]:
        factor = factors[key]
        lines.append(f"{factor['label']} | {factor['score']:.1f} | {factor['grade']} | {factor['explanation']}")

    lines.extend(
        [
            "",
            "### 关键指标",
            f"- 最新收盘价: {_fmt(metrics.get('latest_close'))} 元（{metrics.get('latest_date', 'N/A')}）",
            f"- 5/20/60日收益: {_fmt_pct(metrics.get('return_5d'))} / {_fmt_pct(metrics.get('return_20d'))} / {_fmt_pct(metrics.get('return_60d'))}",
            f"- MA5/MA20/MA60: {_fmt(metrics.get('ma5'))} / {_fmt(metrics.get('ma20'))} / {_fmt(metrics.get('ma60'))}",
            f"- RSI14: {_fmt(metrics.get('rsi14'))}，20日年化波动: {_fmt_pct(metrics.get('volatility_20d_annualized'))}，60日最大回撤: {_fmt_pct(metrics.get('max_drawdown_60d'))}",
            f"- 简单PE/PB/PS: {_fmt(metrics.get('pe_simple'))}x / {_fmt(metrics.get('pb_simple'))}x / {_fmt(metrics.get('ps_simple'))}x",
            f"- 收入同比/利润同比/ROE: {_fmt_pct(metrics.get('revenue_yoy'))} / {_fmt_pct(metrics.get('profit_yoy'))} / {_fmt_pct(metrics.get('roe'))}",
        ]
    )

    if result.warnings:
        lines.extend(["", "### 风险提示"])
        lines.extend(f"- {warning}" for warning in result.warnings)

    lines.extend(
        [
            "",
            "### 使用说明",
            "- 当前为离线基线因子模型，用于约束LLM投研观点，不构成自动交易指令。",
            "- 后续可用同一接口替换为LightGBM/CatBoost多因子排序模型，并接入滚动回测胜率。",
        ]
    )
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    try:
        if pd.isna(value):
            return "N/A"
    except Exception:
        pass
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        if pd.isna(value):
            return "N/A"
        return f"{float(value):+.2f}%"
    except Exception:
        return str(value)
