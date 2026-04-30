"""Post-run quality gates and repair helpers for user-visible stock reports."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.dataflows.fundamentals_quality import ensure_fundamentals_quality
from tradingagents.llm_clients.openai_client import OpenAIClient
from tradingagents.tools.unified_news_tool import UnifiedNewsAnalyzer


class ReportQualityError(RuntimeError):
    pass


def _make_llm():
    provider = os.getenv("VALUE_LAYER_PROVIDER") or os.getenv("LLM_PROVIDER", "custom_openai")
    model = os.getenv("VALUE_LAYER_MODEL") or os.getenv("QUICK_THINK_LLM") or os.getenv("DEEP_THINK_LLM") or "gpt-5.5"
    base_url = os.getenv("VALUE_LAYER_BASE_URL") or os.getenv("CUSTOM_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""
    api_key = os.getenv("CUSTOM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    kwargs: Dict[str, Any] = {
        "temperature": float(os.getenv("REPORT_REPAIR_TEMPERATURE", "0.2")),
        "max_tokens": int(os.getenv("REPORT_REPAIR_MAX_TOKENS", "7000")),
        "timeout": int(os.getenv("REPORT_REPAIR_TIMEOUT", "180")),
        "max_retries": 1,
    }
    if api_key:
        kwargs["api_key"] = api_key
    return OpenAIClient(model=model, base_url=base_url or None, provider=provider, **kwargs).get_llm()


def _invoke_text(llm, system: str, user: str) -> str:
    response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "\n".join(str(item) for item in content)
    return str(content or "").strip()


def _extract_current_price(text: str) -> float | None:
    for pattern in (
        r"(?:当前行情价格|当前价格|当前价|最新收盘价|收盘价)[^\n\d]{0,40}([0-9]+(?:\.[0-9]+)?)",
    ):
        match = __import__("re").search(pattern, str(text or ""))
        if match:
            try:
                return float(match.group(1))
            except Exception:
                return None
    return None


def _ensure_price_guard(report: str, source_text: str) -> None:
    price = _extract_current_price(source_text)
    if price is None:
        raise ReportQualityError("基本面修复缺少行情当前价格，禁止生成价位结论。")
    forbidden = ["隐含当前价格", "隐含价格", "反推", "PE×EPS", "PE × EPS", "EPS×PE", "EPS × PE"]
    hits = [item for item in forbidden if item in report]
    if hits:
        raise ReportQualityError("基本面分析包含禁止的当前价反推表述: " + ", ".join(hits))
    if price < 20:
        return
    import re

    suspicious = []
    price_keywords = "当前价|当前价格|参考价|目标价|合理价|合理价值|风险回落|回落区间|买入区|低吸区|支撑|压力|止损|减仓|上涨空间|下跌空间"
    for line in str(report or "").splitlines():
        if not re.search(price_keywords, line):
            continue
        for match in re.finditer(r"(?<![\d.])([0-9]+(?:\.[0-9]+)?)\s*元(?:/股)?", line):
            try:
                value = float(match.group(1))
            except Exception:
                continue
            if 0 < value < price * 0.35:
                suspicious.append(value)
    if suspicious:
        raise ReportQualityError(f"基本面分析出现脱离当前价{price:.2f}元的价格 {suspicious[:8]}，疑似口径错误。")


def ensure_report_price_guard(report: str, source_text: str, *, label: str = "report") -> None:
    try:
        _ensure_price_guard(report, source_text)
    except ReportQualityError as exc:
        raise ReportQualityError(f"{label} 价格质量门禁未通过: {exc}") from exc


def _looks_like_snapshot_only(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) < 1800:
        return True
    analysis_markers = ["估值", "盈利能力", "现金流", "资产质量", "投资建议", "风险", "结论"]
    return sum(1 for marker in analysis_markers if marker in stripped) < 4


def validate_analysis_sections(state: Dict[str, Any], *, require_news: bool = True) -> Dict[str, Any]:
    news_report = str(state.get("news_report") or "").strip()
    fundamentals_report = str(state.get("fundamentals_report") or "").strip()
    issues = []
    if require_news and len(news_report) < int(os.getenv("MIN_NEWS_REPORT_CHARS", "800")):
        issues.append("news_report_empty_or_too_short")
    if len(fundamentals_report) < int(os.getenv("MIN_FUNDAMENTALS_ANALYSIS_CHARS", "2500")):
        issues.append("fundamentals_report_too_short")
    if _looks_like_snapshot_only(fundamentals_report):
        issues.append("fundamentals_snapshot_without_analysis")
    return {
        "ok": not issues,
        "issues": issues,
        "news_report_length": len(news_report),
        "fundamentals_report_length": len(fundamentals_report),
    }


def repair_news_report(symbol: str, stock_name: str, analysis_date: str, toolkit: Any, llm: Any | None = None) -> str:
    analyzer = UnifiedNewsAnalyzer(toolkit)
    news_data = analyzer.get_stock_news_unified(symbol, max_news=int(os.getenv("REPORT_REPAIR_MAX_NEWS", "10")), model_info="post_run_repair")
    if not news_data or len(news_data.strip()) < 100:
        raise ReportQualityError(f"新闻数据获取失败或过短，不能生成新闻分析: {symbol}")
    llm = llm or _make_llm()
    system = "你是专业财经新闻分析师。必须只基于用户提供的新闻数据分析，不得编造新闻。输出中文Markdown。"
    user = f"""请基于以下真实新闻数据，为 {stock_name}（{symbol}）生成完整新闻分析报告。\n\n分析日期：{analysis_date}\n\n必须包含：\n1. 新闻数据来源与时效性说明\n2. 关键新闻事件列表\n3. 对股价短期情绪影响\n4. 对基本面/行业逻辑影响\n5. 风险事件与需跟踪事项\n6. 新闻面结论：正面/中性/负面，并说明理由\n\n真实新闻数据：\n{news_data}\n"""
    report = _invoke_text(llm, system, user)
    if len(report) < 800:
        raise ReportQualityError(f"新闻分析生成过短，不能通过质量门禁: {len(report)} 字符")
    return report


def repair_fundamentals_analysis(symbol: str, stock_name: str, analysis_date: str, fundamentals_text: str, llm: Any | None = None) -> Tuple[str, Dict[str, Any]]:
    repaired_text, quality = ensure_fundamentals_quality(symbol, fundamentals_text)
    llm = llm or _make_llm()
    system = "你是专业股票基本面分析师。必须严格基于用户提供的财务数据和三表摘要分析，不得编造未给出的数据。输出中文Markdown。"
    user = f"""请基于以下已经通过质量门禁的真实基本面数据，为 {stock_name}（{symbol}）生成完整基本面分析报告。\n\n分析日期：{analysis_date}\n\n必须包含：\n1. 公司与数据源说明\n2. 核心财务指标表：PE、PB、ROE、资产负债率、毛利率、净利率、营收、归母净利润、经营现金流、EPS\n3. 利润表摘要分析\n4. 资产负债表摘要分析\n5. 现金流量表摘要分析\n6. 盈利能力、资产质量、现金流质量分析\n7. PE/PB/成长性估值解读；若PEG无法计算，说明原因但不能说核心数据缺失\n8. 当前价格是否低估/合理/偏高\n9. 合理价位区间、目标价与风险回落区间\n10. 投资建议和不买/减仓条件\n\n禁止使用“工具未返回”“数据不足，不能确认”“无法给出真实PE/PB”等说法，因为下列数据已经通过质量门禁。\n\n真实基本面数据：\n{repaired_text}\n"""
    report = _invoke_text(llm, system, user)
    if len(report) < 2500:
        raise ReportQualityError(f"基本面分析生成过短，不能通过质量门禁: {len(report)} 字符")
    bad_phrases = ["工具未返回", "数据不足，不能确认", "无法给出真实PE", "无法给出真实PB", "完整财务指标未能充分返回"]
    found = [phrase for phrase in bad_phrases if phrase in report]
    if found:
        raise ReportQualityError("基本面分析包含禁用缺失表述: " + ", ".join(found))
    _ensure_price_guard(report, repaired_text)
    _, report_quality = ensure_fundamentals_quality(symbol, report)
    return report, {"input_quality": quality, "report_quality": report_quality}


def ensure_analysis_sections(symbol: str, stock_name: str, analysis_date: str, state: Dict[str, Any], toolkit: Any, *, require_news: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state = dict(state)
    before = validate_analysis_sections(state, require_news=require_news)
    repairs: Dict[str, Any] = {"before": before, "repaired": []}
    if before["ok"]:
        repairs["after"] = before
        return state, repairs

    llm = _make_llm()
    if "news_report_empty_or_too_short" in before["issues"]:
        state["news_report"] = repair_news_report(symbol, stock_name, analysis_date, toolkit, llm)
        repairs["repaired"].append("news_report")

    if any(issue in before["issues"] for issue in ("fundamentals_report_too_short", "fundamentals_snapshot_without_analysis")):
        report, quality = repair_fundamentals_analysis(symbol, stock_name, analysis_date, str(state.get("fundamentals_report") or ""), llm)
        state["fundamentals_report"] = report
        repairs["fundamentals_repair_quality"] = quality
        repairs["repaired"].append("fundamentals_report")

    after = validate_analysis_sections(state, require_news=require_news)
    repairs["after"] = after
    repairs["repaired_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not after["ok"]:
        raise ReportQualityError("新闻/基本面分析质量门禁未通过: " + str(after))
    return state, repairs
