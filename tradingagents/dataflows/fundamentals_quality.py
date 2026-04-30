"""Quality gates and repair helpers for A-share fundamentals reports."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


class FundamentalsQualityError(RuntimeError):
    """Raised when strict fundamentals quality requirements are not met."""

    def __init__(self, message: str, quality: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.quality = quality or {}


@dataclass(frozen=True)
class MetricRule:
    key: str
    label: str
    patterns: Tuple[str, ...]


FATAL_MISSING_PHRASES = (
    "工具未返回",
    "未返回明确行业",
    "完整财务报表\t工具未返回",
    "未提供完整的利润表",
    "未提供完整的资产负债表",
    "未提供完整的现金流量表",
    "无法给出真实PE",
    "无法给出真实PB",
    "无法给出真实PEG",
    "无法直接计算真实PE",
    "无法直接计算真实PB",
    "无法直接计算真实PEG",
    "数据不足，不能确认",
    "缺少完整财务数据",
    "缺少PE、PB、PEG",
    "缺少估值指标",
    "PE/PB/PS: N/A",
    "简单PE/PB/PS: N/A",
    "N/Ax / N/Ax",
)


METRIC_RULES: Tuple[MetricRule, ...] = (
    MetricRule(
        "pe",
        "PE/市盈率",
        (
            r"(?:简单PE|PE\s*\(?市盈率\)?|市盈率)[^\n\d负亏\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:倍|x)?",
        ),
    ),
    MetricRule(
        "pb",
        "PB/市净率",
        (
            r"(?:简单PB|PB\s*\(?市净率\)?|市净率)[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:倍|x)?",
        ),
    ),
    MetricRule(
        "roe",
        "ROE/净资产收益率",
        (
            r"(?:ROE|净资产收益率)[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*%",
        ),
    ),
    MetricRule(
        "asset_liability_ratio",
        "资产负债率",
        (
            r"资产负债率[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*%",
        ),
    ),
    MetricRule(
        "gross_margin",
        "毛利率",
        (
            r"毛利率[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*%",
        ),
    ),
    MetricRule(
        "net_margin",
        "净利率",
        (
            r"净利率[^\n\d\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*%",
        ),
    ),
    MetricRule(
        "revenue",
        "营业收入",
        (
            r"营业(?:总)?收入[^\n]*(?:\d+(?:\.\d+)?)(?:万|亿|元)",
        ),
    ),
    MetricRule(
        "net_profit",
        "净利润",
        (
            r"(?:归母净利润|净利润)[^\n]*(?:\d+(?:\.\d+)?)(?:万|亿|元)",
        ),
    ),
    MetricRule(
        "operating_cash_flow",
        "经营现金流",
        (
            r"经营现金流[^\n]*(?:\d+(?:\.\d+)?)(?:万|亿|元)",
            r"经营现金流净额[^\n]*(?:\d+(?:\.\d+)?)(?:万|亿|元)",
        ),
    ),
    MetricRule(
        "current_price",
        "当前价格",
        (
            r"(?:当前行情价格|当前价格|当前价|最新收盘价|收盘价)[^\n\d]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:元|¥|人民币)?",
        ),
    ),
)


DEFAULT_REQUIRED_METRICS = "current_price,pe,pb,roe,asset_liability_ratio,gross_margin,net_margin"
REQUIRED_STATEMENT_SECTIONS = ("资产负债表摘要", "利润表摘要", "现金流量表摘要")


def _truthy_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _required_metrics() -> List[str]:
    raw = os.getenv("FUNDAMENTALS_MIN_REQUIRED", DEFAULT_REQUIRED_METRICS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def is_a_share_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", str(symbol or "").strip()))


def _metric_rules_by_key() -> Dict[str, MetricRule]:
    return {rule.key: rule for rule in METRIC_RULES}


def _first_match(text: str, patterns: Iterable[str]) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


def _detect_metrics(text: str) -> Dict[str, str]:
    detected: Dict[str, str] = {}
    for rule in METRIC_RULES:
        value = _first_match(text, rule.patterns)
        if value is not None:
            detected[rule.key] = value
    return detected


def validate_fundamentals_quality(text: str, *, strict: Optional[bool] = None) -> Dict[str, Any]:
    """Return a deterministic quality report for fundamentals text."""

    strict = _truthy_env("FUNDAMENTALS_STRICT", True) if strict is None else strict
    text = str(text or "")
    required = _required_metrics()
    rules = _metric_rules_by_key()
    detected = _detect_metrics(text)
    missing_metrics = [key for key in required if key not in detected]
    fatal_phrases = [phrase for phrase in FATAL_MISSING_PHRASES if phrase in text]

    missing_sections: List[str] = []
    if _truthy_env("FUNDAMENTALS_REQUIRE_STATEMENTS", False):
        missing_sections = [section for section in REQUIRED_STATEMENT_SECTIONS if section not in text]

    ok = bool(text.strip()) and not missing_metrics and not fatal_phrases and not missing_sections
    labels = {key: rules[key].label for key in missing_metrics if key in rules}
    return {
        "ok": ok,
        "strict": strict,
        "required_metrics": required,
        "detected_metrics": detected,
        "missing_metrics": missing_metrics,
        "missing_metric_labels": labels,
        "fatal_phrases": fatal_phrases,
        "missing_sections": missing_sections,
        "text_length": len(text),
    }


def _build_repaired_report(symbol: str, supplement: str, source: str, previous_quality: Dict[str, Any]) -> str:
    missing_metrics = previous_quality.get("missing_metrics") or []
    missing_sections = previous_quality.get("missing_sections") or []
    fatal_phrases = previous_quality.get("fatal_phrases") or []
    reason_parts = []
    if missing_metrics:
        reason_parts.append("缺失核心指标: " + ", ".join(missing_metrics))
    if missing_sections:
        reason_parts.append("缺失报表摘要: " + ", ".join(missing_sections))
    if fatal_phrases:
        reason_parts.append("包含缺失数据表述: " + ", ".join(fatal_phrases))
    reason = "；".join(reason_parts) or "原始段未通过完整性门禁"
    return "\n".join(
        [
            f"# {symbol} 基本面补充数据（质量门禁重取）",
            "",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"补充数据源: {source}",
            f"重取原因: {reason}",
            "说明: 这表示原始基本面段不满足本系统的完整性要求；不等于所有上游行情源永久不可用。系统按严格门禁使用下方补齐后的财务指标和三表摘要作为后续 Agent 输入。",
            "",
            supplement.strip(),
        ]
    ).strip() + "\n"


def _fetch_current_price(symbol: str) -> Tuple[Optional[float], str]:
    errors: List[str] = []
    try:
        from tradingagents.dataflows.providers.china.integrated import get_integrated_china_provider

        info = get_integrated_china_provider().get_stock_info(symbol)
        price = info.get("current_price") if isinstance(info, dict) else None
        if price is not None:
            return float(price), f"integrated/{info.get('quote_source') or info.get('source') or 'quote'}"
    except Exception as exc:
        errors.append(f"integrated_quote失败: {exc}")

    try:
        from tradingagents.dataflows.interface import get_china_stock_data_unified

        today = datetime.now().strftime("%Y-%m-%d")
        text = get_china_stock_data_unified(symbol, today, today)
        match = re.search(r"当前价格[:：]\s*([0-9]+(?:\.[0-9]+)?)", text or "")
        if match:
            return float(match.group(1)), "china_stock_data_unified"
    except Exception as exc:
        errors.append(f"china_stock_data_unified失败: {exc}")

    raise FundamentalsQualityError("无法从行情源获取当前价格，禁止用估值公式反推当前价。" + " | ".join(errors[-3:]))


def _format_akshare_snapshot(symbol: str, metrics: Dict[str, Any], source_notes: List[str]) -> str:
    lines = [
        f"# 股票{symbol} A股基本面财务数据",
        "数据来源: AKShare公开财务与估值接口（补充数据源）",
        "",
        "## 核心结论数据",
    ]
    label_map = {
        "current_price": "当前价格",
        "pe": "PE/市盈率",
        "pb": "PB/市净率",
        "roe": "ROE",
        "asset_liability_ratio": "资产负债率",
        "gross_margin": "毛利率",
        "net_margin": "净利率",
        "revenue_yoy": "营业收入同比",
        "profit_yoy": "净利润同比",
        "revenue": "营业总收入",
        "net_profit": "归母净利润",
        "operating_cash_flow": "经营现金流净额",
        "eps": "每股收益EPS",
    }
    suffix_map = {
        "current_price": "元",
        "pe": "倍",
        "pb": "倍",
        "roe": "%",
        "asset_liability_ratio": "%",
        "gross_margin": "%",
        "net_margin": "%",
        "revenue_yoy": "%",
        "profit_yoy": "%",
        "revenue": "元",
        "net_profit": "元",
        "operating_cash_flow": "元",
        "eps": "元",
    }
    for key, label in label_map.items():
        value = metrics.get(key)
        if value is not None:
            suffix = suffix_map.get(key, "")
            lines.append(f"- {label}: {value:.2f}{suffix}")
    report_period = metrics.get("report_period")
    if report_period:
        lines.append(f"- 最新报告期: {report_period}")
    statements = metrics.get("statements") or {}
    if statements.get("balance_sheet"):
        lines.extend(["", "## 资产负债表摘要", statements["balance_sheet"]])
    if statements.get("income_statement"):
        lines.extend(["", "## 利润表摘要", statements["income_statement"]])
    if statements.get("cash_flow"):
        lines.extend(["", "## 现金流量表摘要", statements["cash_flow"]])
    lines.extend(["", "## 数据源记录"])
    lines.extend(f"- {note}" for note in source_notes)
    return "\n".join(lines).strip() + "\n"


def _lookup_abstract_metric(df: Any, names: Iterable[str]) -> Optional[float]:
    if df is None or getattr(df, "empty", True) or "指标" not in df.columns:
        return None
    date_columns = [column for column in df.columns if re.fullmatch(r"\d{8}", str(column))]
    if not date_columns:
        return None
    latest_date = sorted(date_columns)[-1]
    for name in names:
        rows = df[df["指标"].astype(str).str.fullmatch(name, na=False)]
        if rows.empty:
            rows = df[df["指标"].astype(str).str.contains(name, na=False, regex=False)]
        if not rows.empty:
            value = _safe_float(rows.iloc[0].get(latest_date))
            if value is not None:
                return value
    return None


def _latest_abstract_period(df: Any) -> Optional[str]:
    if df is None or getattr(df, "empty", True):
        return None
    date_columns = [str(column) for column in df.columns if re.fullmatch(r"\d{8}", str(column))]
    if not date_columns:
        return None
    latest = sorted(date_columns)[-1]
    return f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"


def _format_money_value(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "N/A"
    abs_number = abs(number)
    if abs_number >= 100000000:
        return f"{number / 100000000:.2f}亿元"
    if abs_number >= 10000:
        return f"{number / 10000:.2f}万元"
    return f"{number:.2f}元"


def _em_prefixed_symbol(symbol: str) -> str:
    return ("SH" if str(symbol).startswith("6") else "SZ") + str(symbol)


def _latest_statement_row(df: Any) -> Any:
    if df is None or getattr(df, "empty", True):
        return None
    frame = df.copy()
    if "REPORT_DATE" in frame.columns:
        frame = frame.sort_values("REPORT_DATE")
    return frame.iloc[-1]


def _format_statement_row(row: Any, fields: Iterable[Tuple[str, str]]) -> str:
    if row is None:
        return ""
    report_date = str(row.get("REPORT_DATE") or row.get("REPORT_DATE_NAME") or "最新报告期")[:10]
    lines = [f"- 报告期: {report_date}"]
    for field, label in fields:
        if field in row:
            lines.append(f"- {label}: {_format_money_value(row.get(field))}")
    return "\n".join(lines)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        text = str(value).replace(",", "").replace("%", "").strip()
        if not text or text.lower() in {"nan", "none", "n/a", "--"}:
            return None
        return float(text)
    except Exception:
        return None


def _pick_column(row: Any, names: Iterable[str]) -> Optional[float]:
    for name in names:
        if name in row:
            value = _safe_float(row.get(name))
            if value is not None:
                return value
    return None


def _latest_row(df: Any, date_columns: Iterable[str]) -> Any:
    if df is None or getattr(df, "empty", True):
        return None
    frame = df.copy()
    for column in date_columns:
        if column in frame.columns:
            frame = frame.sort_values(column)
            break
    return frame.iloc[-1]


def _fetch_akshare_snapshot(symbol: str) -> Tuple[str, str]:
    import akshare as ak

    metrics: Dict[str, Any] = {}
    source_notes: List[str] = []
    em_symbol = _em_prefixed_symbol(symbol)

    try:
        current_price, price_source = _fetch_current_price(symbol)
        metrics["current_price"] = current_price
        source_notes.append(f"{price_source}: 当前价格")
    except Exception as exc:
        source_notes.append(f"当前价格获取失败: {exc}")

    try:
        value_df = ak.stock_value_em(symbol=symbol)
        row = _latest_row(value_df, ("数据日期", "trade_date", "日期", "date"))
        if row is not None:
            metrics["pe"] = _pick_column(row, ("PE(TTM)", "PE(静)", "pe_ttm", "pe", "市盈率"))
            metrics["pb"] = _pick_column(row, ("市净率", "pb"))
            source_notes.append("stock_value_em: PE/PB")
    except Exception as exc:
        source_notes.append(f"stock_value_em失败: {exc}")

    try:
        indicator = ak.stock_financial_analysis_indicator(symbol=symbol)
        row = _latest_row(indicator, ("日期", "报告期", "REPORT_DATE"))
        if row is not None:
            metrics["roe"] = _pick_column(row, ("净资产收益率(%)", "净资产收益率", "ROE"))
            metrics["asset_liability_ratio"] = _pick_column(row, ("资产负债率(%)", "资产负债率"))
            metrics["gross_margin"] = _pick_column(row, ("销售毛利率(%)", "销售毛利率", "毛利率"))
            metrics["net_margin"] = _pick_column(row, ("销售净利率(%)", "销售净利率", "净利率"))
            metrics["revenue_yoy"] = _pick_column(row, ("主营业务收入增长率(%)", "营业收入增长率(%)"))
            metrics["profit_yoy"] = _pick_column(row, ("净利润增长率(%)", "净利润增长率"))
            for column in ("日期", "报告期", "REPORT_DATE"):
                if column in row:
                    metrics["report_period"] = str(row.get(column))
                    break
            source_notes.append("stock_financial_analysis_indicator: ROE/利润率/负债率")
    except Exception as exc:
        source_notes.append(f"stock_financial_analysis_indicator失败: {exc}")

    try:
        abstract = ak.stock_financial_abstract(symbol=symbol)
        metric_map = {
            "roe": ("净资产收益率(ROE)", "净资产收益率"),
            "asset_liability_ratio": ("资产负债率",),
            "gross_margin": ("毛利率", "销售毛利率"),
            "net_margin": ("销售净利率", "净利率"),
            "revenue": ("营业总收入",),
            "net_profit": ("归母净利润", "净利润"),
            "operating_cash_flow": ("经营现金流量净额", "经营现金流净额"),
            "eps": ("基本每股收益",),
        }
        for key, names in metric_map.items():
            if metrics.get(key) is None:
                metrics[key] = _lookup_abstract_metric(abstract, names)
        metrics.setdefault("report_period", _latest_abstract_period(abstract))
        source_notes.append("stock_financial_abstract: ROE/利润率/负债率/收入/利润/现金流")
    except Exception as exc:
        source_notes.append(f"stock_financial_abstract失败: {exc}")

    statements: Dict[str, str] = {}
    try:
        balance = ak.stock_balance_sheet_by_report_em(symbol=em_symbol)
        section = _format_statement_row(
            _latest_statement_row(balance),
            (
                ("TOTAL_ASSETS", "总资产"),
                ("TOTAL_LIABILITIES", "总负债"),
                ("TOTAL_EQUITY", "股东权益"),
                ("MONETARYFUNDS", "货币资金"),
                ("ACCOUNTS_RECE", "应收账款"),
                ("INVENTORY", "存货"),
                ("GOODWILL", "商誉"),
            ),
        )
        if section:
            statements["balance_sheet"] = section
            source_notes.append("stock_balance_sheet_by_report_em: 资产负债表")
    except Exception as exc:
        source_notes.append(f"stock_balance_sheet_by_report_em失败: {exc}")

    try:
        income = ak.stock_profit_sheet_by_report_em(symbol=em_symbol)
        section = _format_statement_row(
            _latest_statement_row(income),
            (
                ("TOTAL_OPERATE_INCOME", "营业总收入"),
                ("TOTAL_OPERATE_COST", "营业总成本"),
                ("OPERATE_PROFIT", "营业利润"),
                ("TOTAL_PROFIT", "利润总额"),
                ("PARENT_NETPROFIT", "归母净利润"),
            ),
        )
        if section:
            statements["income_statement"] = section
            source_notes.append("stock_profit_sheet_by_report_em: 利润表")
    except Exception as exc:
        source_notes.append(f"stock_profit_sheet_by_report_em失败: {exc}")

    try:
        cash = ak.stock_cash_flow_sheet_by_report_em(symbol=em_symbol)
        section = _format_statement_row(
            _latest_statement_row(cash),
            (
                ("TOTAL_OPERATE_INFLOW", "经营流入"),
                ("TOTAL_OPERATE_OUTFLOW", "经营流出"),
                ("NETCASH_OPERATE", "经营现金流净额"),
                ("NETCASH_INVEST", "投资现金流净额"),
                ("NETCASH_FINANCE", "筹资现金流净额"),
            ),
        )
        if section:
            statements["cash_flow"] = section
            source_notes.append("stock_cash_flow_sheet_by_report_em: 现金流量表")
    except Exception as exc:
        source_notes.append(f"stock_cash_flow_sheet_by_report_em失败: {exc}")

    metrics["statements"] = statements

    report = _format_akshare_snapshot(symbol, metrics, source_notes)
    return report, "AKShare"


def fetch_supplemental_fundamentals(symbol: str) -> Tuple[str, str]:
    """Retry fundamentals from live sources, then optional Mongo cache, then AKShare."""

    errors: List[str] = []
    try:
        from tradingagents.dataflows.providers.china.integrated import get_integrated_china_provider

        report = get_integrated_china_provider().get_fundamentals_data(symbol)
        quality = validate_fundamentals_quality(report, strict=True)
        if quality["ok"]:
            try:
                from tradingagents.dataflows.fundamentals_cache import write_cached_fundamentals

                write_cached_fundamentals(symbol, report, "integrated/eastmoney", quality)
            except Exception:
                pass
            return report, "integrated/eastmoney"
        errors.append(f"integrated未达标: {quality}")
    except Exception as exc:
        errors.append(f"integrated失败: {exc}")

    try:
        from tradingagents.dataflows.fundamentals_cache import read_cached_fundamentals

        cached = read_cached_fundamentals(symbol)
        cached_text = str((cached or {}).get("text") or "")
        quality = validate_fundamentals_quality(cached_text, strict=True)
        if cached_text and quality["ok"]:
            return cached_text, "mongo_cache"
        if cached_text:
            errors.append(f"mongo_cache未达标: {quality}")
    except Exception as exc:
        errors.append(f"mongo_cache失败: {exc}")

    try:
        report, source = _fetch_akshare_snapshot(symbol)
        quality = validate_fundamentals_quality(report, strict=True)
        if quality["ok"]:
            try:
                from tradingagents.dataflows.fundamentals_cache import write_cached_fundamentals

                write_cached_fundamentals(symbol, report, source, quality)
            except Exception:
                pass
            return report, source
        errors.append(f"AKShare未达标: {quality}")
    except Exception as exc:
        errors.append(f"AKShare失败: {exc}")

    raise FundamentalsQualityError("财务数据重取后仍未达标: " + " | ".join(errors[-5:]))


def ensure_fundamentals_quality(symbol: str, text: str, *, strict: Optional[bool] = None) -> Tuple[str, Dict[str, Any]]:
    """Validate and, when needed, repair A-share fundamentals text."""

    strict = _truthy_env("FUNDAMENTALS_STRICT", True) if strict is None else strict
    if not is_a_share_symbol(symbol):
        quality = validate_fundamentals_quality(text, strict=False)
        quality["ok"] = True
        quality["skipped_reason"] = "非A股代码，暂不套用A股财务门禁"
        return text, quality

    quality = validate_fundamentals_quality(text, strict=strict)
    if quality["ok"]:
        quality["repaired"] = False
        return text, quality

    supplement, source = fetch_supplemental_fundamentals(symbol)
    repaired = _build_repaired_report(symbol, supplement, source, quality)
    repaired_quality = validate_fundamentals_quality(repaired, strict=strict)
    repaired_quality.update({"repaired": True, "repair_source": source, "previous_quality": quality})
    if repaired_quality["ok"]:
        return repaired, repaired_quality

    if strict:
        raise FundamentalsQualityError(
            "A股基本面数据未达到生成报告门槛，已中止，避免基于缺失财务数据分析。",
            repaired_quality,
        )
    return repaired, repaired_quality
