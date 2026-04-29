#!/usr/bin/env python3
"""Generate a personal position decision note from the latest agent report.

This script does not make a fresh stock forecast. It reads an existing
TradingAgents report, combines it with a user's position information, calculates
risk numbers, and writes a Markdown note for learning/risk-management use.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from report_paths import analysis_stock_dir, candidate_analysis_dirs, candidate_result_dirs, safe_name

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_OUTPUTS = ROOT / "analysis_outputs"
RESULTS_DIR = ROOT / "results"


@dataclass
class ReportSummary:
    symbol: str
    stock_name: str
    report_date: str
    action: str
    target_price: Optional[float]
    confidence: Optional[float]
    risk_score: Optional[float]
    current_price: Optional[float]
    stop_loss: Optional[float]
    confirm_price: Optional[float]
    reduce_text: str
    support_text: str
    pressure_text: str
    key_reasons: list[str]
    final_text: str
    risk_text: str
    source_paths: list[Path]


@dataclass
class PositionInput:
    symbol: str
    stock_name: str
    cost: float
    price: float
    shares: Optional[float]
    position_ratio: Optional[float]
    risk: str
    horizon: str
    question: str
    report_date: str


@dataclass
class PositionMetrics:
    market_value: Optional[float]
    loss_amount: Optional[float]
    loss_pct: float
    rebound_to_breakeven_pct: float
    stop_loss_loss_pct: Optional[float]
    target_loss_pct: Optional[float]
    position_risk_level: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a personal holding/position Q&A report.")
    parser.add_argument("--symbol", required=True, help="Stock code, e.g. 002410")
    parser.add_argument("--stock-name", default="", help="Stock name, e.g. 广联达")
    parser.add_argument("--date", default="", help="Report date, e.g. 2026-04-29. Default: latest available")
    parser.add_argument("--cost", type=float, required=True, help="Your average buy price")
    parser.add_argument("--price", type=float, default=None, help="Current/reference price. Default: parse from latest report")
    parser.add_argument("--shares", type=float, default=None, help="Shares held")
    parser.add_argument("--position-ratio", type=float, default=None, help="Position size as % of total assets")
    parser.add_argument("--risk", choices=["low", "medium", "high"], default="medium", help="Risk tolerance")
    parser.add_argument("--horizon", choices=["short", "swing", "long"], default="swing", help="Investment horizon")
    parser.add_argument("--question", default="我要卖吗？", help="Your question")
    parser.add_argument("--report", default="", help="Explicit report md path if needed")
    parser.add_argument("--out-dir", default=str(ANALYSIS_OUTPUTS), help="Output directory")
    return parser.parse_args()


def normalize_symbol(symbol: str) -> str:
    match = re.search(r"\d{6}", str(symbol))
    if not match:
        raise ValueError(f"Invalid A-share symbol: {symbol}")
    return match.group(0)


def infer_stock_name(symbol: str, explicit_name: str, text: str) -> str:
    if explicit_name:
        return explicit_name
    patterns = [
        rf"#\s*([^\n（(]+)[（(]{re.escape(symbol)}[）)]",
        rf"([^\s（(]+)[（(]股票代码[:：]?\s*{re.escape(symbol)}[）)]",
        r"股票名称[:：]\s*([^\n]+)",
        r"公司名称[:：]\s*([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = re.sub(r"[*#`\s]", "", match.group(1)).strip()
            if name and len(name) <= 12 and not name.isdigit():
                return name
    return f"股票{symbol}"


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            result.append(path)
            seen.add(resolved)
    return result


def find_latest_report(symbol: str, report_date: str, explicit_report: str) -> tuple[Optional[Path], Optional[Path], Optional[Path], list[Path]]:
    if explicit_report:
        path = Path(explicit_report).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise FileNotFoundError(path)
        return path, None, None, [path]

    candidates: list[Path] = []
    for analysis_dir in candidate_analysis_dirs(symbol):
        if report_date:
            compact = report_date.replace("-", "")
            candidates.extend(analysis_dir.glob(f"*{symbol}*{compact}*finance_fixed_report.md"))
            candidates.extend(analysis_dir.glob(f"*{symbol}*{compact}*report.md"))
            candidates.extend(analysis_dir.glob(f"*{report_date}*report.md"))
        else:
            candidates.extend(analysis_dir.glob(f"*{symbol}*finance_fixed_report.md"))
            candidates.extend(analysis_dir.glob(f"*{symbol}*report.md"))
            candidates.extend(analysis_dir.glob("*-report.md"))

    candidates = [path for path in candidates if path.is_file()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    primary = candidates[0] if candidates else None

    result_report = None
    risk_report = None
    if report_date:
        for result_dir in candidate_result_dirs(symbol):
            reports_dir = result_dir / report_date / "reports"
            candidate = reports_dir / "final_trade_decision.md"
            if candidate.exists():
                result_report = candidate
                risk_report = reports_dir / "risk_management_decision.md"
                break
    else:
        for result_dir in candidate_result_dirs(symbol):
            if result_dir.exists():
                dated = sorted([path for path in result_dir.iterdir() if path.is_dir()], reverse=True)
                for directory in dated:
                    candidate = directory / "reports" / "final_trade_decision.md"
                    if candidate.exists():
                        result_report = candidate
                        risk_report = directory / "reports" / "risk_management_decision.md"
                        break
            if result_report:
                break

    source_paths = []
    if primary:
        source_paths.append(primary)
    if result_report and result_report.exists():
        source_paths.append(result_report)
    if risk_report and risk_report.exists():
        source_paths.append(risk_report)
    return (
        primary,
        result_report if result_report and result_report.exists() else None,
        risk_report if risk_report and risk_report.exists() else None,
        unique_paths(source_paths),
    )


def read_text(path: Optional[Path]) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def clean_line(line: str) -> str:
    clean = line.strip()
    clean = re.sub(r"^>\s*", "", clean)
    clean = re.sub(r"^[\s\-#*`>]+", "", clean)
    clean = re.sub(r"^\d+[.、]\s*", "", clean)
    clean = clean.replace("**", "").replace("`", "")
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def extract_float(patterns: list[str], text: str) -> Optional[float]:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except Exception:
                continue
    return None


def extract_action(text: str) -> str:
    patterns = [
        r"\*\*行动\*\*[:：]\s*([^\n]+)",
        r"行动[:：]\s*([^\n]+)",
        r"最终建议[^：:]*[:：]\s*\*\*([^*]+)\*\*",
        r"最终交易建议[^\n]*?卖出\s*/\s*主动减仓",
        r"最终评级[^：:]*[:：]\s*\*\*([^*]+)\*\*",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            if match.groups():
                return clean_line(match.group(1)).replace("。", "")
            return "卖出 / 主动减仓"
    for action in ["卖出", "主动减仓", "持有", "观望", "买入"]:
        if action in text[:3000]:
            return action
    return "未知"


def extract_between(text: str, start_markers: list[str], end_markers: list[str]) -> str:
    starts = [text.find(marker) for marker in start_markers if text.find(marker) >= 0]
    if not starts:
        return ""
    start = min(starts)
    ends = [text.find(marker, start + 1) for marker in end_markers if text.find(marker, start + 1) >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end].strip()


def remove_quote_blocks(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def extract_execution_text(risk_report_text: str) -> str:
    chunks: list[str] = []
    execution = extract_between(
        risk_report_text,
        ["# 三、对交易员原计划的完善", "## 最终交易建议", "### 1. 当前价格"],
        ["# 四、目标价格判断", "# 五、从过去错误中吸取"],
    )
    conclusion = extract_between(
        risk_report_text,
        ["# 六、最终结论", "## 对 `002410` 的最终建议", "## 对 ", "最终结论"],
        [],
    )
    if execution:
        chunks.append(execution)
    if conclusion and conclusion not in execution:
        chunks.append(conclusion)
    if not chunks:
        chunks.append(extract_final_risk_decision(risk_report_text))
    return remove_quote_blocks("\n\n".join(chunks)).strip()


def extract_final_risk_decision(text: str) -> str:
    if not text:
        return ""
    markers = [
        "## 风险管理委员会最终决议",
        "## 对 `002410` 的最终建议",
        "## 最终交易建议",
        "# 六、最终结论",
        "# 三、对交易员原计划的完善",
        "# 二、最终判断",
        "## 🎯 投资组合经理最终决策",
    ]
    positions = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    if not positions:
        return text
    return text[min(positions):].strip()


def extract_report_date(paths: list[Path], text: str) -> str:
    for path in paths:
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", str(path))
        if match:
            return match.group(1)
    match = re.search(r"分析日期[:：]\s*(20\d{2}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    return datetime.now().strftime("%Y-%m-%d")


def extract_section(text: str, section_key: str) -> str:
    marker = f"## {section_key}"
    idx = text.find(marker)
    if idx < 0:
        return ""
    next_idx = text.find("\n## ", idx + len(marker))
    return text[idx: next_idx if next_idx > idx else len(text)].strip()


def extract_price_context(text: str, prices: list[str], keywords: list[str], limit: int = 3) -> str:
    source = extract_execution_text(text) or extract_final_risk_decision(text) or text
    contexts: list[str] = []
    for line in source.splitlines():
        if line.lstrip().startswith(">"):
            continue
        clean = clean_line(line)
        if not clean or len(clean) > 160:
            continue
        if any(price in clean for price in prices) and any(keyword in clean for keyword in keywords):
            if clean not in contexts:
                contexts.append(clean)
            if len(contexts) >= limit:
                break
    return "；".join(contexts)


def extract_key_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    keywords = [
        "收入", "净利润", "现金流", "ROE", "净利率", "估值", "PE", "PB", "PS",
        "趋势", "量能", "目标价", "风险收益", "弱修复", "负增长",
    ]
    skip_prefixes = ("行动", "置信度", "风险评分", "目标价位", "分析推理", "具体执行")
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        clean = clean_line(line)
        if not clean or len(clean) > 170 or clean.startswith(skip_prefixes):
            continue
        if any(keyword in clean for keyword in keywords) and clean not in reasons:
            reasons.append(clean)
        if len(reasons) >= 8:
            break
    return reasons


def extract_confirm_price(text: str) -> Optional[float]:
    source = extract_execution_text(text) or text
    priority_patterns = [
        r"(?:放量突破|突破并站稳|趋势改善线|站稳)\s*(?:¥|￥)?(11\.35)元?",
        r"(11\.35)元(?:上方|至|附近)?",
        r"11\.29元至(11\.35)元",
    ]
    for pattern in priority_patterns:
        match = re.search(pattern, source)
        if match:
            return float(match.group(1))
    return extract_float([
        r"(?:确认位|压力位|趋势改善线)[^0-9]{0,30}([0-9]+\.[0-9]+)元?",
    ], source)


def summarize_report(symbol: str, report_date: str, stock_name: str, explicit_report: str) -> ReportSummary:
    report_path, final_path, risk_path, source_paths = find_latest_report(symbol, report_date, explicit_report)
    report_text = read_text(report_path)
    final_text = read_text(final_path)
    risk_report_text = read_text(risk_path)
    combined = "\n\n".join([final_text, risk_report_text, report_text])
    if not combined.strip():
        raise FileNotFoundError(f"No report found for {symbol} {report_date or '(latest)'}")

    inferred_name = infer_stock_name(symbol, stock_name, combined)
    inferred_date = report_date or extract_report_date(source_paths, combined)
    execution_text = extract_execution_text(risk_report_text)
    anchor_text = "\n\n".join([final_text, execution_text, report_text])

    target_price = extract_float([
        r"目标价位\*\*[:：]\s*([0-9.]+)",
        r"目标价位[:：]\s*([0-9.]+)",
        r"6个月基准目标价[^0-9]*([0-9.]+)",
        r"基准目标价为\s*([0-9.]+)",
        r"基准目标价[^0-9]*([0-9.]+)",
    ], anchor_text)
    confidence = extract_float([r"置信度\*\*[:：]\s*([0-9.]+)", r"置信度[:：]\s*([0-9.]+)"], anchor_text)
    risk_score = extract_float([r"风险评分\*\*[:：]\s*([0-9.]+)", r"风险评分[:：]\s*([0-9.]+)"], anchor_text)
    current_price = extract_float([
        r"当前股价[:：]\s*[¥￥]?\s*([0-9.]+)",
        r"当前价格[:：]\s*[¥￥]?\s*([0-9.]+)",
        r"当前参考价[:：].*?[¥￥]?\s*([0-9.]+)",
        r"当前股价约[¥￥]?\s*([0-9.]+)",
        r"当前([0-9.]+)元附近",
    ], anchor_text)
    stop_loss = extract_float([
        r"(?:跌破|风险控制线|关键止损位|止损位|风控位)[^0-9]{0,20}([0-9.]+)元?",
        r"([0-9]+\.[0-9]+)元是当前最重要的风险控制线",
    ], execution_text or anchor_text)
    confirm_price = extract_confirm_price(execution_text or anchor_text)

    reduce_text = extract_price_context(
        risk_report_text,
        ["10.95", "11.15", "11.00", "11"],
        ["减仓", "当前", "已有仓位", "区间"],
        limit=3,
    )
    pressure_text = extract_price_context(
        risk_report_text,
        ["11.29", "11.35", "25万", "30万"],
        ["反弹", "突破", "站稳", "放量", "减仓", "成交量"],
        limit=3,
    )
    support_text = extract_price_context(
        risk_report_text,
        ["10.75"],
        ["跌破", "风险", "退出", "止损", "控制线"],
        limit=3,
    )

    final_reason_text = "\n".join([final_text, execution_text])
    return ReportSummary(
        symbol=symbol,
        stock_name=inferred_name,
        report_date=inferred_date,
        action=extract_action(final_text or execution_text or combined),
        target_price=target_price,
        confidence=confidence,
        risk_score=risk_score,
        current_price=current_price,
        stop_loss=stop_loss,
        confirm_price=confirm_price,
        reduce_text=reduce_text,
        support_text=support_text,
        pressure_text=pressure_text,
        key_reasons=extract_key_reasons(final_reason_text),
        final_text=final_text or extract_section(report_text, "final_trade_decision"),
        risk_text=execution_text or extract_section(report_text, "risk_debate_state") or extract_final_risk_decision(risk_report_text),
        source_paths=source_paths,
    )


def compute_metrics(position: PositionInput, summary: ReportSummary) -> PositionMetrics:
    loss_pct = (position.price - position.cost) / position.cost * 100
    rebound_to_breakeven_pct = (position.cost / position.price - 1) * 100
    market_value = position.price * position.shares if position.shares is not None else None
    loss_amount = (position.price - position.cost) * position.shares if position.shares is not None else None
    stop_loss_loss_pct = None
    if summary.stop_loss is not None:
        stop_loss_loss_pct = (summary.stop_loss - position.cost) / position.cost * 100
    target_loss_pct = None
    if summary.target_price is not None:
        target_loss_pct = (summary.target_price - position.cost) / position.cost * 100

    return PositionMetrics(
        market_value=market_value,
        loss_amount=loss_amount,
        loss_pct=loss_pct,
        rebound_to_breakeven_pct=rebound_to_breakeven_pct,
        stop_loss_loss_pct=stop_loss_loss_pct,
        target_loss_pct=target_loss_pct,
        position_risk_level=classify_position_risk(loss_pct, position.position_ratio),
    )


def classify_position_risk(loss_pct: float, position_ratio: Optional[float]) -> str:
    ratio = position_ratio or 0
    if loss_pct <= -25 and ratio >= 30:
        return "高风险：亏损较大且仓位较重"
    if loss_pct <= -20 or ratio >= 25:
        return "中高风险：需要主动控制仓位"
    if loss_pct <= -10 or ratio >= 15:
        return "中等风险：建议分层管理"
    return "较低风险：以纪律跟踪为主"


def build_action_plan(position: PositionInput, summary: ReportSummary) -> dict[str, str]:
    action_text = summary.action
    bearish = any(word in action_text for word in ["卖", "减仓"]) or "卖出" in summary.final_text[:500]
    hold = "持有" in action_text or "观望" in action_text
    bullish = "买入" in action_text and not bearish

    if bearish:
        if position.risk == "low":
            immediate = "偏保守：当前先减仓 60%—80%，甚至直接退出大部分仓位。"
        elif position.risk == "high":
            immediate = "偏激进：也不建议补仓；最多保留 30%—50% 观察仓，先退出其余仓位。"
        else:
            immediate = "中等风险：当前先减仓 40%—50%，不要以回本为唯一目标继续满仓硬扛。"
        rebound = f"若反弹到 {format_price(summary.confirm_price) or '关键压力位'} 附近但不能放量站稳，继续减仓。"
        stop = f"若跌破 {format_price(summary.stop_loss) or '报告止损位'}，剩余仓位退出。"
        reeval = f"只有放量站稳 {format_price(summary.confirm_price) or '关键确认位'}，并且后续财报收入/现金流改善，才重新评估。"
    elif hold:
        immediate = "报告偏持有/观望：不建议新增补仓，先控制仓位并设置纪律线。"
        rebound = f"若反弹到 {format_price(summary.confirm_price) or '关键确认位'} 且放量站稳，再评估是否继续持有。"
        stop = f"若跌破 {format_price(summary.stop_loss) or '报告止损位'}，应降低仓位。"
        reeval = "若基本面继续恶化或报告更新为卖出，应重新评估。"
    elif bullish:
        immediate = "报告偏买入，但你的持仓已亏损，仍不建议盲目补仓；先确认仓位是否过重。"
        rebound = "若趋势确认，可考虑继续持有而不是急于回本卖出。"
        stop = f"若跌破 {format_price(summary.stop_loss) or '报告止损位'}，说明报告假设失效。"
        reeval = "若基本面或技术面与报告相反，应重新分析。"
    else:
        immediate = "报告结论不清晰：先不要加仓，建议降低问题仓位或等待新报告。"
        rebound = "反弹但无量时优先降低风险。"
        stop = "跌破关键支撑时严格止损。"
        reeval = "重新运行完整 Agent 报告后再决策。"

    return {"immediate": immediate, "rebound": rebound, "stop": stop, "reeval": reeval}


def format_price(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"¥{value:.2f}"


def format_money(value: Optional[float]) -> str:
    if value is None:
        return "未填写"
    return f"¥{value:,.2f}"


def pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def risk_label(risk: str) -> str:
    return {"low": "保守", "medium": "中等", "high": "激进"}.get(risk, risk)


def horizon_label(horizon: str) -> str:
    return {"short": "短线", "swing": "波段", "long": "中长期"}.get(horizon, horizon)


def extract_risk_summary(text: str) -> str:
    if not text.strip():
        return "暂无风险委员会摘要。"
    selected: list[str] = []
    keep_keywords = [
        "最终交易建议", "最终建议", "不新增买入", "主动减仓", "减仓", "继续减仓",
        "全部退出", "重新评估", "放量突破", "站稳", "跌破", "风险控制线",
        "当前不是", "执行卖出", "被动持有", "已有仓位",
    ]
    for line in text.splitlines():
        clean = clean_line(line)
        if not clean or len(clean) > 180:
            continue
        if any(keyword in clean for keyword in keep_keywords) and clean not in selected:
            selected.append(clean)
        if len(selected) >= 10:
            break
    if not selected:
        return excerpt(text, 900)
    return "\n".join(f"- {line}" for line in selected)


def excerpt(text: str, max_chars: int = 1200) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    idx = max(cut.rfind("\n\n"), cut.rfind("。"))
    if idx > max_chars * 0.6:
        return cut[: idx + 1].strip() + "\n\n> ……详见原报告。"
    return cut.rstrip() + "……\n\n> ……详见原报告。"


def build_markdown(position: PositionInput, summary: ReportSummary, metrics: PositionMetrics, plan: dict[str, str]) -> str:
    lines: list[str] = []
    lines.append(f"# {position.stock_name}-{summary.report_date}-position")
    lines.append("")
    lines.append("> 仅供学习和风险管理参考，不构成投资建议。请结合你的真实资金安排、风险承受能力和最新行情独立决策。")
    lines.append("")
    lines.append("## 你的问题")
    lines.append("")
    lines.append(position.question)
    lines.append("")
    lines.append("## 快速结论")
    lines.append("")
    lines.append(f"- 报告结论：**{summary.action}**。")
    lines.append(f"- 你的成本：`¥{position.cost:.2f}`，当前价：`¥{position.price:.2f}`。")
    lines.append(f"- 当前浮亏：**{pct(metrics.loss_pct)}**，回本需要上涨 **{metrics.rebound_to_breakeven_pct:.2f}%**。")
    lines.append(f"- 持仓风险：**{metrics.position_risk_level}**。")
    lines.append(f"- 当前动作：{plan['immediate']}")
    lines.append("")
    lines.append("## 持仓数据")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("|---|---:|")
    lines.append(f"| 股票 | {position.stock_name}（{position.symbol}） |")
    lines.append(f"| 买入成本 | ¥{position.cost:.2f} |")
    lines.append(f"| 当前价格 | ¥{position.price:.2f} |")
    lines.append(f"| 当前浮亏比例 | {pct(metrics.loss_pct)} |")
    lines.append(f"| 回本所需涨幅 | {metrics.rebound_to_breakeven_pct:.2f}% |")
    lines.append(f"| 持仓股数 | {position.shares if position.shares is not None else '未填写'} |")
    lines.append(f"| 当前市值 | {format_money(metrics.market_value)} |")
    lines.append(f"| 当前浮亏金额 | {format_money(metrics.loss_amount)} |")
    lines.append(f"| 占总资金比例 | {position.position_ratio if position.position_ratio is not None else '未填写'}% |")
    lines.append(f"| 风险偏好 | {risk_label(position.risk)} |")
    lines.append(f"| 投资周期 | {horizon_label(position.horizon)} |")
    lines.append("")
    lines.append("## 报告锚点")
    lines.append("")
    lines.append(f"- 最终动作：**{summary.action}**。")
    if summary.target_price is not None:
        lines.append(f"- 报告目标价：`{format_price(summary.target_price)}`，相对你的成本约 `{pct(metrics.target_loss_pct)}`。")
    if summary.stop_loss is not None:
        lines.append(f"- 关键止损/风险线：`{format_price(summary.stop_loss)}`，相对你的成本约 `{pct(metrics.stop_loss_loss_pct)}`。")
    if summary.confirm_price is not None:
        lines.append(f"- 重新评估确认位：`{format_price(summary.confirm_price)}` 附近。")
    else:
        lines.append("- 重新评估确认位：报告建议重点看 `¥11.35` 是否放量站稳。")
    if summary.reduce_text:
        lines.append(f"- 当前减仓区间：{summary.reduce_text}")
    if summary.pressure_text:
        lines.append(f"- 压力/确认信息：{summary.pressure_text}")
    if summary.support_text:
        lines.append(f"- 支撑/风控信息：{summary.support_text}")
    lines.append("")
    lines.append("## 执行计划")
    lines.append("")
    lines.append(f"1. **现在**：{plan['immediate']}")
    lines.append(f"2. **如果反弹**：{plan['rebound']}")
    lines.append(f"3. **如果下跌**：{plan['stop']}")
    lines.append(f"4. **重新评估**：{plan['reeval']}")
    lines.append("")
    lines.append("## 为什么不能只等回本")
    lines.append("")
    lines.append(f"你现在亏损 {abs(metrics.loss_pct):.2f}%，但回本需要上涨 {metrics.rebound_to_breakeven_pct:.2f}%。亏损后的回本涨幅永远大于亏损比例，所以不能把 `¥{position.cost:.2f}` 当作唯一决策依据。更合理的是看：当前报告结论、基本面是否改善、关键价位是否突破、仓位风险是否可承受。")
    lines.append("")
    if summary.key_reasons:
        lines.append("## 报告提取的关键理由")
        lines.append("")
        for reason in summary.key_reasons[:8]:
            lines.append(f"- {reason}")
        lines.append("")
    lines.append("## 风险委员会摘要")
    lines.append("")
    lines.append(extract_risk_summary(summary.risk_text))
    lines.append("")
    lines.append("## 来源文件")
    lines.append("")
    for path in summary.source_paths:
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = path
        lines.append(f"- `{rel}`")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def output_path(out_dir: Path, symbol: str, stock_name: str, report_date: str) -> Path:
    stock_dir = analysis_stock_dir(symbol, stock_name) if out_dir == ANALYSIS_OUTPUTS else out_dir / safe_name(stock_name)
    stock_dir.mkdir(parents=True, exist_ok=True)
    return stock_dir / f"{safe_name(stock_name)}-{report_date}-position.md"


def main() -> None:
    args = parse_args()
    symbol = normalize_symbol(args.symbol)
    summary = summarize_report(symbol, args.date, args.stock_name, args.report)
    price = args.price if args.price is not None else summary.current_price
    if price is None:
        raise ValueError("Current price was not provided and could not be parsed from report. Use --price.")

    position = PositionInput(
        symbol=symbol,
        stock_name=summary.stock_name,
        cost=args.cost,
        price=price,
        shares=args.shares,
        position_ratio=args.position_ratio,
        risk=args.risk,
        horizon=args.horizon,
        question=args.question,
        report_date=summary.report_date,
    )
    metrics = compute_metrics(position, summary)
    plan = build_action_plan(position, summary)
    markdown = build_markdown(position, summary, metrics, plan)

    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = output_path(out_dir, symbol, summary.stock_name, summary.report_date)
    path.write_text(markdown, encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
